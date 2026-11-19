"""Explicit, owner-authorized classification of pre-existing unclassified work."""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, connection, transaction
from django.db.models import F, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View

from apps.core.exam_targets import ExamTarget, targets_for_track, track_for_target
from apps.core.permissions import ActiveUserRequiredMixin, StudentRequiredMixin
from apps.core.utils import log_audit
from apps.exams.models import Exam, ExamAttempt
from .models import Question, QuestionPaperUpload


def _live_attempts(queryset):
    return queryset.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
                           status=ExamAttempt.AttemptStatus.IN_PROGRESS)


class PaperTargetView(ActiveUserRequiredMixin, View):
    """Classify only an unclassified paper; never overwrite an existing label."""
    def post(self, request, pk):
        try:
            return self._classify(request, pk)
        except OperationalError as exc:
            if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                raise
            response = HttpResponse("This paper is busy. Please retry; existing timers continue.", status=503)
            response["Retry-After"] = "1"
            return response

    def _classify(self, request, pk):
        user = request.user
        target = request.POST.get("target_type", "")
        if target not in ExamTarget.values:
            messages.error(request, "Choose a valid JEE or NEET target.")
            return redirect("core:dashboard_router")
        with transaction.atomic():
            if user.is_student:
                from apps.payments.services import lock_practice_user
                user = lock_practice_user(user)
            if connection.vendor == "sqlite":
                # SQLite ignores SELECT FOR UPDATE. Obtain its writer lock
                # before reading related attempts and questions.
                QuestionPaperUpload.objects.filter(pk=pk).update(id=F("id"))
            paper = get_object_or_404(QuestionPaperUpload.objects.select_for_update(), pk=pk)
            if paper.is_student_private:
                if not user.is_student or paper.uploaded_by_id != user.pk or target not in targets_for_track(user.exam_track):
                    raise Http404()
                destination = "questions:practice_list" if paper.practice_exam_id else "questions:practice_settings"
                kwargs = {} if paper.practice_exam_id else {"pk": paper.pk}
                if _live_attempts(ExamAttempt.objects.filter(student=user)).exists():
                    messages.error(request, "Finish your active test before classifying an older paper.")
                    return redirect("questions:practice_list")
            elif paper.is_classroom_private:
                if not user.is_teacher or paper.uploaded_by_id != user.pk:
                    raise Http404()
                destination, kwargs = "questions:classroom_paper_review", {"pk": paper.pk}
            else:
                if not (user.is_admin_role or user.is_teacher and (
                    paper.uploaded_by_id == user.pk or user.assigned_categories.filter(pk=paper.category_id).exists()
                )):
                    raise PermissionDenied()
                destination, kwargs = "questions:paper_review", {"pk": paper.pk}
            if paper.target_type:
                messages.info(request, "This paper already has an exam target; it was not changed.")
                return redirect(destination, **kwargs)
            # Source questions can also be shared with public tests. Lock every
            # linked exam (not just unclassified classroom/private tests) before
            # checking live attempts or changing any question. Candidate starts
            # and exam edits take the same exam lock; bank edits take the same
            # question locks below. Separate ID selection avoids PostgreSQL's
            # FOR UPDATE + DISTINCT limitation.
            linked_ids = list(Exam.objects.filter(
                Q(pk=paper.practice_exam_id) | Q(exam_questions__question__source_paper=paper)
            ).values_list("pk", flat=True).distinct())
            linked = list(Exam.objects.select_for_update().filter(pk__in=linked_ids).order_by("pk"))
            practice_exam = next((exam for exam in linked if exam.pk == paper.practice_exam_id), None)
            if practice_exam and practice_exam.target_type and track_for_target(practice_exam.target_type) != track_for_target(target):
                messages.error(request, "The existing test is tagged for a different track; this paper was not changed.")
                return redirect(destination, **kwargs)
            questions = list(Question.objects.select_for_update().filter(source_paper=paper).order_by("pk"))
            if _live_attempts(ExamAttempt.objects.filter(exam__in=linked)).exists():
                messages.error(request, "Wait for the paper's active tests to finish before classifying it.")
                return redirect(destination, **kwargs)
            allowed = targets_for_track(track_for_target(target))
            # Only newly classified, source-linked questions with no conflicting
            # tagged exam receive the same label. Historical scores never change.
            conflicting_count = 0
            for question in questions:
                if question.target_type:
                    continue
                conflicting = question.exam_instances.exclude(exam__target_type="").exclude(exam__target_type__in=allowed).exists()
                if conflicting:
                    conflicting_count += 1
                else:
                    Question.objects.filter(pk=question.pk, target_type="").update(target_type=target)
            for exam in linked:
                # A public test can use questions from many sources; classify it
                # explicitly in its editor instead of inferring from one paper.
                if exam.target_type or (exam.pk != paper.practice_exam_id and not exam.is_classroom_only):
                    continue
                # Classroom snapshots must be entirely from this paper; never
                # infer a mixed exam's target from one of several source papers.
                if exam.is_classroom_only and exam.exam_questions.exclude(question__source_paper=paper).exists():
                    continue
                if not exam.exam_questions.exclude(question__target_type__in=allowed).exists():
                    Exam.objects.filter(pk=exam.pk, target_type="").update(target_type=target)
            paper.target_type = target
            paper.save(update_fields=["target_type"])
        log_audit(user, "PAPER_TARGET_SET", f"Classified paper #{paper.pk} as {target}.", request=request)
        messages.success(request, "Paper target saved. Existing answers, scores and timers were not changed.")
        if conflicting_count:
            messages.warning(request, f"{conflicting_count} linked question(s) remain unclassified because another test has a conflicting target. Review them individually.")
        return redirect(destination, **kwargs)


class LegacyPracticeTestTargetView(StudentRequiredMixin, View):
    """Explicitly classify an owner's older test and its unclassified questions."""
    def post(self, request, pk):
        from apps.payments.services import lock_practice_user

        target = request.POST.get("target_type", "")
        with transaction.atomic():
            user = lock_practice_user(request.user)
            allowed = targets_for_track(user.exam_track)
            if not user.is_student or target not in allowed:
                raise Http404()
            exam = get_object_or_404(Exam.objects.select_for_update(), pk=pk, creator=user,
                                     is_private_practice=True)
            if request.POST.get("confirm_questions") != "on":
                messages.error(request, "Confirm that you checked the test and its questions against the selected exam.")
            elif exam.target_type and exam.target_type != target:
                messages.error(request, "This test already has a different target; it was not changed.")
            elif _live_attempts(ExamAttempt.objects.filter(student=user)).exists():
                messages.error(request, "Finish your active test before classifying older questions.")
            else:
                questions = list(Question.objects.select_for_update().filter(
                    pk__in=exam.exam_questions.values("question_id")).order_by("pk"))
                invalid = any(q.creator_id != user.pk or not q.is_private_practice or
                              (q.target_type and q.target_type not in allowed) for q in questions)
                # A question may be shared by older tests. Never reclassify it
                # if any already-classified test belongs to the other track.
                for question in questions:
                    if not question.target_type and question.exam_instances.exclude(
                            exam__target_type="").exclude(exam__target_type__in=allowed).exists():
                        invalid = True
                if invalid:
                    messages.error(request, "Some questions belong to another exam or conflict with another test. Review them individually; nothing was changed.")
                else:
                    Question.objects.filter(pk__in=[q.pk for q in questions], target_type="").update(target_type=target)
                    if not exam.target_type:
                        exam.target_type = target
                        exam.save(update_fields=["target_type"])
                    log_audit(user, "PRACTICE_TARGET_SET", f"Classified private test #{exam.pk} as {target}.", request=request)
                    messages.success(request, "Test and unclassified questions updated. Previous attempts and scores are unchanged.")
        return redirect("questions:practice_list")
