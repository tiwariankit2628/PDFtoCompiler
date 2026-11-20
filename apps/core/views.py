from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Avg, Count, Q
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.generic import TemplateView, View

from apps.accounts.models import User
from apps.core.models import AuditLog
from apps.core.exam_targets import student_exam_queryset
from apps.core.permissions import (
    ActiveUserRequiredMixin,
    AdminRequiredMixin,
    StudentRequiredMixin,
    SuperAdminRequiredMixin,
    TeacherRequiredMixin,
)
from apps.exams.models import Category, Exam, ExamAttempt


class HomeView(TemplateView):
    """Public landing page; examples are illustrative, not platform metrics."""

    template_name = "core/home.html"


class AboutView(TemplateView):
    """Platform overview, security architecture, and institutional information."""

    template_name = "core/about.html"


class ContactView(TemplateView):
    """Institutional sales, support, and inquiry portal."""

    template_name = "core/contact.html"


class DashboardRouterView(ActiveUserRequiredMixin, View):
    """
    Intelligently routes the authenticated active user to their role-specific dashboard:
    - Super Admin -> /dashboard/super-admin/
    - Admin -> /dashboard/admin/
    - Teacher / Content Manager -> /dashboard/teacher/
    - Student -> /dashboard/student/
    """

    def get(self, request):
        user = request.user
        if user.is_super_admin:
            return redirect("core:super_admin_dashboard")
        elif user.is_admin_user:
            return redirect("core:admin_dashboard")
        elif user.is_teacher:
            return redirect("core:teacher_dashboard")
        else:
            return redirect("core:student_dashboard")


# ==============================================================================
# 1. SUPER ADMIN DASHBOARD
# ==============================================================================

class SuperAdminDashboardView(SuperAdminRequiredMixin, View):
    """Replace the old overview without promoting existing administrators.

    Explicitly enrolled founders are sent to their separately protected page;
    other super-administrators retain the ordinary operations dashboard and
    all of their existing role permissions.
    """

    def get(self, request):
        from .models import FounderAccess
        if (request.user.role == User.Roles.SUPER_ADMIN
                and FounderAccess.objects.filter(user=request.user, is_enabled=True).exists()):
            return redirect("founder:overview")
        return AdminDashboardView.as_view()(request)


# ==============================================================================
# 2. ADMIN DASHBOARD
# ==============================================================================

class AdminDashboardView(AdminRequiredMixin, TemplateView):
    """
    Administrative operations dashboard for managing students, teachers,
    categories, and monitoring examinations.
    """

    template_name = "core/dashboard_admin.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Metrics
        context["total_students"] = User.objects.filter(role=User.Roles.STUDENT).count()
        context["active_students"] = User.objects.filter(
            role=User.Roles.STUDENT, is_active=True
        ).count()
        context["total_teachers"] = User.objects.filter(role=User.Roles.TEACHER).count()
        context["active_teachers"] = User.objects.filter(
            role=User.Roles.TEACHER, is_active=True
        ).count()
        context["total_categories"] = Category.objects.count()
        context["total_exams"] = Exam.objects.filter(is_private_practice=False, is_classroom_only=False).count()
        context["published_exams"] = Exam.objects.filter(
            status=Exam.ExamStatus.PUBLISHED, is_private_practice=False, is_classroom_only=False
        ).count()
        context["draft_exams"] = Exam.objects.filter(status=Exam.ExamStatus.DRAFT, is_private_practice=False, is_classroom_only=False).count()
        context["total_attempts"] = ExamAttempt.objects.filter(exam__is_private_practice=False, classroom_session__isnull=True).count()

        # Recent records
        context["recent_students"] = User.objects.filter(role=User.Roles.STUDENT).order_by(
            "-date_joined"
        )[:5]
        context["recent_attempts"] = (
            ExamAttempt.objects.filter(exam__is_private_practice=False, classroom_session__isnull=True).select_related("student", "exam")
            .order_by("-started_at")[:5]
        )

        return context


# ==============================================================================
# 3. TEACHER DASHBOARD
# ==============================================================================

class TeacherDashboardView(TeacherRequiredMixin, TemplateView):
    """
    Content manager workspace scoped strictly to exams and categories
    assigned or created by the logged-in teacher.
    """

    template_name = "core/dashboard_teacher.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        teacher = self.request.user

        # Scoped strictly to teacher's exams
        created_exams = Exam.objects.filter(creator=teacher, is_private_practice=False, is_classroom_only=False,
                                            series_slot__isnull=True)
        context["total_exams_created"] = created_exams.count()
        context["published_exams_count"] = created_exams.filter(
            status=Exam.ExamStatus.PUBLISHED, is_private_practice=False
        ).count()
        context["draft_exams_count"] = created_exams.filter(
            status=Exam.ExamStatus.DRAFT
        ).count()

        # Attempts on teacher's examinations
        teacher_attempts = ExamAttempt.objects.filter(exam__creator=teacher, exam__is_private_practice=False,
                                                      exam__series_slot__isnull=True, classroom_session__isnull=True)
        context["total_student_attempts"] = teacher_attempts.count()

        # Permitted categories
        context["assigned_categories"] = teacher.assigned_categories.filter(is_active=True)

        # Recent exam and student attempt activity
        from apps.payments.packs import balances
        context["credit_balances"] = balances(teacher)
        context["recent_created_exams"] = created_exams.order_by("-created_at")[:5]
        context["recent_attempts"] = (
            teacher_attempts.select_related("student", "exam")
            .order_by("-started_at")[:5]
        )

        return context


# ==============================================================================
# 4. STUDENT DASHBOARD
# ==============================================================================

class StudentDashboardView(StudentRequiredMixin, TemplateView):
    """
    Candidate examination workspace with actual database metrics,
    available tests, attempt history, and performance stats.
    """

    template_name = "core/dashboard_student.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        student = self.request.user

        # Institutional metrics exclude private practice. Do not leak scores or
        # pass/fail outcomes from exams whose results have not been released.
        all_exam_attempts = ExamAttempt.objects.filter(student=student, exam__is_private_practice=False)
        # Series results are separate from institutional pass/fail metrics;
        # series snapshots have no institution-defined passing threshold.
        student_attempts = all_exam_attempts.filter(classroom_session__isnull=True, exam__series_slot__isnull=True)
        visible_results = student_attempts.filter(
            status__in=ExamAttempt.FINAL_STATUSES, exam__show_result_immediately=True,
        ).exclude(responses__key_confirmed_at_grading=False)
        context["total_attempts"] = student_attempts.count()
        context["passed_attempts"] = visible_results.filter(is_passed=True).count()
        context["released_result_count"] = visible_results.count()
        avg_res = visible_results.aggregate(Avg("percentage"))["percentage__avg"]
        context["avg_percentage"] = round(avg_res, 1) if avg_res is not None else None

        # Attempt history
        context["recent_test_count"] = all_exam_attempts.count()
        context["recent_attempts"] = (
            all_exam_attempts.select_related("exam", "exam__category", "exam__series_slot", "classroom_session")
            .order_by("-started_at")[:5]
        )

        # Available exams with free vs premium labels
        available = student_exam_queryset(
            Exam.objects.filter(status=Exam.ExamStatus.PUBLISHED, is_private_practice=False, is_classroom_only=False,
                                series_slot__isnull=True, classroom_sessions__isnull=True), student
        )
        context["available_exams"] = available.select_related("category").order_by("-created_at")[:6]

        # Private practice is a separate workspace, not part of institutional metrics.
        from apps.payments.packs import balances
        context["credit_balances"] = balances(student)
        context["pdf_balance"] = next((row for row in context["credit_balances"] if row["kind"] == "pdf"), None)
        context["classroom_balance"] = next((row for row in context["credit_balances"] if row["kind"] == "classroom"), None)
        context["recent_practice_attempts"] = (
            ExamAttempt.objects.filter(student=student, exam__creator=student, exam__is_private_practice=True)
            .select_related("exam").order_by("-started_at")[:5]
        )
        context["practice_count"] = ExamAttempt.objects.filter(student=student, exam__creator=student, exam__is_private_practice=True).count()

        # Dynamic categories for quick navigation
        context["categories"] = Category.objects.filter(is_active=True, pk__in=available.values("category_id")).order_by("display_order")

        return context
