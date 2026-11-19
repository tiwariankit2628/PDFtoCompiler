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
from django.http import Http404, HttpResponse, HttpResponseNotAllowed
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
            user = form.save(commit=False)
            user.verification_required = True
            user.is_verified = False
            user.save()
            login(request, user)
            from .verification import send_verification, status_message
            status_message(request, send_verification(user))
            return redirect("accounts:verification_pending")
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
                original_email = user.email
                form = UserProfileForm(request.POST, request.FILES, instance=user)
                if form.is_valid():
                    user = form.save(commit=False)
                    from .verification import invalidate_changed_email
                    email_changed = invalidate_changed_email(user, original_email)
                    user.save()
                    form.save_m2m()
                    messages.success(request, "Your profile has been updated successfully.")
                    if email_changed:
                        # The new address must be verified; old signed links no
                        # longer match. Active tests remain accessible.
                        from .verification import send_verification, status_message
                        transaction.on_commit(lambda: status_message(request, send_verification(user)))
                        return redirect("accounts:verification_pending")
                    return redirect("accounts:profile")
        except User.DoesNotExist:
            raise Http404() from None
        except OperationalError as exc:
            if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                raise
            response = HttpResponse("Your profile is busy. Please retry; any test timer continues.", status=503)
            response["Retry-After"] = "1"
            return response
    else:
        form = UserProfileForm(instance=request.user)

    return render(request, "accounts/profile.html", {"form": form, "user": request.user})


@login_required
def verification_pending(request):
    if request.user.is_verified or not request.user.verification_required:
        return redirect("core:dashboard_router")
    from apps.exams.models import ExamAttempt
    active = ExamAttempt.objects.filter(student=request.user, status=ExamAttempt.AttemptStatus.IN_PROGRESS).select_related("exam")
    return render(request, "accounts/verification_pending.html", {"active_attempts": active})


@login_required
def verification_resend(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    from .verification import send_verification, status_message
    status_message(request, send_verification(request.user))
    return redirect("accounts:verification_pending")


def verify_email_view(request, token):
    from .verification import token_user
    from django.utils.cache import patch_cache_control
    user = token_user(token)
    if request.method == "GET":
        response = render(request, "accounts/verification_confirm.html", {"valid": bool(user), "token": token})
        patch_cache_control(response, private=True, no_store=True)
        # Do not disclose a signed link to third-party resources, while still
        # allowing the browser's same-origin CSRF-protected confirmation POST.
        # `no-referrer` can make browsers send Origin: null and break Django CSRF.
        response["Referrer-Policy"] = "same-origin"
        return response
    if request.method != "POST":
        return HttpResponseNotAllowed(["GET", "POST"])
    if not user:
        messages.error(request, "This verification link is invalid, expired or has already been used. Request a new link.")
        return redirect("accounts:verification_pending" if request.user.is_authenticated else "accounts:login")
    import uuid
    from apps.payments.services import lock_practice_user
    try:
        with transaction.atomic():
            locked = lock_practice_user(user)
            # Check again after locking: a resend or email change invalidates this link.
            verified_user = token_user(token)
            if not verified_user:
                messages.error(request, "This verification link is no longer valid. Request a new link.")
            else:
                locked.is_verified = True
                locked.verification_token = uuid.uuid4()
                locked.save(update_fields=["is_verified", "verification_token"])
                messages.success(request, "Your email address is verified. You may sign in and start practising.")
    except User.DoesNotExist:
        messages.error(request, "This verification link is no longer valid.")
        return redirect("accounts:login")
    except OperationalError as exc:
        if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
            raise
        response = HttpResponse("Verification is busy. Please retry this link.", status=503)
        response["Retry-After"] = "1"
        return response
    if request.user.is_authenticated and request.user.pk == user.pk:
        return redirect("core:dashboard_router")
    return redirect("accounts:login")


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

