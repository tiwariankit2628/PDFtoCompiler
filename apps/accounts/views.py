from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import (
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import View

from .forms import StudentRegistrationForm, UserLoginForm, UserProfileForm
from .models import User


class UserRegisterView(View):
    """
    Handles new student self-registration with instant onboarding.
    """

    template_name = "accounts/register.html"

    def get(self, request):
        if request.user.is_authenticated:
            return redirect("core:dashboard_router")
        form = StudentRegistrationForm()
        return render(request, self.template_name, {"form": form})

    def post(self, request):
        if request.user.is_authenticated:
            return redirect("core:dashboard_router")
        form = StudentRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(
                request,
                f"Welcome to the platform, {user.first_name or user.username}! Your student account has been created successfully.",
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
        form = UserProfileForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Your profile has been updated successfully.")
            return redirect("accounts:profile")
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

