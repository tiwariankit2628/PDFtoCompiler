from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Avg, Count, Q
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.generic import TemplateView, View

from apps.accounts.models import User
from apps.core.models import AuditLog
from apps.core.permissions import (
    ActiveUserRequiredMixin,
    AdminRequiredMixin,
    StudentRequiredMixin,
    SuperAdminRequiredMixin,
    TeacherRequiredMixin,
)
from apps.exams.models import Category, Exam, ExamAttempt


class HomeView(TemplateView):
    """
    Commercial landing page showcasing real dynamic examination categories,
    CBT engine security features, and institutional scale.
    """

    template_name = "core/home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Dynamically load active categories from database
        context["categories"] = Category.objects.filter(is_active=True).order_by("display_order")
        context["total_categories"] = context["categories"].count()
        context["stats"] = [
            {"value": "99.98%", "label": "Engine Reliability"},
            {"value": "Zero", "label": "Client-Side Leakage"},
            {"value": "50K+", "label": "Concurrent Exam Capacity"},
            {"value": "Multi-Lingual", "label": "English & Hindi Support"},
        ]
        return context


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

class SuperAdminDashboardView(SuperAdminRequiredMixin, TemplateView):
    """
    Executive command center for Super Administrators with platform-wide
    analytics, user growth, exam attempts, and audit logs.
    """

    template_name = "core/dashboard_super_admin.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Core platform counters
        context["total_students"] = User.objects.filter(role=User.Roles.STUDENT).count()
        context["total_active_students"] = User.objects.filter(
            role=User.Roles.STUDENT, is_active=True
        ).count()
        context["total_teachers"] = User.objects.filter(role=User.Roles.TEACHER).count()
        context["total_active_teachers"] = User.objects.filter(
            role=User.Roles.TEACHER, is_active=True
        ).count()
        context["total_admins"] = User.objects.filter(role=User.Roles.ADMIN).count()
        context["total_super_admins"] = User.objects.filter(role=User.Roles.SUPER_ADMIN).count()
        context["total_categories"] = Category.objects.count()
        context["total_active_categories"] = Category.objects.filter(is_active=True).count()
        context["total_exams"] = Exam.objects.count()
        context["total_published_exams"] = Exam.objects.filter(
            status=Exam.ExamStatus.PUBLISHED
        ).count()
        context["total_attempts"] = ExamAttempt.objects.count()

        # Recent activities and registrations
        context["recent_registrations"] = User.objects.order_by("-date_joined")[:6]
        context["recent_activities"] = AuditLog.objects.select_related("user").order_by(
            "-created_at"
        )[:8]

        # Category distribution
        context["category_breakdown"] = (
            Category.objects.annotate(exam_count=Count("exams"))
            .filter(is_active=True)
            .order_by("-exam_count")[:6]
        )

        return context


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
        context["total_exams"] = Exam.objects.count()
        context["published_exams"] = Exam.objects.filter(
            status=Exam.ExamStatus.PUBLISHED
        ).count()
        context["draft_exams"] = Exam.objects.filter(status=Exam.ExamStatus.DRAFT).count()
        context["total_attempts"] = ExamAttempt.objects.count()

        # Recent records
        context["recent_students"] = User.objects.filter(role=User.Roles.STUDENT).order_by(
            "-date_joined"
        )[:5]
        context["recent_attempts"] = (
            ExamAttempt.objects.select_related("student", "exam")
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
        created_exams = Exam.objects.filter(creator=teacher)
        context["total_exams_created"] = created_exams.count()
        context["published_exams_count"] = created_exams.filter(
            status=Exam.ExamStatus.PUBLISHED
        ).count()
        context["draft_exams_count"] = created_exams.filter(
            status=Exam.ExamStatus.DRAFT
        ).count()

        # Attempts on teacher's examinations
        teacher_attempts = ExamAttempt.objects.filter(exam__creator=teacher)
        context["total_student_attempts"] = teacher_attempts.count()

        # Permitted categories
        context["assigned_categories"] = teacher.assigned_categories.filter(is_active=True)

        # Recent exam and student attempt activity
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

        # Real student attempt metrics from database
        student_attempts = ExamAttempt.objects.filter(student=student)
        context["total_attempts"] = student_attempts.count()
        context["passed_attempts"] = student_attempts.filter(is_passed=True).count()

        # Average accuracy percentage
        avg_res = student_attempts.aggregate(Avg("percentage"))["percentage__avg"]
        context["avg_percentage"] = round(avg_res, 1) if avg_res is not None else 0.0

        # Attempt history
        context["recent_attempts"] = (
            student_attempts.select_related("exam", "exam__category")
            .order_by("-started_at")[:5]
        )

        # Available exams with free vs premium labels
        context["available_exams"] = (
            Exam.objects.filter(status=Exam.ExamStatus.PUBLISHED)
            .select_related("category")
            .order_by("-created_at")[:6]
        )

        # Dynamic categories for quick navigation
        context["categories"] = Category.objects.filter(is_active=True).order_by("display_order")

        return context
