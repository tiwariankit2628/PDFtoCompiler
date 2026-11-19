"""Teacher-led classroom tests: join codes, sessions, live monitoring, leaderboard.

Built on top of the existing Exam / ExamQuestion / ExamAttempt / ExamResponse
infrastructure and the existing test-taking UI. A ClassroomSession is a join-code
run of one of the teacher's own published exams; attempts link to the session, so
counts, results and the leaderboard are scoped per session and isolated per
teacher. There is no application-imposed cap on the number of participants.

Grading reuses the server-side, confirmed-key-only path (see
ExamAttempt.uses_confirmed_key_only): a missing key is 'awaiting key', never a
wrong answer. Nothing here sends PDFs or content to external services.
"""
import csv
import time

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import F
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_cache_control
from datetime import timedelta
from decimal import Decimal
import random

from apps.core.permissions import ActiveUserRequiredMixin, StudentRequiredMixin, TeacherRequiredMixin
from apps.core.exam_targets import exam_matches_student, targets_for_track, track_for_target
from apps.core.utils import log_audit
from django.views import View

from .models import ClassroomParticipant, ClassroomSession, Exam, ExamAttempt, ExamResponse


# --- helpers ---------------------------------------------------------------

def normalize_code(raw):
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())[:12]


def _owned_session(user, pk):
    session = get_object_or_404(ClassroomSession.objects.select_related("exam"), pk=pk)
    if session.teacher_id != user.pk and not getattr(user, "is_admin_role", False):
        raise PermissionDenied("You can only manage your own classroom sessions.")
    return session


def join_url(request, session):
    return request.build_absolute_uri(reverse("exams:classroom_join") + f"?code={session.code}")


# Guess throttling: generous per-user cap over a short window. Legitimate students
# succeed on their first try and never accumulate failures, so this never limits
# how many students can join — it only slows blind code guessing.
_GUESS_LIMIT = 20
_GUESS_WINDOW = 300


def _guesses_exhausted(user):
    try:
        return cache.get(f"cc_guess:{user.pk}", 0) >= _GUESS_LIMIT
    except Exception:
        return False


def _register_failed_guess(user):
    try:
        key = f"cc_guess:{user.pk}"
        cache.set(key, cache.get(key, 0) + 1, _GUESS_WINDOW)
    except Exception:
        pass


def _finalize_overdue(session):
    """Refresh server-truthful counts even when a student went offline."""
    # Materialize IDs before starting per-attempt write transactions; keeping a
    # SQLite read cursor open while updating it can cause avoidable lock errors.
    ids = list(session.attempts.filter(status=ExamAttempt.AttemptStatus.IN_PROGRESS,
                                       expires_at__lte=timezone.now()).values_list("pk", flat=True))
    for attempt in ExamAttempt.objects.filter(pk__in=ids).select_related("exam"):
        attempt.auto_timeout_if_expired()


def _score_label(result):
    if not result or not result["best"]:
        return "Not submitted"
    attempt, summary = result["best"], result["summary"]
    if attempt.status == ExamAttempt.AttemptStatus.TERMINATED:
        return "Security review · unranked"
    if not summary["score_available"]:
        return "Awaiting keys · unranked"
    return "Complete" if summary["fully_keyed"] else "Partial · unranked"


def _participant_rows(session, page_number, per_page=25):
    """Latest status and best final result, with no live score presented as final."""
    from .classroom_ranking import classroom_attempts_by_student
    participants = session.participants.select_related("student").order_by("joined_at", "id")
    page = Paginator(participants, per_page).get_page(page_number)
    results = classroom_attempts_by_student(session, [p.student_id for p in page.object_list])
    rows = []
    for p in page.object_list:
        result = results.get(p.student_id)
        latest = result["latest"] if result else None
        best = result["best"] if result else None
        summary = result["summary"] if result else None
        rows.append({
            "name": p.student.get_full_name() or p.student.username,
            "username": p.student.username, "joined_at": p.joined_at,
            "status": latest.status if latest else "joined",
            "status_display": latest.get_status_display() if latest else "Joined",
            "started_at": latest.started_at if latest else None,
            "submitted_at": latest.submitted_at if latest else None,
            "score": float(best.score_obtained) if best and summary and summary["score_available"] else None,
            "score_label": _score_label(result),
            "attempt_uuid": str(best.attempt_uuid if best else latest.attempt_uuid) if latest else None,
            "result_url": reverse("exams:student_result_detail", args=[best.attempt_uuid]) if best else None,
        })
    return page, rows


def _session_leaderboard(session):
    """Rank one best fully keyed eligible result per student."""
    from .classroom_ranking import classroom_attempts_by_student, classroom_ranked_results
    results = classroom_attempts_by_student(session)
    board = []
    for row in classroom_ranked_results(results):
        a = row["best"]
        board.append({
            "rank": row["rank"], "name": a.student.get_full_name() or a.student.username,
            "username": a.student.username, "score": a.score_obtained,
            "correct": a.correct_count, "partial": a.partial_count, "incorrect": a.incorrect_count,
            "percentage": a.percentage, "attempt_uuid": str(a.attempt_uuid),
        })
    return board


# --- teacher and student views --------------------------------------------

class ClassroomNoStoreMixin:
    """Join codes, participant lists and controls must not enter shared caches."""

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        patch_cache_control(response, private=True, no_store=True)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response


class ClassroomHubView(ClassroomNoStoreMixin, TeacherRequiredMixin, View):
    """Classroom workspace: create a session from an owned test, and manage sessions."""

    def get(self, request):
        exams = (Exam.objects.filter(creator=request.user, is_private_practice=False)
                 .order_by("-updated_at")[:50])
        sessions = (ClassroomSession.objects.filter(teacher=request.user)
                    .select_related("exam").order_by("-created_at")[:50])
        session_rows = [{"session": s, "counts": s.counts()} for s in sessions]
        from apps.questions.models import QuestionPaperUpload
        papers = QuestionPaperUpload.objects.filter(uploaded_by=request.user, is_classroom_private=True).order_by("-created_at")[:50]
        return render(request, "exams/classroom/hub.html", {
            "exams": exams, "session_rows": session_rows, "classroom_papers": papers,
        })


class ClassroomSessionCreateView(ClassroomNoStoreMixin, TeacherRequiredMixin, View):
    def post(self, request):
        exam = get_object_or_404(Exam, pk=request.POST.get("exam"), creator=request.user, is_private_practice=False)
        if exam.status != Exam.ExamStatus.PUBLISHED:
            messages.error(request, "Publish the test before starting a classroom session.")
            return redirect("exams:classroom_hub")
        if not exam.exam_questions.exists():
            messages.error(request, "This test has no questions yet.")
            return redirect("exams:classroom_hub")
        allowed = targets_for_track(track_for_target(exam.target_type))
        if not allowed or exam.exam_questions.exclude(question__target_type__in=allowed).exists():
            messages.error(request, "Choose the test's JEE or NEET target and review questions tagged for the other exam first.")
            return redirect("exams:exam_edit", pk=exam.pk) if not exam.is_classroom_only else redirect("exams:classroom_hub")
        session = ClassroomSession.create_for(
            exam, request.user,
            label=(request.POST.get("label") or "").strip()[:120],
            allow_retakes=bool(request.POST.get("allow_retakes")),
            show_leaderboard_to_students=bool(request.POST.get("show_leaderboard_to_students")),
        )
        log_audit(request.user, "CLASSROOM_SESSION_CREATED",
                  f"Classroom session #{session.pk} created.", request=request)
        messages.success(request, f"Classroom session ready. Join code: {session.code}")
        return redirect("exams:classroom_session", pk=session.pk)


class ClassroomSessionDetailView(ClassroomNoStoreMixin, TeacherRequiredMixin, View):
    def get(self, request, pk):
        session = _owned_session(request.user, pk)
        _finalize_overdue(session)
        page, rows = _participant_rows(session, request.GET.get("page"))
        response = render(request, "exams/classroom/session_detail.html", {
            "session": session, "counts": session.counts(), "rows": rows, "page_obj": page,
            "join_url": join_url(request, session),
        })
        patch_cache_control(response, private=True, no_store=True)
        return response


class ClassroomSessionMonitorView(ClassroomNoStoreMixin, TeacherRequiredMixin, View):
    """JSON polling endpoint: live counts + a page of participants. No fake data."""

    def get(self, request, pk):
        session = _owned_session(request.user, pk)
        _finalize_overdue(session)
        page, rows = _participant_rows(session, request.GET.get("page"))
        for r in rows:  # datetimes -> iso for JSON
            r["joined_at"] = r["joined_at"].isoformat() if r["joined_at"] else None
            r["started_at"] = r["started_at"].isoformat() if r["started_at"] else None
            r["submitted_at"] = r["submitted_at"].isoformat() if r["submitted_at"] else None
        response = JsonResponse({
            "counts": session.counts(),
            "joining_allowed": session.joining_allowed,
            "is_open": session.is_open, "is_ended": session.is_ended,
            "server_time": timezone.now().isoformat(),
            "rows": rows, "page": page.number, "num_pages": page.paginator.num_pages,
            "total": page.paginator.count,
        })
        patch_cache_control(response, private=True, no_store=True)
        return response


class ClassroomSessionControlView(ClassroomNoStoreMixin, TeacherRequiredMixin, View):
    def post(self, request, pk):
        _owned_session(request.user, pk)
        for retry in range(5):
            try:
                return self._control_locked(request, pk)
            except OperationalError as exc:
                if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                    raise
                if retry == 4:
                    messages.error(request, "The session is busy. Please try the action again.")
                    return redirect("exams:classroom_session", pk=pk)
                time.sleep(0.025 * (2 ** retry))

    @transaction.atomic
    def _control_locked(self, request, pk):
        if connection.vendor == "sqlite":
            ClassroomSession.objects.filter(pk=pk).update(id=F("id"))
        session = ClassroomSession.objects.select_for_update(of=("self",)).select_related("exam").get(pk=pk)
        action = request.POST.get("action")
        if action == "open":
            if session.is_ended:
                messages.error(request, "An ended session cannot be reopened. Create a new session instead.")
            else:
                session.is_open = True
                session.save(update_fields=["is_open", "updated_at"])
                messages.success(request, "Joining is open.")
        elif action == "close":
            # Closing joining never ends existing attempts.
            session.is_open = False
            session.save(update_fields=["is_open", "updated_at"])
            messages.success(request, "Joining is closed. Students already taking the test can continue.")
        elif action == "regenerate":
            if session.is_ended:
                messages.error(request, "An ended session cannot receive a new code.")
            else:
                session.regenerate_code()
                messages.success(request, f"New join code: {session.code}")
        elif action == "release_results":
            if request.POST.get("confirm") != "yes":
                messages.error(request, "Releasing results and answer review requires confirmation.")
            else:
                session.results_released = True
                session.save(update_fields=["results_released", "updated_at"])
                log_audit(request.user, "CLASSROOM_RESULTS_RELEASED", f"Results released for classroom session #{session.pk}.", request=request)
                messages.success(request, "Results and answer review are now visible to participants in this session.")
        elif action == "end":
            if request.POST.get("confirm") != "yes":
                messages.error(request, "Ending the session requires confirmation.")
            else:
                session.end_now()
                log_audit(request.user, "CLASSROOM_SESSION_ENDED", f"Classroom session #{session.pk} ended.", request=request)
                messages.success(request, "Session ended. In-progress attempts were submitted and scored; saved answers are preserved.")
        else:
            messages.error(request, "Unknown action.")
        return redirect("exams:classroom_session", pk=session.pk)


class ClassroomSessionQrView(ClassroomNoStoreMixin, TeacherRequiredMixin, View):
    """Optional, locally generated QR (via segno). 404 if segno is not installed."""

    def get(self, request, pk):
        session = _owned_session(request.user, pk)
        try:
            import segno  # optional, pure-Python, offline
        except Exception:
            return HttpResponse(status=404)
        import io
        # Segno's SVG serializer writes bytes to file-like objects.
        buf = io.BytesIO()
        segno.make(join_url(request, session), error="m").save(buf, kind="svg", scale=5, border=2)
        return HttpResponse(buf.getvalue(), content_type="image/svg+xml")


class ClassroomLeaderboardView(ClassroomNoStoreMixin, ActiveUserRequiredMixin, View):
    def get(self, request, pk):
        session = get_object_or_404(ClassroomSession.objects.select_related("exam"), pk=pk)
        is_owner = session.teacher_id == request.user.pk or getattr(request.user, "is_admin_role", False)
        if not is_owner:
            # Students see it only if the teacher allowed it and they participated.
            if not (session.show_leaderboard_to_students
                    and session.participants.filter(student=request.user).exists()
                    and exam_matches_student(request.user, session.exam)):
                raise PermissionDenied("This leaderboard is not available for this account.")
        _finalize_overdue(session)
        response = render(request, "exams/classroom/leaderboard.html", {
            "session": session, "leaderboard": _session_leaderboard(session), "is_owner": is_owner,
        })
        patch_cache_control(response, private=True, no_store=True)
        return response


def _spreadsheet_text(value):
    """Prevent names from being interpreted as spreadsheet formulas on import."""
    value = str(value or "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value


class ClassroomExportView(ClassroomNoStoreMixin, TeacherRequiredMixin, View):
    def get(self, request, pk):
        from .classroom_ranking import classroom_attempts_by_student, classroom_ranked_results
        session = _owned_session(request.user, pk)
        _finalize_overdue(session)
        results = classroom_attempts_by_student(session)
        classroom_ranked_results(results)  # adds the same rank used on the leaderboard
        participants = list(session.participants.select_related("student").order_by("joined_at", "id"))

        def order(p):
            result = results.get(p.student_id)
            if not result:
                return (5, 0, 0, 0, p.student.username.casefold(), p.pk)
            best = result["best"]
            quality = result["quality"]
            if quality == 3:
                return (0, -best.score_obtained, best.incorrect_count, best.time_taken_seconds, p.student.username.casefold(), p.pk)
            if quality == 2:
                return (1, -best.score_obtained, best.incorrect_count, best.time_taken_seconds, p.student.username.casefold(), p.pk)
            if quality == 1:
                return (2, 0, 0, 0, p.student.username.casefold(), p.pk)
            if quality == 0:
                return (3, 0, 0, 0, p.student.username.casefold(), p.pk)
            return (4, 0, 0, 0, p.student.username.casefold(), p.pk)

        participants.sort(key=order)
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="classroom_{session.code}_results.csv"'
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        writer = csv.writer(response)
        writer.writerow(["Rank", "Name", "Username", "Status", "Result completeness", "Joined", "Started", "Submitted",
                         "Score (confirmed keys)", "Correct", "Partial credit", "Incorrect", "Unanswered", "Ungraded",
                         "Unconfirmed keys at grading", "Unknown key status", "Percentage (confirmed keys)",
                         "Scored attempt status", "Scored attempt UUID"])
        for p in participants:
            result = results.get(p.student_id)
            latest = result["latest"] if result else None
            best = result["best"] if result else None
            summary = result["summary"] if result else None
            counts = summary["counts"] if summary else {}
            writer.writerow([
                result.get("rank", "") if result else "",
                _spreadsheet_text(p.student.get_full_name() or p.student.username), _spreadsheet_text(p.student.username),
                latest.status if latest else "joined", _score_label(result),
                timezone.localtime(p.joined_at).strftime("%Y-%m-%d %H:%M") if p.joined_at else "",
                timezone.localtime(latest.started_at).strftime("%Y-%m-%d %H:%M") if latest and latest.started_at else "",
                timezone.localtime(latest.submitted_at).strftime("%Y-%m-%d %H:%M") if latest and latest.submitted_at else "",
                f"{best.score_obtained}" if summary and summary["score_available"] else "",
                counts.get("correct", ""), counts.get("partial", ""), counts.get("incorrect", ""), counts.get("unattempted", ""),
                counts.get("ungraded", ""), summary["pending_keys"] if summary else "",
                summary["unknown_keys"] if summary else "",
                f'{summary["percentage"]}' if summary and summary["percentage"] is not None else "",
                best.status if best else "", str(best.attempt_uuid) if best else "",
            ])
        return response


# --- student views ---------------------------------------------------------

class ClassroomJoinView(ClassroomNoStoreMixin, StudentRequiredMixin, View):
    """Prominent 'Join Test with Code'. GET shows the form; POST joins by code."""

    def get(self, request):
        return render(request, "exams/classroom/join.html", {"prefill": normalize_code(request.GET.get("code"))})

    def post(self, request):
        code = normalize_code(request.POST.get("code"))
        if not code:
            messages.error(request, "Enter a join code.")
            return redirect("exams:classroom_join")
        if _guesses_exhausted(request.user):
            messages.error(request, "Too many invalid codes. Please wait a few minutes and try again.")
            return redirect("exams:classroom_join")

        session_id = ClassroomSession.objects.filter(code=code).values_list("pk", flat=True).first()
        if session_id is None:
            _register_failed_guess(request.user)
            messages.error(request, "That code is not valid. Check it and try again.")
            return redirect("exams:classroom_join")

        for retry in range(5):
            try:
                return self._join_once(request, session_id, code)
            except OperationalError as exc:
                if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                    raise
                if retry == 4:
                    messages.error(request, "The session is busy. Please try joining again.")
                    return redirect("exams:classroom_join")
                time.sleep(0.025 * (2 ** retry))

    def _join_once(self, request, session_id, code):
        with transaction.atomic():
            from apps.payments.services import lock_practice_user
            user = lock_practice_user(request.user)
            if not user.is_active or not user.is_student:
                raise PermissionDenied()
            # Serialize code regeneration, joining and end-now. Re-check the
            # submitted code after taking the lock, not only before it.
            if connection.vendor == "sqlite":
                ClassroomSession.objects.filter(pk=session_id).update(id=F("id"))
            session = (ClassroomSession.objects.select_for_update(of=("self",)).select_related("exam")
                       .filter(pk=session_id, code=code).first())
            if session is None:
                _register_failed_guess(request.user)
                messages.error(request, "That code is no longer valid. Ask your teacher for the current code.")
                return redirect("exams:classroom_join")
            session.exam = Exam.objects.select_for_update().get(pk=session.exam_id)

            active = session.attempts.filter(student=request.user, status=ExamAttempt.AttemptStatus.IN_PROGRESS).first()
            if active and not active.is_expired:
                return redirect("exams:exam_session", attempt_uuid=active.attempt_uuid)
            if active:
                active.auto_timeout_if_expired()
            if not exam_matches_student(user, session.exam):
                messages.error(request, "This test is not available for your selected exam, or its target has not been set. Ask your teacher to check it.")
                return redirect("exams:classroom_join")
            if session.is_ended:
                messages.error(request, "This session has ended and is no longer accepting students.")
                return redirect("exams:classroom_join")
            already = session.participants.filter(student=request.user).exists()
            if not session.joining_allowed and not already:
                messages.error(request, "Joining is closed for this session. Ask your teacher to reopen it.")
                return redirect("exams:classroom_join")

            # Idempotent join: a reconnect never creates a second participant.
            _, created = ClassroomParticipant.objects.get_or_create(session=session, student=request.user)
            if created:
                log_audit(request.user, "CLASSROOM_JOINED", f"Joined classroom session #{session.pk}.", request=request)
        return redirect("exams:classroom_take", pk=session.pk)


class ClassroomTakeView(ClassroomNoStoreMixin, StudentRequiredMixin, View):
    """Brief per-session entry screen; Begin starts (or resumes) the attempt."""

    def _session_for(self, request, pk):
        session = get_object_or_404(ClassroomSession.objects.select_related("exam"), pk=pk)
        joined = session.participants.filter(student=request.user).exists()
        if not joined:
            raise PermissionDenied("Join this session with its code first.")
        # Existing live attempts survive a classification change or upgrade.
        active = session.attempts.filter(student=request.user, status=ExamAttempt.AttemptStatus.IN_PROGRESS).first()
        if not (active and not active.is_expired) and not exam_matches_student(request.user, session.exam):
            from django.http import Http404
            raise Http404()
        return session

    def get(self, request, pk):
        session = self._session_for(request, pk)
        active = session.attempts.filter(student=request.user,
                                         status=ExamAttempt.AttemptStatus.IN_PROGRESS).first()
        if active and not active.is_expired:
            return redirect("exams:exam_session", attempt_uuid=active.attempt_uuid)
        if active and active.is_expired:
            active.auto_timeout_if_expired()
        completed = session.attempts.filter(student=request.user).exclude(
            status=ExamAttempt.AttemptStatus.IN_PROGRESS)
        can_start = not session.is_ended and session.exam.status == Exam.ExamStatus.PUBLISHED and (session.allow_retakes or not completed.exists())
        return render(request, "exams/classroom/take.html", {
            "session": session, "exam": session.exam, "can_start": can_start,
            "completed": completed.order_by("-started_at"),
        })

    def post(self, request, pk):
        session = self._session_for(request, pk)
        for retry in range(5):
            try:
                return self._start_once(request, session)
            except OperationalError as exc:
                if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                    raise
                if retry == 4:
                    messages.error(request, "The session is busy. Please try starting again.")
                    return redirect("exams:classroom_take", pk=session.pk)
                time.sleep(0.025 * (2 ** retry))

    def _start_once(self, request, session):
        with transaction.atomic():
            from apps.payments.services import lock_practice_user
            user = lock_practice_user(request.user)
            if not user.is_active or not user.is_student:
                raise PermissionDenied()
            # End-now locks the same row. SQLite needs a write to acquire the
            # lock because SELECT FOR UPDATE is ignored there.
            if connection.vendor == "sqlite":
                ClassroomSession.objects.filter(pk=session.pk).update(id=F("id"))
            session = ClassroomSession.objects.select_for_update(of=("self",)).select_related("exam").get(pk=session.pk)
            session.exam = Exam.objects.select_for_update().get(pk=session.exam_id)
            # Resume an existing active attempt rather than starting a new one.
            active = (ExamAttempt.objects.select_for_update()
                      .filter(classroom_session=session, student=request.user,
                              status=ExamAttempt.AttemptStatus.IN_PROGRESS).first())
            if active:
                if active.is_expired:
                    active.auto_timeout_if_expired()
                else:
                    return redirect("exams:exam_session", attempt_uuid=active.attempt_uuid)

            if not exam_matches_student(user, session.exam):
                from django.http import Http404
                raise Http404()
            if session.is_ended or session.exam.status != Exam.ExamStatus.PUBLISHED:
                messages.error(request, "This session has ended or its test is no longer available.")
                return redirect("exams:classroom_take", pk=session.pk)

            completed = session.attempts.filter(student=request.user).exclude(
                status=ExamAttempt.AttemptStatus.IN_PROGRESS)
            if completed.exists() and not session.allow_retakes:
                messages.info(request, "You have already completed this test.")
                last = completed.order_by("-started_at").first()
                return redirect("exams:exam_completed", attempt_uuid=last.attempt_uuid)

            exam = session.exam
            exam_questions = list(exam.exam_questions.select_related("question").order_by("order", "id"))
            if not exam_questions:
                messages.error(request, "This test has no questions.")
                return redirect("exams:classroom_take", pk=session.pk)

            started_at = timezone.now()
            try:
                # A savepoint keeps the enclosing transaction usable if a
                # concurrent start loses the unique-constraint race.
                with transaction.atomic():
                    attempt = ExamAttempt.objects.create(
                        exam=exam, student=request.user, classroom_session=session,
                        status=ExamAttempt.AttemptStatus.IN_PROGRESS, started_at=started_at,
                        expires_at=started_at + timedelta(minutes=exam.duration_minutes),
                        total_marks=exam.total_marks,
                    )
            except IntegrityError:
                # Lost a concurrent-start race; resume the winner's attempt.
                existing = session.attempts.filter(student=request.user,
                                                   status=ExamAttempt.AttemptStatus.IN_PROGRESS).first()
                if existing:
                    return redirect("exams:exam_session", attempt_uuid=existing.attempt_uuid)
                raise

            # started_at uses auto_now_add, so derive expiry from the persisted
            # timestamp rather than the earlier pre-insert clock reading.
            attempt.expires_at = attempt.started_at + timedelta(minutes=exam.duration_minutes)
            attempt.save(update_fields=["expires_at"])
            ordered = list(exam_questions)
            if exam.shuffle_questions:
                random.shuffle(ordered)
            ExamResponse.objects.bulk_create([
                ExamResponse(attempt=attempt, exam_question=eq, order=i + 1, is_visited=(i == 0))
                for i, eq in enumerate(ordered)
            ])
            attempt.last_active_question = ordered[0]
            attempt.save(update_fields=["last_active_question"])
            log_audit(request.user, "CLASSROOM_ATTEMPT_STARTED",
                      f"Started classroom session #{session.pk} (attempt #{attempt.id}).", request=request)
        return redirect("exams:exam_session", attempt_uuid=attempt.attempt_uuid)
