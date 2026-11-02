from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


class ActiveUserRequiredMixin(LoginRequiredMixin):
    """
    Ensures user is logged in and active. If account is deactivated,
    forces logout and redirects with error message.
    """

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        if not request.user.is_active:
            logout(request)
            messages.error(
                request,
                "Your account has been deactivated. Please contact an administrator.",
            )
            return redirect("accounts:login")

        return super().dispatch(request, *args, **kwargs)


class SuperAdminRequiredMixin(ActiveUserRequiredMixin, UserPassesTestMixin):
    """
    Allows access strictly to Super Administrators.
    Raises HTTP 403 PermissionDenied if unauthorized.
    """

    def test_func(self):
        user = self.request.user
        return bool(user and user.is_authenticated and user.is_active and user.is_super_admin)

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied("You do not have Super Administrator privileges.")


class AdminRequiredMixin(ActiveUserRequiredMixin, UserPassesTestMixin):
    """
    Allows access to Administrators and Super Administrators.
    Raises HTTP 403 PermissionDenied if unauthorized.
    """

    def test_func(self):
        user = self.request.user
        return bool(user and user.is_authenticated and user.is_active and user.is_admin_role)

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied("You do not have Administrator privileges.")


class TeacherRequiredMixin(ActiveUserRequiredMixin, UserPassesTestMixin):
    """
    Allows access to Teachers / Content Managers and Administrators.
    """

    def test_func(self):
        user = self.request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and (user.is_teacher or user.is_admin_role)
        )

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied("You do not have Teacher / Content Manager privileges.")


class StudentRequiredMixin(ActiveUserRequiredMixin, UserPassesTestMixin):
    """
    Allows access to Students.
    """

    def test_func(self):
        user = self.request.user
        return bool(user and user.is_authenticated and user.is_active and user.is_student)

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied("Access is restricted to student candidate accounts.")

