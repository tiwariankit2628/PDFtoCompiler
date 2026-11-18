"""
Candidate and Institutional CBT Examination Analytics & Reporting Views.
Handles student result scorecards, question-level solution review, PDF scorecard generation,
public leaderboards, and teacher/administrator cohort performance analytics.
"""
import csv
import json
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_cache_control
from django.views import View
from django.views.generic import DetailView, ListView

from apps.core.permissions import ActiveUserRequiredMixin, TeacherRequiredMixin
from apps.core.utils import log_audit
from apps.questions.presentation import missing_crop_pages, primary_question_crops, use_original_image
from .analytics import compute_student_attempt_analytics, compute_exam_institutional_analytics
from .models import Exam, ExamAttempt, ExamResponse
from .pdf_generator import generate_student_scorecard_pdf


def _ensure_final_for_results(attempt):
    """Do not present an active attempt as a completed result or scorecard."""
    if attempt.status == ExamAttempt.AttemptStatus.IN_PROGRESS and attempt.is_expired:
        attempt.auto_timeout_if_expired()
    if attempt.status == ExamAttempt.AttemptStatus.IN_PROGRESS:
        raise PermissionDenied("Results are available after this attempt ends.")


def _can_access_attempt_result(user, attempt):
    if attempt.student_id == user.pk or user.is_admin_role:
        return True
    if not user.is_teacher:
        return False
    if attempt.classroom_session_id:
        return attempt.classroom_session.teacher_id == user.pk
    return (attempt.exam.creator_id == user.pk
            or user.assigned_categories.filter(pk=attempt.exam.category_id).exists())


class PrivateResultRedirectMixin:
    """Keep private results separate and protect an active institutional test.

    Dispatch is used rather than get so the same policy applies to the PDF
    view, which implements its own get method. Previous results from the same
    examination must not reveal answers while a new attempt is in progress.
    """
    def dispatch(self, request, *args, **kwargs):
        attempt = get_object_or_404(ExamAttempt.objects.select_related("exam"), attempt_uuid=kwargs["attempt_uuid"])
        if attempt.exam.is_private_practice:
            if attempt.student_id != request.user.pk or attempt.exam.creator_id != request.user.pk:
                raise Http404()
            return redirect("questions:practice_result", attempt_uuid=attempt.attempt_uuid)
        if request.user.is_student and attempt.student_id == request.user.pk:
            active_attempts = ExamAttempt.objects.filter(
                exam_id=attempt.exam_id, student=request.user, status=ExamAttempt.AttemptStatus.IN_PROGRESS,
            ).select_related("exam").order_by("started_at")
            for active in active_attempts:
                if active.is_expired:
                    active.auto_timeout_if_expired()
                else:
                    return redirect("exams:exam_session", attempt_uuid=active.attempt_uuid)
        response = super().dispatch(request, *args, **kwargs)
        if attempt.classroom_session_id:
            # Results and answer reviews are private to this attempt even after
            # the session's teacher has released them to its participants.
            patch_cache_control(response, private=True, no_store=True)
        return response


class StudentResultDetailView(ActiveUserRequiredMixin, PrivateResultRedirectMixin, DetailView):
    """
    Detailed Student Result Scorecard and Diagnostic Dashboard.
    Displays marks, percentage, pass/fail status, accuracy, subject/topic analytics,
    strengths, weaknesses, time management analysis, rank, and percentile.
    """

    model = ExamAttempt
    template_name = "exams/result_detail.html"
    context_object_name = "attempt"
    slug_field = "attempt_uuid"
    slug_url_kwarg = "attempt_uuid"

    def get_template_names(self):
        if self.object.classroom_session_id:
            return ["exams/classroom/result.html"]
        return super().get_template_names()

    def get_object(self, queryset=None):
        attempt = super().get_object(queryset)
        user = self.request.user

        # Access Control: Only the candidate or authorized teachers/admins can view this result
        if not _can_access_attempt_result(user, attempt):
            raise PermissionDenied("You do not have permission to view this student's result.")

        _ensure_final_for_results(attempt)
        return attempt

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        attempt = self.object
        exam = attempt.exam
        user = self.request.user

        # Ensure attempt is evaluated
        if attempt.status == ExamAttempt.AttemptStatus.IN_PROGRESS and attempt.is_expired:
            attempt.auto_timeout_if_expired()

        # Check Result Visibility Settings
        student_scores_visible = (attempt.classroom_session.student_results_visible if attempt.classroom_session_id
                                  else exam.show_result_immediately)
        can_view_scores = bool(student_scores_visible or user.is_admin_role or user.is_teacher)
        context["can_view_scores"] = can_view_scores
        context["exam"] = exam

        if attempt.classroom_session_id:
            # The ordinary institutional report assumes every answer has a key;
            # classroom attempts explicitly allow unverified/ungraded answers.
            if can_view_scores:
                from .classroom_results import classroom_result_summary
                context["classroom_summary"] = classroom_result_summary(attempt)
                context["can_review"] = bool(attempt.classroom_session.student_answers_visible or user.is_admin_role or user.is_teacher)
            is_reviewer = bool(user.is_admin_role or (user.is_teacher and attempt.classroom_session.teacher_id == user.pk))
            context["is_security_reviewer"] = is_reviewer
            if is_reviewer:
                context["security_logs"] = attempt.security_logs.all()[:200]
                context["security_tab_switches"] = attempt.tab_switch_warnings
                context["security_locked"] = attempt.is_security_locked
            return context

        if can_view_scores and attempt.has_ungraded_keys:
            from .pdf_key_results import partial_pdf_result_summary
            context["partial_summary"] = partial_pdf_result_summary(attempt)
        elif can_view_scores:
            analytics = compute_student_attempt_analytics(attempt)
            context["analytics"] = analytics

            # Prepare JSON for Chart.js rendering
            context["subject_labels_json"] = json.dumps([s["subject"] for s in analytics["subject_summary"]])
            context["subject_scores_json"] = json.dumps([float(s["percentage"]) for s in analytics["subject_summary"]])
            context["subject_accuracy_json"] = json.dumps([float(s["accuracy"]) for s in analytics["subject_summary"]])

            context["difficulty_labels_json"] = json.dumps([d["level"] for d in analytics["difficulty_summary"]])
            context["difficulty_accuracy_json"] = json.dumps([float(d["accuracy"]) for d in analytics["difficulty_summary"]])

            context["score_distribution_json"] = json.dumps({
                "Correct": analytics["correct_count"],
                "Partial credit": analytics["partial_count"],
                "Incorrect": analytics["incorrect_count"],
                "Unattempted": analytics["unattempted_count"],
            })

        # Previous attempts by this student on this examination
        context["other_attempts"] = (
            ExamAttempt.objects.filter(exam=exam, student=attempt.student, classroom_session__isnull=True)
            .exclude(id=attempt.id)
            .order_by("-started_at")
        )

        # Security review (authorized teacher/admin only): reported signals, not
        # proof of cheating, and shown without changing the score or the timer.
        is_reviewer = bool(user.is_admin_role or (user.is_teacher and (
            exam.creator_id == user.pk or user.assigned_categories.filter(pk=exam.category_id).exists())))
        context["is_security_reviewer"] = is_reviewer
        if is_reviewer:
            context["security_logs"] = attempt.security_logs.all()[:200]
            context["security_tab_switches"] = attempt.tab_switch_warnings
            context["security_mixed_count"] = attempt.anti_cheat_warnings
            context["security_locked"] = attempt.is_security_locked
            context["strict_mode"] = exam.strict_exam_mode

        return context


class StudentResultReviewView(ActiveUserRequiredMixin, PrivateResultRedirectMixin, DetailView):
    """
    Question-by-Question Solution & Answer Review Workspace.
    Allows candidates to review their selected answers, correct answers,
    detailed explanations, KaTeX equations, and marks awarded per question.
    Strictly enforces exam.allow_review_answers and result release settings.
    """

    model = ExamAttempt
    template_name = "exams/result_review.html"
    context_object_name = "attempt"
    slug_field = "attempt_uuid"
    slug_url_kwarg = "attempt_uuid"

    def get_object(self, queryset=None):
        attempt = super().get_object(queryset)
        user = self.request.user

        if not _can_access_attempt_result(user, attempt):
            raise PermissionDenied("You do not have permission to review these answers.")

        _ensure_final_for_results(attempt)
        return attempt

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        attempt = self.object
        exam = attempt.exam
        user = self.request.user

        # Security & Policy Verification
        student_can_review = (attempt.classroom_session.student_answers_visible if attempt.classroom_session_id
                              else exam.allow_review_answers and (exam.show_result_immediately or exam.is_expired))
        can_review = bool(student_can_review or user.is_admin_role or user.is_teacher)
        context["can_review"] = can_review
        context["exam"] = exam

        if not can_review:
            context["review_blocked_reason"] = (
                "Answer key and step-by-step solutions have been restricted by the examination authority for this test."
            )
            return context

        # Fetch responses with questions, options, and solutions
        responses = (
            attempt.responses.select_related(
                "exam_question__question",
                "selected_option",
            )
            .prefetch_related("exam_question__question__options__figures", "exam_question__question__figures")
            .order_by("order")
        )

        # Prepare detailed question cards
        reviewed_questions = []
        for r in responses:
            eq = r.exam_question
            q = eq.question
            options = list(q.options.all())
            correct_options = [opt for opt in options if opt.is_correct]
            # PDF/classroom reviews must use the recorded decision even for
            # legacy rows with no key-label snapshot. Today's editable key is
            # not evidence of the key that was used for a past attempt.
            use_snapshot = bool(attempt.classroom_session_id or q.is_pdf_derived
                                or q.question_type in ("mcq_multiple", "numerical")
                                or r.key_confirmed_at_grading is not None)
            if use_snapshot:
                from .classroom_results import classroom_response_status
                status = classroom_response_status(r)
                # Never replace an older saved grade with today's editable key.
                # For new attempts the original option label was snapshotted;
                # legacy attempts without it do not expose an invented answer.
                saved_key = r.correct_option_key_at_grading if r.key_confirmed_at_grading is True else None
                matching = [opt for opt in options if opt.option_key == saved_key] if saved_key is not None else []
                correct_option = matching[0] if len(matching) == 1 else None
                current_key_matches = bool(
                    saved_key is not None and q.answer_status == "manually_verified"
                    and q.question_type == "mcq_single"
                    and len(correct_options) == 1 and correct_options[0].option_key == saved_key
                )
                explanation = q.explanation if current_key_matches else ""
            else:
                saved_key = None
                correct_option = correct_options[0] if correct_options else None
                explanation = q.explanation
                if r.selected_option is None:
                    status = "unattempted"
                elif r.selected_option.is_correct:
                    status = "correct"
                else:
                    status = "incorrect"
            from .typed_responses import typed_snapshot_review
            typed = typed_snapshot_review(r)
            if typed:
                saved_key = typed["key"]
                correct_option = None
                explanation = typed["explanation"]  # grading-time solution, never today's edited value
            status_label, badge_class = {
                "correct": ("Correct", "success"), "partial": ("Partial credit", "warning"),
                "incorrect": ("Incorrect", "danger"),
                "ungraded": ("Answered, ungraded", "secondary"), "unattempted": ("Unattempted", "secondary"),
            }[status]

            private_images = bool(attempt.classroom_session_id)
            wants_crop = use_original_image(q, image_first=exam.image_first_mode, private=private_images)
            primary_crops = primary_question_crops(q, image_first=wants_crop, private=private_images)
            incomplete_pages = missing_crop_pages(q, primary_crops) if primary_crops else []
            reviewed_questions.append({
                "response": r,
                "order": r.order,
                "section": eq.section,
                "question": q,
                "primary_crops": primary_crops,
                "missing_crop_pages": incomplete_pages,
                "original_image_missing": bool(wants_crop and not primary_crops),
                "options": options,
                "selected_option": r.selected_option,
                "correct_option": correct_option,
                "saved_key": saved_key,
                "saved_choice": typed["answer"] if typed else r.selected_option_key_at_grading if use_snapshot else None,
                "typed": typed,
                "use_snapshot": use_snapshot,
                "status": status,
                "status_label": status_label,
                "badge_class": badge_class,
                "marks_awarded": r.marks_awarded,
                "time_spent": r.time_spent_seconds,
                "positive_marks": eq.positive_marks,
                "negative_marks": eq.negative_marks,
                "explanation": explanation,
            })

        context["reviewed_questions"] = reviewed_questions
        context["has_snapshot_reviews"] = any(item["use_snapshot"] for item in reviewed_questions)
        context["total_reviewed"] = len(reviewed_questions)
        context["correct_total"] = sum(1 for q in reviewed_questions if q["status"] == "correct")
        context["partial_total"] = sum(1 for q in reviewed_questions if q["status"] == "partial")
        context["incorrect_total"] = sum(1 for q in reviewed_questions if q["status"] == "incorrect")
        context["unattempted_total"] = sum(1 for q in reviewed_questions if q["status"] == "unattempted")
        context["ungraded_total"] = sum(1 for q in reviewed_questions if q["status"] == "ungraded")
        if attempt.classroom_session_id:
            from .classroom_results import classroom_result_summary
            context["classroom_summary"] = classroom_result_summary(attempt)
        elif attempt.has_ungraded_keys:
            from .pdf_key_results import partial_pdf_result_summary
            context["partial_summary"] = partial_pdf_result_summary(attempt)
        return context


class StudentResultPdfView(ActiveUserRequiredMixin, PrivateResultRedirectMixin, View):
    """
    Generates and downloads the official candidate scorecard PDF.
    Uses PyMuPDF (fitz) to create publication-quality vector reports.
    """

    def get(self, request, attempt_uuid):
        attempt = get_object_or_404(
            ExamAttempt.objects.select_related("exam", "student"),
            attempt_uuid=attempt_uuid,
        )
        user = request.user
        if attempt.exam.is_private_practice:
            if attempt.student_id != user.pk or attempt.exam.creator_id != user.pk:
                raise Http404()
            return redirect("questions:practice_result", attempt_uuid=attempt.attempt_uuid)

        # Access Control
        if not _can_access_attempt_result(user, attempt):
            raise PermissionDenied("You do not have permission to download this scorecard.")

        # A PDF must obey the same release setting as the web scorecard.
        visible = (attempt.classroom_session.student_results_visible if attempt.classroom_session_id
                   else attempt.exam.show_result_immediately)
        if user.is_student and not visible:
            raise PermissionDenied("Results have not been released for this examination.")

        _ensure_final_for_results(attempt)

        if attempt.classroom_session_id:
            from .classroom_report import generate_classroom_report
            from .classroom_results import classroom_result_summary
            pdf_bytes = generate_classroom_report(attempt, classroom_result_summary(attempt))
        elif attempt.has_ungraded_keys:
            from .classroom_report import generate_classroom_report
            from .pdf_key_results import partial_pdf_result_summary
            pdf_bytes = generate_classroom_report(attempt, partial_pdf_result_summary(attempt), title="Partial examination result")
        else:
            analytics = compute_student_attempt_analytics(attempt)
            pdf_bytes = generate_student_scorecard_pdf(attempt, analytics)

        filename = f"{attempt.student.username}_{attempt.exam.slug[:30]}_scorecard.pdf"
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        patch_cache_control(response, private=True, no_store=True)
        return response


class ExamLeaderboardView(ActiveUserRequiredMixin, DetailView):
    """
    Public / Candidate Leaderboard for an Examination.
    Displays ranked list of candidates with tie-breaking rules and percentile metrics.
    """

    model = Exam
    template_name = "exams/exam_leaderboard.html"
    context_object_name = "exam"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        queryset = super().get_queryset().filter(is_private_practice=False, is_classroom_only=False)
        # Do not reveal unpublished scores through the leaderboard.
        if self.request.user.is_student:
            queryset = queryset.filter(show_result_immediately=True)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        exam = self.object

        # Recalculate official ranks to ensure leaderboard accuracy
        latest_attempt = exam.attempts.filter(
            classroom_session__isnull=True, status__in=["submitted", "evaluated", "timed_out"]
        ).first()
        if latest_attempt:
            latest_attempt.recalculate_ranks()

        analytics = compute_exam_institutional_analytics(exam)
        context["leaderboard"] = analytics["leaderboard"]
        context["total_candidates"] = analytics["total_attempts"]
        context["highest_score"] = analytics["highest_score"]
        context["avg_score"] = analytics["avg_score"]
        context["pass_percentage"] = analytics["pass_percentage"]
        context["partial_attempts"] = analytics["partial_attempts"]
        context["ranked_attempts"] = analytics["ranked_attempts"]

        # If user is a student, find their rank in this exam
        if self.request.user.is_student:
            student_best = exam.attempts.filter(
                student=self.request.user,
                classroom_session__isnull=True,
                status__in=["submitted", "evaluated", "timed_out"],
            ).exclude(responses__key_confirmed_at_grading=False).order_by("rank").first()
            context["student_best_attempt"] = student_best

        return context


class TeacherExamAnalyticsView(TeacherRequiredMixin, DetailView):
    """
    Institutional Class & Examination Performance Dashboard for Teachers and Administrators.
    Displays cohort KPIs, score histograms, question discrimination/difficulty index,
    and student performance comparison tables.
    """

    model = Exam
    template_name = "exams/teacher_exam_analytics.html"
    context_object_name = "exam"

    def get_object(self, queryset=None):
        exam = super().get_object(queryset)
        if exam.is_private_practice or exam.is_classroom_only:
            raise Http404()
        user = self.request.user

        # Strict Teacher Category & Exam Ownership Scoping
        if not user.is_admin_role and exam.creator != user:
            if not user.assigned_categories.filter(pk=exam.category_id).exists():
                raise PermissionDenied("You do not have permission to view analytics for this examination.")

        return exam

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        exam = self.object

        analytics = compute_exam_institutional_analytics(exam)
        context["analytics"] = analytics

        # JSON datasets for interactive charts
        context["score_distribution_labels_json"] = json.dumps(list(analytics["score_distribution"].keys()))
        context["score_distribution_values_json"] = json.dumps(list(analytics["score_distribution"].values()))

        # Question difficulty distribution
        easy_q = sum(1 for q in analytics["question_stats"] if q["difficulty"] == "Easy")
        med_q = sum(1 for q in analytics["question_stats"] if q["difficulty"] == "Medium")
        hard_q = sum(1 for q in analytics["question_stats"] if q["difficulty"] == "Hard")

        context["question_diff_json"] = json.dumps({
            "Easy": easy_q,
            "Medium": med_q,
            "Hard": hard_q,
        })

        return context


class TeacherExportExamResultsCsvView(TeacherRequiredMixin, View):
    """
    Exports full candidate examination results as a CSV spreadsheet.
    Includes candidate ranks, percentage, accuracy, marks, and timestamps.
    """

    def get(self, request, pk):
        exam = get_object_or_404(Exam, pk=pk, is_private_practice=False, is_classroom_only=False)
        user = request.user

        if not user.is_admin_role and exam.creator != user:
            if not user.assigned_categories.filter(pk=exam.category_id).exists():
                raise PermissionDenied("You cannot export results for this examination.")

        # Recalculate ranks to ensure export accuracy
        sample_att = exam.attempts.filter(classroom_session__isnull=True, status__in=["submitted", "evaluated", "timed_out"]).first()
        if sample_att:
            sample_att.recalculate_ranks()

        attempts = (
            exam.attempts.filter(classroom_session__isnull=True, status__in=["submitted", "evaluated", "timed_out"])
            .select_related("student")
            .order_by("rank", "-score_obtained", "time_taken_seconds")
        )

        response = HttpResponse(content_type="text/csv")
        filename = f"{exam.slug[:30]}_results_{timezone.now().strftime('%Y%m%d')}.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            "Rank",
            "Candidate Name",
            "Username",
            "Email Address",
            "Target Exam",
            "Score Obtained",
            "Total Marks (graded marks if partial)",
            "Percentage",
            "Result Outcome",
            "Percentile",
            "Correct Answers",
            "Partial-credit Answers",
            "Incorrect Answers",
            "Unattempted",
            "Accuracy %",
            "Time Taken (Minutes)",
            "Completion Date",
            "Attempt Status",
        ])

        for att in attempts:
            writer.writerow([
                att.rank or "N/A",
                att.student.get_full_name() or att.student.username,
                att.student.username,
                att.student.email,
                att.student.target_exam or "N/A",
                att.score_obtained,
                (att.graded_possible_marks if att.graded_possible_marks is not None else "Unknown") if att.has_ungraded_keys else att.total_marks,
                ("Unknown (legacy key details)" if att.has_legacy_key_snapshots else f"{att.percentage}% (partial)") if att.has_ungraded_keys else f"{att.percentage}%",
                ("PARTIAL / UNRANKED" if att.has_ungraded_keys else
                 "PASSED (LEGACY RECORDED)" if att.has_legacy_key_snapshots and att.is_passed else
                 "FAILED (LEGACY RECORDED)" if att.has_legacy_key_snapshots else
                 "PASSED" if att.is_passed else "FAILED"),
                f"{att.percentile}%" if att.percentile else "N/A",
                att.correct_count,
                att.partial_count,
                att.incorrect_count,
                att.unattempted_count,
                f"{att.accuracy_percentage}%",
                round(att.time_taken_seconds / 60, 1),
                att.submitted_at.strftime("%Y-%m-%d %H:%M") if att.submitted_at else "N/A",
                att.get_status_display(),
            ])

        return response

