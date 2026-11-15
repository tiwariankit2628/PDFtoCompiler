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
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
import random

from apps.core.permissions import ActiveUserRequiredMixin, TeacherRequiredMixin
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


def _participant_rows(session, page_number, per_page=25):
    """A page of participants with their best/last attempt status and score."""
    participants = (session.participants.select_related("student")
                    .order_by("joined_at", "id"))
    paginator = Paginator(participants, per_page)
    page = paginator.get_page(page_number)
    # Map student -> most relevant attempt (latest) for this session.
    attempts = {a.student_id: a for a in session.attempts.select_related("student").order_by("student_id", "-started_at")}
    rows = []
    for p in page.object_list:
        a = attempts.get(p.student_id)
        rows.append({
            "name": p.student.get_full_name() or p.student.username,
            "username": p.student.username,
            "joined_at": p.joined_at,
            "status": a.status if a else "joined",
            "started_at": a.started_at if a else None,
            "submitted_at": a.submitted_at if a else None,
            "score": float(a.score_obtained) if a and a.status in (
                ExamAttempt.AttemptStatus.SUBMITTED, ExamAttempt.AttemptStatus.EVALUATED,
                ExamAttempt.AttemptStatus.TIMED_OUT) else None,
            "attempt_uuid": str(a.attempt_uuid) if a else None,
        })
    return page, rows


def _session_leaderboard(session):
    """Graded attempts ranked by score (ties: fewer wrong, then faster).

    Only confirmed-key graded attempts are ranked. With retakes, each student's
    best graded attempt is used. Ungraded/awaiting-key attempts are not ranked.
    """
    final = (session.attempts.select_related("student").filter(
        status__in=[ExamAttempt.AttemptStatus.SUBMITTED, ExamAttempt.AttemptStatus.EVALUATED,
                    ExamAttempt.AttemptStatus.TIMED_OUT]))
    best = {}
    for a in final:
        cur = best.get(a.student_id)
        key = (a.score_obtained, -a.incorrect_count, -a.time_taken_seconds)
        if cur is None or key > cur[0]:
            best[a.student_id] = (key, a)
    ranked = sorted((v[1] for v in best.values()),
                    key=lambda a: (a.score_obtained, -a.incorrect_count, -a.time_taken_seconds), reverse=True)
    rows, prev_key, rank = [], None, 0
    for i, a in enumerate(ranked, 1):
        k = (a.score_obtained, a.incorrect_count, a.time_taken_seconds)
        if k != prev_key:
            rank = i
            prev_key = k
        rows.append({
            "rank": rank, "name": a.student.get_full_name() or a.student.username,
            "username": a.student.username, "score": a.score_obtained,
            "correct": a.correct_count, "incorrect": a.incorrect_count,
            "percentage": a.percentage, "attempt_uuid": str(a.attempt_uuid),
        })
    return rows


# --- teacher views ---------------------------------------------------------

class ClassroomHubView(TeacherRequiredMixin, View):
    """Classroom workspace: create a session from an owned test, and manage sessions."""

    def get(self, request):
        exams = (Exam.objects.filter(creator=request.user, is_private_practice=False)
                 .order_by("-updated_at")[:50])
        sessions = (ClassroomSession.objects.filter(teacher=request.user)
                    .select_related("exam").order_by("-created_at")[:50])
        session_rows = [{"session": s, "counts": s.counts()} for s in sessions]
        return render(request, "exams/classroom/hub.html", {
            "exams": exams, "session_rows": session_rows,
        })


class ClassroomSessionCreateView(TeacherRequiredMixin, View):
    def post(self, request):
        exam = get_object_or_404(Exam, pk=request.POST.get("exam"), creator=request.user, is_private_practice=False)
        if exam.status != Exam.ExamStatus.PUBLISHED:
            messages.error(request, "Publish the test before starting a classroom session.")
            return redirect("exams:classroom_hub")
        if not exam.exam_questions.exists():
            messages.error(request, "This test has no questions yet.")
            return redirect("exams:classroom_hub")
        session = ClassroomSession.create_for(
            exam, request.user,
            label=(request.POST.get("label") or "").strip()[:120],
            allow_retakes=bool(request.POST.get("allow_retakes")),
            show_leaderboard_to_students=bool(request.POST.get("show_leaderboard_to_students")),
        )
        log_audit(request.user, "CLASSROOM_SESSION_CREATED",
                  f"Session {session.code} for exam '{exam.title}'.", request=request)
        messages.success(request, f"Classroom session ready. Join code: {session.code}")
        return redirect("exams:classroom_session", pk=session.pk)


class ClassroomSessionDetailView(TeacherRequiredMixin, View):
    def get(self, request, pk):
        session = _owned_session(request.user, pk)
        page, rows = _participant_rows(session, request.GET.get("page"))
        return render(request, "exams/classroom/session_detail.html", {
            "session": session, "counts": session.counts(), "rows": rows, "page_obj": page,
            "join_url": join_url(request, session),
            "leaderboard": _session_leaderboard(session),
        })


class ClassroomSessionMonitorView(TeacherRequiredMixin, View):
    """JSON polling endpoint: live counts + a page of participants. No fake data."""

    def get(self, request, pk):
        session = _owned_session(request.user, pk)
        page, rows = _participant_rows(session, request.GET.get("page"))
        for r in rows:  # datetimes -> iso for JSON
            r["joined_at"] = r["joined_at"].isoformat() if r["joined_at"] else None
            r["started_at"] = r["started_at"].isoformat() if r["started_at"] else None
            r["submitted_at"] = r["submitted_at"].isoformat() if r["submitted_at"] else None
        return JsonResponse({
            "counts": session.counts(),
            "joining_allowed": session.joining_allowed,
            "is_open": session.is_open, "is_ended": session.is_ended,
            "server_time": timezone.now().isoformat(),
            "rows": rows, "page": page.number, "num_pages": page.paginator.num_pages,
            "total": page.paginator.count,
        })


class ClassroomSessionControlView(TeacherRequiredMixin, View):
    def post(self, request, pk):
        session = _owned_session(request.user, pk)
        action = request.POST.get("action")
        if action == "open":
            session.is_open = True
            session.save(update_fields=["is_open", "updated_at"])
            messages.success(request, "Joining is open.")
        elif action == "close":
            # Closing joining never ends existing attempts.
            session.is_open = False
            session.save(update_fields=["is_open", "updated_at"])
            messages.success(request, "Joining is closed. Students already taking the test can continue.")
        elif action == "regenerate":
            session.regenerate_code()
            messages.success(request, f"New join code: {session.code}")
        elif action == "end":
            if request.POST.get("confirm") != "yes":
                messages.error(request, "Ending the session requires confirmation.")
            else:
                session.end_now()
                log_audit(request.user, "CLASSROOM_SESSION_ENDED", f"Session {session.code} ended.", request=request)
                messages.success(request, "Session ended. In-progress attempts were submitted and scored; saved answers are preserved.")
        else:
            messages.error(request, "Unknown action.")
        return redirect("exams:classroom_session", pk=session.pk)


class ClassroomSessionQrView(TeacherRequiredMixin, View):
    """Optional, locally generated QR (via segno). 404 if segno is not installed."""

    def get(self, request, pk):
        session = _owned_session(request.user, pk)
        try:
            import segno  # optional, pure-Python, offline
        except Exception:
            return HttpResponse(status=404)
        import io
        buf = io.StringIO()
        segno.make(join_url(request, session), error="m").save(buf, kind="svg", scale=5, border=2)
        return HttpResponse(buf.getvalue(), content_type="image/svg+xml")


class ClassroomLeaderboardView(ActiveUserRequiredMixin, View):
    def get(self, request, pk):
        session = get_object_or_404(ClassroomSession.objects.select_related("exam"), pk=pk)
        is_owner = session.teacher_id == request.user.pk or getattr(request.user, "is_admin_role", False)
        if not is_owner:
            # Students see it only if the teacher allowed it and they participated.
            if not (session.show_leaderboard_to_students
                    and session.participants.filter(student=request.user).exists()):
                raise PermissionDenied("This leaderboard is private to the teacher.")
        return render(request, "exams/classroom/leaderboard.html", {
            "session": session, "leaderboard": _session_leaderboard(session), "is_owner": is_owner,
        })


class ClassroomExportView(TeacherRequiredMixin, View):
    def get(self, request, pk):
        session = _owned_session(request.user, pk)
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="classroom_{session.code}_results.csv"'
        writer = csv.writer(response)
        writer.writerow(["Name", "Username", "Status", "Joined", "Started", "Submitted",
                         "Score", "Correct", "Incorrect", "Unanswered", "Percentage"])
        attempts = {a.student_id: a for a in session.attempts.select_related("student").order_by("student_id", "-started_at")}
        for p in session.participants.select_related("student").order_by("joined_at"):
            a = attempts.get(p.student_id)
            graded = a and a.status in (ExamAttempt.AttemptStatus.SUBMITTED,
                                        ExamAttempt.AttemptStatus.EVALUATED, ExamAttempt.AttemptStatus.TIMED_OUT)
            writer.writerow([
                p.student.get_full_name() or p.student.username, p.student.username,
                a.status if a else "joined",
                timezone.localtime(p.joined_at).strftime("%Y-%m-%d %H:%M") if p.joined_at else "",
                timezone.localtime(a.started_at).strftime("%Y-%m-%d %H:%M") if a and a.started_at else "",
                timezone.localtime(a.submitted_at).strftime("%Y-%m-%d %H:%M") if a and a.submitted_at else "",
                f"{a.score_obtained}" if graded else "", a.correct_count if graded else "",
                a.incorrect_count if graded else "", a.unattempted_count if graded else "",
                f"{a.percentage}" if graded else "",
            ])
        return response


# --- student views ---------------------------------------------------------

class ClassroomJoinView(ActiveUserRequiredMixin, View):
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

        session = ClassroomSession.objects.select_related("exam").filter(code=code).first()
        if session is None:
            _register_failed_guess(request.user)
            messages.error(request, "That code is not valid. Check it and try again.")
            return redirect("exams:classroom_join")

        # Already taking it? Always resume the existing active attempt.
        active = session.attempts.filter(student=request.user,
                                         status=ExamAttempt.AttemptStatus.IN_PROGRESS).first()
        if active and not active.is_expired:
            return redirect("exams:exam_session", attempt_uuid=active.attempt_uuid)

        if session.is_ended:
            messages.error(request, "This session has ended and is no longer accepting students.")
            return redirect("exams:classroom_join")
        already = session.participants.filter(student=request.user).exists()
        if not session.joining_allowed and not already:
            messages.error(request, "Joining is closed for this session. Ask your teacher to reopen it.")
            return redirect("exams:classroom_join")

        # Idempotent join: a reconnect never creates a second participant.
        ClassroomParticipant.objects.get_or_create(session=session, student=request.user)
        log_audit(request.user, "CLASSROOM_JOINED", f"Joined session {session.code}.", request=request)
        return redirect("exams:classroom_take", pk=session.pk)


class ClassroomTakeView(ActiveUserRequiredMixin, View):
    """Brief per-session entry screen; Begin starts (or resumes) the attempt."""

    def _session_for(self, request, pk):
        session = get_object_or_404(ClassroomSession.objects.select_related("exam"), pk=pk)
        joined = session.participants.filter(student=request.user).exists()
        if not joined:
            raise PermissionDenied("Join this session with its code first.")
        return session

    def get(self, request, pk):
        session = self._session_for(request, pk)
        active = session.attempts.filter(student=request.user,
                                         status=ExamAttempt.AttemptStatus.IN_PROGRESS).first()
        if active and not active.is_expired:
            return redirect("exams:exam_session", attempt_uuid=active.attempt_uuid)
        completed = session.attempts.filter(student=request.user).exclude(
            status=ExamAttempt.AttemptStatus.IN_PROGRESS)
        can_start = session.allow_retakes or not completed.exists()
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
                if connection.vendor != "sqlite" or "locked" not in str(exc).lower() or retry == 4:
                    raise
                time.sleep(0.025 * (2 ** retry))

    def _start_once(self, request, session):
        with transaction.atomic():
            # Resume an existing active attempt rather than starting a new one.
            active = (ExamAttempt.objects.select_for_update()
                      .filter(classroom_session=session, student=request.user,
                              status=ExamAttempt.AttemptStatus.IN_PROGRESS).first())
            if active:
                if active.is_expired:
                    active.auto_timeout_if_expired()
                else:
                    return redirect("exams:exam_session", attempt_uuid=active.attempt_uuid)

            if session.is_ended:
                messages.error(request, "This session has ended.")
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
                      f"Started session {session.code} (attempt #{attempt.id}).", request=request)
        return redirect("exams:exam_session", attempt_uuid=attempt.attempt_uuid)
