import logging

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, connection, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect


logger = logging.getLogger(__name__)


def lock_active_teacher(user):
    """Recheck an author on the account row used by deletion and email edits.

    The caller must hold an atomic transaction and acquire this before any
    exam/question locks. An authenticated request can otherwise outlive an
    account deletion, role change, or email change.
    """
    from apps.payments.services import lock_practice_user

    user = lock_practice_user(user)
    if not user.is_active or not (user.is_teacher or user.is_admin_role):
        raise PermissionDenied("An active teacher or administrator is required.")
    return user


def discard_rolled_back_uploads(fields):
    """Delete only new files written by an operation whose transaction failed."""
    for field in fields:
        if field and getattr(field, "_committed", False):
            try:
                field.storage.delete(field.name)
            except Exception:
                # Keep the original failure, and never log a private file path.
                logger.warning("Could not remove a rolled-back author upload.")


class FreshTeacherWriteMixin:
    """Place after TeacherRequiredMixin and before exam/question lock mixins.

    Hold a fresh account lock while authoring records or publishing. This mixin
    is not for long-running PDF extraction: uploads commit the source first and
    run extraction outside the lock. Only explicitly tracked new files are
    removed if this transaction rolls back.
    """

    def track_new_upload(self, field):
        if field and not getattr(field, "_committed", True):
            self._rollback_uploads.append(field)

    def dispatch(self, request, *args, **kwargs):
        if request.method != "POST":
            return super().dispatch(request, *args, **kwargs)
        from apps.accounts.models import User

        self._rollback_uploads = []
        try:
            with transaction.atomic():
                user = lock_active_teacher(request.user)
                if user.needs_email_verification:
                    return redirect("accounts:verification_pending")
                request.user = user
                return super().dispatch(request, *args, **kwargs)
        except Exception as exc:
            discard_rolled_back_uploads(self._rollback_uploads)
            if isinstance(exc, User.DoesNotExist):
                raise Http404() from None
            if isinstance(exc, OperationalError) and connection.vendor == "sqlite" and "locked" in str(exc).lower():
                response = HttpResponse("The account is busy. Please retry your changes.", status=503)
                response["Retry-After"] = "1"
                return response
            raise


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

