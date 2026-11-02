from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View
from apps.core.permissions import AdminRequiredMixin, SuperAdminRequiredMixin
from apps.core.utils import log_audit
from apps.exams.models import Exam, ExamAttempt
from .forms import (
    AdminUserCreateForm,
    AdminUserUpdateForm,
    StudentAdminCreateForm,
    StudentAdminUpdateForm,
    TeacherAdminCreateForm,
    TeacherAdminUpdateForm,
)
from .models import User


# ==============================================================================
# 1. STUDENT MANAGEMENT
# ==============================================================================

class StudentListView(AdminRequiredMixin, ListView):
    """Lists student accounts with search, active filter, and pagination."""

    model = User
    template_name = "accounts/management/student_list.html"
    context_object_name = "students"
    paginate_by = 10

    def get_queryset(self):
        qs = User.objects.filter(role=User.Roles.STUDENT).order_by("-date_joined")
        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(username__icontains=query)
                | Q(email__icontains=query)
                | Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(target_exam__icontains=query)
                | Q(institute_name__icontains=query)
            )

        status_filter = self.request.GET.get("status", "").strip()
        if status_filter == "active":
            qs = qs.filter(is_active=True)
        elif status_filter == "inactive":
            qs = qs.filter(is_active=False)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_query"] = self.request.GET.get("q", "")
        context["status_filter"] = self.request.GET.get("status", "")
        context["total_students_count"] = User.objects.filter(role=User.Roles.STUDENT).count()
        context["active_students_count"] = User.objects.filter(
            role=User.Roles.STUDENT, is_active=True
        ).count()
        return context


class StudentCreateView(AdminRequiredMixin, CreateView):
    """Enables administrators to provision a new student candidate account."""

    model = User
    form_class = StudentAdminCreateForm
    template_name = "accounts/management/student_form.html"
    success_url = reverse_lazy("accounts:student_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        log_audit(
            self.request.user,
            "STUDENT_CREATED",
            f"Created student account '{self.object.username}' ({self.object.email})",
            request=self.request,
        )
        messages.success(
            self.request, f"Student candidate '{self.object.username}' created successfully."
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["action_title"] = "Create New Student Account"
        return context


class StudentDetailView(AdminRequiredMixin, DetailView):
    """Displays comprehensive student profile, freemium status, and test attempts."""

    model = User
    template_name = "accounts/management/student_detail.html"
    context_object_name = "student"

    def get_queryset(self):
        return User.objects.filter(role=User.Roles.STUDENT)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        student = self.get_object()
        context["attempts"] = ExamAttempt.objects.filter(student=student).order_by("-started_at")
        context["total_attempts"] = context["attempts"].count()
        return context


class StudentUpdateView(AdminRequiredMixin, UpdateView):
    """Edits student candidate details."""

    model = User
    form_class = StudentAdminUpdateForm
    template_name = "accounts/management/student_form.html"
    success_url = reverse_lazy("accounts:student_list")

    def get_queryset(self):
        return User.objects.filter(role=User.Roles.STUDENT)

    def form_valid(self, form):
        response = super().form_valid(form)
        log_audit(
            self.request.user,
            "STUDENT_UPDATED",
            f"Updated student account '{self.object.username}'",
            request=self.request,
        )
        messages.success(
            self.request, f"Student candidate '{self.object.username}' updated successfully."
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["action_title"] = f"Edit Student: {self.object.username}"
        context["is_edit"] = True
        return context


class StudentToggleStatusView(AdminRequiredMixin, View):
    """Activates or deactivates a student candidate account."""

    def post(self, request, pk):
        student = get_object_or_404(User, pk=pk, role=User.Roles.STUDENT)
        student.is_active = not student.is_active
        student.save(update_fields=["is_active"])

        state = "activated" if student.is_active else "deactivated"
        log_audit(
            request.user,
            "STUDENT_STATUS_TOGGLED",
            f"Student account '{student.username}' was {state}.",
            request=request,
        )
        messages.success(request, f"Student '{student.username}' has been {state}.")
        return redirect("accounts:student_list")


# ==============================================================================
# 2. TEACHER MANAGEMENT
# ==============================================================================

class TeacherListView(AdminRequiredMixin, ListView):
    """Lists teacher / content manager accounts with category assignments."""

    model = User
    template_name = "accounts/management/teacher_list.html"
    context_object_name = "teachers"
    paginate_by = 10

    def get_queryset(self):
        qs = (
            User.objects.filter(role=User.Roles.TEACHER)
            .prefetch_related("assigned_categories")
            .annotate(exams_created_count=Count("created_exams"))
            .order_by("-date_joined")
        )
        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(username__icontains=query)
                | Q(email__icontains=query)
                | Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(institute_name__icontains=query)
            )

        status_filter = self.request.GET.get("status", "").strip()
        if status_filter == "active":
            qs = qs.filter(is_active=True)
        elif status_filter == "inactive":
            qs = qs.filter(is_active=False)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_query"] = self.request.GET.get("q", "")
        context["status_filter"] = self.request.GET.get("status", "")
        context["total_teachers_count"] = User.objects.filter(role=User.Roles.TEACHER).count()
        context["active_teachers_count"] = User.objects.filter(
            role=User.Roles.TEACHER, is_active=True
        ).count()
        return context


class TeacherCreateView(AdminRequiredMixin, CreateView):
    """Provisions a new Teacher / Content Manager with category permissions."""

    model = User
    form_class = TeacherAdminCreateForm
    template_name = "accounts/management/teacher_form.html"
    success_url = reverse_lazy("accounts:teacher_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        log_audit(
            self.request.user,
            "TEACHER_CREATED",
            f"Created teacher account '{self.object.username}' with permitted categories.",
            request=self.request,
        )
        messages.success(
            self.request, f"Teacher account '{self.object.username}' created successfully."
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["action_title"] = "Add New Teacher / Content Manager"
        return context


class TeacherDetailView(AdminRequiredMixin, DetailView):
    """Displays teacher profile, assigned categories, and examinations created."""

    model = User
    template_name = "accounts/management/teacher_detail.html"
    context_object_name = "teacher"

    def get_queryset(self):
        return User.objects.filter(role=User.Roles.TEACHER).prefetch_related(
            "assigned_categories"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        teacher = self.get_object()
        context["created_exams"] = Exam.objects.filter(creator=teacher).order_by("-created_at")
        context["total_exams"] = context["created_exams"].count()
        context["total_student_attempts"] = ExamAttempt.objects.filter(
            exam__creator=teacher
        ).count()
        return context


class TeacherUpdateView(AdminRequiredMixin, UpdateView):
    """Edits teacher profile and updates permitted categories."""

    model = User
    form_class = TeacherAdminUpdateForm
    template_name = "accounts/management/teacher_form.html"
    success_url = reverse_lazy("accounts:teacher_list")

    def get_queryset(self):
        return User.objects.filter(role=User.Roles.TEACHER)

    def form_valid(self, form):
        response = super().form_valid(form)
        log_audit(
            self.request.user,
            "TEACHER_UPDATED",
            f"Updated teacher account '{self.object.username}' and category permissions.",
            request=self.request,
        )
        messages.success(
            self.request, f"Teacher '{self.object.username}' updated successfully."
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["action_title"] = f"Edit Teacher: {self.object.username}"
        context["is_edit"] = True
        return context


class TeacherToggleStatusView(AdminRequiredMixin, View):
    """Toggles active/inactive status of a teacher."""

    def post(self, request, pk):
        teacher = get_object_or_404(User, pk=pk, role=User.Roles.TEACHER)
        teacher.is_active = not teacher.is_active
        teacher.save(update_fields=["is_active"])

        state = "activated" if teacher.is_active else "deactivated"
        log_audit(
            request.user,
            "TEACHER_STATUS_TOGGLED",
            f"Teacher account '{teacher.username}' was {state}.",
            request=request,
        )
        messages.success(request, f"Teacher '{teacher.username}' has been {state}.")
        return redirect("accounts:teacher_list")


# ==============================================================================
# 3. ADMINISTRATOR MANAGEMENT (SUPER ADMIN ONLY)
# ==============================================================================

class AdminUserListView(SuperAdminRequiredMixin, ListView):
    """Lists administrators (Super Admin restricted)."""

    model = User
    template_name = "accounts/management/admin_list.html"
    context_object_name = "admin_users"
    paginate_by = 10

    def get_queryset(self):
        return (
            User.objects.filter(role__in=[User.Roles.ADMIN, User.Roles.SUPER_ADMIN])
            .order_by("-date_joined")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["total_admins_count"] = self.get_queryset().count()
        return context


class AdminUserCreateView(SuperAdminRequiredMixin, CreateView):
    """Super Admin creates a new Administrator."""

    model = User
    form_class = AdminUserCreateForm
    template_name = "accounts/management/admin_form.html"
    success_url = reverse_lazy("accounts:admin_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        log_audit(
            self.request.user,
            "ADMIN_CREATED",
            f"Super Admin created Administrator account '{self.object.username}'",
            request=self.request,
        )
        messages.success(
            self.request, f"Administrator account '{self.object.username}' created successfully."
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["action_title"] = "Create New Administrator"
        return context


class AdminUserUpdateView(SuperAdminRequiredMixin, UpdateView):
    """Super Admin edits an existing Administrator."""

    model = User
    form_class = AdminUserUpdateForm
    template_name = "accounts/management/admin_form.html"
    success_url = reverse_lazy("accounts:admin_list")

    def get_queryset(self):
        return User.objects.filter(role=User.Roles.ADMIN)

    def form_valid(self, form):
        response = super().form_valid(form)
        log_audit(
            self.request.user,
            "ADMIN_UPDATED",
            f"Super Admin updated Administrator account '{self.object.username}'",
            request=self.request,
        )
        messages.success(
            self.request, f"Administrator '{self.object.username}' updated successfully."
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["action_title"] = f"Edit Administrator: {self.object.username}"
        context["is_edit"] = True
        return context


class AdminUserToggleStatusView(SuperAdminRequiredMixin, View):
    """Super Admin toggles active status of an Administrator."""

    def post(self, request, pk):
        admin_user = get_object_or_404(User, pk=pk)

        # Safeguard: cannot deactivate oneself or a super admin
        if admin_user == request.user:
            messages.error(request, "You cannot deactivate your own account.")
            return redirect("accounts:admin_list")

        if admin_user.is_super_admin:
            messages.error(request, "Super Administrator accounts cannot be deactivated here.")
            return redirect("accounts:admin_list")

        admin_user.is_active = not admin_user.is_active
        admin_user.save(update_fields=["is_active"])

        state = "activated" if admin_user.is_active else "deactivated"
        log_audit(
            request.user,
            "ADMIN_STATUS_TOGGLED",
            f"Administrator account '{admin_user.username}' was {state}.",
            request=request,
        )
        messages.success(request, f"Administrator '{admin_user.username}' has been {state}.")
        return redirect("accounts:admin_list")


class AdminUserDeleteView(SuperAdminRequiredMixin, View):
    """Super Admin deletes an Administrator account."""

    def post(self, request, pk):
        admin_user = get_object_or_404(User, pk=pk)

        if admin_user == request.user:
            messages.error(request, "You cannot delete your own account.")
            return redirect("accounts:admin_list")

        if admin_user.is_super_admin:
            messages.error(request, "Super Administrator accounts cannot be deleted.")
            return redirect("accounts:admin_list")

        username = admin_user.username
        admin_user.delete()
        log_audit(
            request.user,
            "ADMIN_DELETED",
            f"Super Admin deleted Administrator account '{username}'",
            request=request,
        )
        messages.success(request, f"Administrator '{username}' has been deleted successfully.")
        return redirect("accounts:admin_list")

