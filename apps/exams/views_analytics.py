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
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, ListView

from apps.core.permissions import ActiveUserRequiredMixin, TeacherRequiredMixin
from apps.core.utils import log_audit
from .analytics import compute_student_attempt_analytics, compute_exam_institutional_analytics
from .models import Exam, ExamAttempt, ExamResponse
from .pdf_generator import generate_student_scorecard_pdf


class StudentResultDetailView(ActiveUserRequiredMixin, DetailView):
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

    def get_object(self, queryset=None):
        attempt = super().get_object(queryset)
        user = self.request.user

        # Access Control: Only the candidate or authorized teachers/admins can view this result
        if attempt.student != user and not user.is_admin_role:
            if not (user.is_teacher and (attempt.exam.creator == user or user.assigned_categories.filter(pk=attempt.exam.category_id).exists())):
                raise PermissionDenied("You do not have permission to view this student's result.")

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
        can_view_scores = bool(exam.show_result_immediately or user.is_admin_role or user.is_teacher)
        context["can_view_scores"] = can_view_scores
        context["exam"] = exam

        if can_view_scores:
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
                "Incorrect": analytics["incorrect_count"],
                "Unattempted": analytics["unattempted_count"],
            })

        # Previous attempts by this student on this examination
        context["other_attempts"] = (
            ExamAttempt.objects.filter(exam=exam, student=attempt.student)
            .exclude(id=attempt.id)
            .order_by("-started_at")
        )

        return context


class StudentResultReviewView(ActiveUserRequiredMixin, DetailView):
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

        if attempt.student != user and not user.is_admin_role:
            if not (user.is_teacher and (attempt.exam.creator == user or user.assigned_categories.filter(pk=attempt.exam.category_id).exists())):
                raise PermissionDenied("You do not have permission to review these answers.")

        return attempt

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        attempt = self.object
        exam = attempt.exam
        user = self.request.user

        # Security & Policy Verification
        can_review = bool(
            (exam.allow_review_answers and (exam.show_result_immediately or exam.is_expired))
            or user.is_admin_role
            or user.is_teacher
        )
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
            .prefetch_related("exam_question__question__options")
            .order_by("order")
        )

        # Prepare detailed question cards
        reviewed_questions = []
        for r in responses:
            eq = r.exam_question
            q = eq.question
            options = list(q.options.all().order_by("option_key"))

            correct_option = next((opt for opt in options if opt.is_correct), None)

            if r.selected_option is None:
                status = "unattempted"
                status_label = "Unattempted"
                badge_class = "secondary"
            elif r.selected_option.is_correct:
                status = "correct"
                status_label = "Correct"
                badge_class = "success"
            else:
                status = "incorrect"
                status_label = "Incorrect"
                badge_class = "danger"

            reviewed_questions.append({
                "response": r,
                "order": r.order,
                "section": eq.section,
                "question": q,
                "options": options,
                "selected_option": r.selected_option,
                "correct_option": correct_option,
                "status": status,
                "status_label": status_label,
                "badge_class": badge_class,
                "marks_awarded": r.marks_awarded,
                "time_spent": r.time_spent_seconds,
                "positive_marks": eq.positive_marks,
                "negative_marks": eq.negative_marks,
                "explanation": q.explanation,
            })

        context["reviewed_questions"] = reviewed_questions
        context["total_reviewed"] = len(reviewed_questions)
        context["correct_total"] = sum(1 for q in reviewed_questions if q["status"] == "correct")
        context["incorrect_total"] = sum(1 for q in reviewed_questions if q["status"] == "incorrect")
        context["unattempted_total"] = sum(1 for q in reviewed_questions if q["status"] == "unattempted")
        return context


class StudentResultPdfView(ActiveUserRequiredMixin, View):
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

        # Access Control
        if attempt.student != user and not user.is_admin_role:
            if not (user.is_teacher and (attempt.exam.creator == user or user.assigned_categories.filter(pk=attempt.exam.category_id).exists())):
                raise PermissionDenied("You do not have permission to download this scorecard.")

        # Ensure evaluated
        if attempt.status == ExamAttempt.AttemptStatus.IN_PROGRESS and attempt.is_expired:
            attempt.auto_timeout_if_expired()

        analytics = compute_student_attempt_analytics(attempt)
        pdf_bytes = generate_student_scorecard_pdf(attempt, analytics)

        filename = f"{attempt.student.username}_{attempt.exam.slug[:30]}_scorecard.pdf"
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        exam = self.object

        # Recalculate official ranks to ensure leaderboard accuracy
        latest_attempt = exam.attempts.filter(
            status__in=["submitted", "evaluated", "timed_out"]
        ).first()
        if latest_attempt:
            latest_attempt.recalculate_ranks()

        analytics = compute_exam_institutional_analytics(exam)
        context["leaderboard"] = analytics["leaderboard"]
        context["total_candidates"] = analytics["total_attempts"]
        context["highest_score"] = analytics["highest_score"]
        context["avg_score"] = analytics["avg_score"]
        context["pass_percentage"] = analytics["pass_percentage"]

        # If user is a student, find their rank in this exam
        if self.request.user.is_student:
            student_best = exam.attempts.filter(
                student=self.request.user,
                status__in=["submitted", "evaluated", "timed_out"],
            ).order_by("rank").first()
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
        exam = get_object_or_404(Exam, pk=pk)
        user = request.user

        if not user.is_admin_role and exam.creator != user:
            if not user.assigned_categories.filter(pk=exam.category_id).exists():
                raise PermissionDenied("You cannot export results for this examination.")

        # Recalculate ranks to ensure export accuracy
        sample_att = exam.attempts.filter(status__in=["submitted", "evaluated", "timed_out"]).first()
        if sample_att:
            sample_att.recalculate_ranks()

        attempts = (
            exam.attempts.filter(status__in=["submitted", "evaluated", "timed_out"])
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
            "Total Marks",
            "Percentage",
            "Result Outcome",
            "Percentile",
            "Correct Answers",
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
                att.total_marks,
                f"{att.percentage}%",
                "PASSED" if att.is_passed else "FAILED",
                f"{att.percentile}%" if att.percentile else "N/A",
                att.correct_count,
                att.incorrect_count,
                att.unattempted_count,
                f"{att.accuracy_percentage}%",
                round(att.time_taken_seconds / 60, 1),
                att.submitted_at.strftime("%Y-%m-%d %H:%M") if att.submitted_at else "N/A",
                att.get_status_display(),
            ])

        return response

