from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import (
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.db import OperationalError, connection, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import View

from .forms import PublicRegistrationForm, StudentRegistrationForm, UserLoginForm, UserProfileForm
from .models import User


class UserRegisterView(View):
    """
    Handles new student self-registration with instant onboarding.
    """

    template_name = "accounts/register.html"

    def get(self, request):
        if request.user.is_authenticated:
            return redirect("core:dashboard_router")
        # A public landing-page link may preselect a role, but only the two
        # public choices are accepted. The POST form remains authoritative.
        requested_role = request.GET.get("role", "").strip().upper()
        role = requested_role if requested_role in (User.Roles.STUDENT, User.Roles.TEACHER) else User.Roles.STUDENT
        form = PublicRegistrationForm(initial={"role": role})
        return render(request, self.template_name, {"form": form})

    def post(self, request):
        if request.user.is_authenticated:
            return redirect("core:dashboard_router")
        form = PublicRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            if user.is_teacher:
                messages.success(
                    request,
                    f"Welcome, {user.first_name or user.username}! Your teacher account is ready. "
                    "An administrator still needs to assign you subject categories before you can "
                    "publish papers — your dashboard explains the next steps.",
                )
            else:
                messages.success(
                    request,
                    f"Welcome to the platform, {user.first_name or user.username}! "
                    "Your student account has been created successfully.",
                )
            login(request, user)
            return redirect("core:dashboard_router")
        return render(request, self.template_name, {"form": form})


class UserLoginView(View):
    """
    Handles authentication with username or email.
    """

    template_name = "accounts/login.html"

    def get(self, request):
        if request.user.is_authenticated:
            return redirect("core:dashboard_router")
        form = UserLoginForm()
        return render(request, self.template_name, {"form": form})

    def post(self, request):
        if request.user.is_authenticated:
            return redirect("core:dashboard_router")
        form = UserLoginForm(request=request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(request, f"Welcome back, {user.get_full_name() or user.username}!")
            next_url = request.GET.get("next")
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                return redirect(next_url)
            return redirect("core:dashboard_router")
        return render(request, self.template_name, {"form": form})


class UserLogoutView(View):
    """
    Logs out the user and redirects to home page with feedback.
    """

    def get(self, request):
        logout(request)
        messages.info(request, "You have been logged out successfully.")
        return redirect("core:home")

    def post(self, request):
        logout(request)
        messages.info(request, "You have been logged out successfully.")
        return redirect("core:home")


@login_required
def profile_view(request):
    """
    Allows user to view and update personal profile information.
    """
    if request.method == "POST":
        from apps.payments.services import lock_practice_user
        try:
            with transaction.atomic():
                # Starts and profile changes take the same lock before checking
                # the active attempts, so neither can race the other.
                user = lock_practice_user(request.user)
                form = UserProfileForm(request.POST, request.FILES, instance=user)
                if form.is_valid():
                    form.save()
                    messages.success(request, "Your profile has been updated successfully.")
                    return redirect("accounts:profile")
        except OperationalError as exc:
            if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                raise
            response = HttpResponse("Your profile is busy. Please retry; any test timer continues.", status=503)
            response["Retry-After"] = "1"
            return response
    else:
        form = UserProfileForm(instance=request.user)

    return render(request, "accounts/profile.html", {"form": form, "user": request.user})


def verify_email_view(request, token):
    """
    Validates email verification token.
    """
    user = get_object_or_404(User, verification_token=token)
    if not user.is_verified:
        user.is_verified = True
        user.save(update_fields=["is_verified"])
        messages.success(request, "Your email address has been verified successfully.")
    else:
        messages.info(request, "Your email is already verified.")
    return redirect("core:dashboard_router")


# Password Reset Views with custom templates
class CustomPasswordResetView(PasswordResetView):
    template_name = "accounts/password_reset.html"
    email_template_name = "accounts/emails/password_reset_email.html"
    subject_template_name = "accounts/emails/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")


class CustomPasswordResetDoneView(PasswordResetDoneView):
    template_name = "accounts/password_reset_done.html"


class CustomPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "accounts/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:password_reset_complete")


class CustomPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = "accounts/password_reset_complete.html"

