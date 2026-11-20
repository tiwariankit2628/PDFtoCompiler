"""Google Sign-In and explicit linking, without altering existing account records."""
import time
import uuid
from functools import wraps

from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, OperationalError, transaction
from django.shortcuts import redirect, render
from django.utils.cache import patch_cache_control
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.payments.services import lock_practice_user
from .forms import GoogleSetPasswordForm, GoogleSignupForm
from .google_signin import (
    SIGNUP_LIFETIME_SECONDS, SIGNUP_SESSION_KEY, GoogleSignInError,
    configuration, consume_authorization_state, create_authorization_url,
    exchange_code, same_configured_origin,
)
from .models import GoogleIdentity, User
from .verification import send_verification, status_message


def private_flow(*, referrer_policy="no-referrer"):
    """Keep OAuth responses out of caches and callback data off third parties."""
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            response = view(request, *args, **kwargs)
            patch_cache_control(response, private=True, no_store=True, no_cache=True, max_age=0, must_revalidate=True)
            response["Referrer-Policy"] = referrer_policy
            response["X-Robots-Tag"] = "noindex, nofollow"
            return response
        return wrapped
    return decorator


def _eligible(user):
    # Google may sign in only an explicitly linked public student/teacher.
    # No role, staff flag, admin permissions, track or category is granted.
    return bool(user.is_active and not user.is_superuser and
                user.role in (User.Roles.STUDENT, User.Roles.TEACHER))


def _return_to_account(request):
    if request.user.is_authenticated:
        return redirect("accounts:verification_pending" if request.user.needs_email_verification else "accounts:profile")
    return redirect("accounts:login")


def _safe_next(request, value):
    if (isinstance(value, str) and value.startswith("/") and len(value) <= 2048 and
            url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}, require_https=request.is_secure())):
        return value
    return ""


def _failure(request, message):
    messages.error(request, message)
    return _return_to_account(request)


def _account_busy(request):
    messages.warning(request, "Your account is busy. Please try Google sign-in again; your saved work is unchanged.")
    return _return_to_account(request)


def _mark_google_owned_email_verified(user, profile):
    # Existing account email is authoritative. Never overwrite it, its role or
    # track with Google claims, or verify a different/non-Google-hosted email.
    if profile["authoritative"] and user.email.casefold() == profile["email"].casefold() and not user.is_verified:
        user.is_verified = True
        user.verification_token = uuid.uuid4()  # Invalidate any old email link.
        user.save(update_fields=["is_verified", "verification_token"])


@private_flow()
@require_POST
def google_start(request):
    config = configuration()
    if not config:
        return _failure(request, "Google sign-in is not configured yet. Please use your existing sign-in or contact the administrator.")
    if not same_configured_origin(request, config):
        return _failure(request, "Open the site's configured address before starting Google sign-in, then try again.")
    mode = request.POST.get("mode", "signin")
    if mode not in ("signin", "link"):
        return _failure(request, "Please start Google sign-in again.")
    if mode == "link":
        if not request.user.is_authenticated or not _eligible(request.user):
            return _failure(request, "Only an active student or teacher account can connect Google here.")
    elif request.user.is_authenticated:
        return _failure(request, "You are already signed in. Connect Google from your profile instead.")
    try:
        url = create_authorization_url(request, config, mode=mode,
                                       next_url=_safe_next(request, request.POST.get("next", "")))
    except OperationalError:
        return _account_busy(request)
    return redirect(url)


def _connect_existing(request, profile):
    try:
        with transaction.atomic():
            locked = lock_practice_user(request.user)
            if not _eligible(locked):
                raise GoogleSignInError("This account cannot use Google sign-in. Please use your existing sign-in.")
            if locked.email.casefold() != profile["email"].casefold():
                raise GoogleSignInError("Choose the Google account matching your profile email, or use your existing sign-in.")
            owned = GoogleIdentity.objects.filter(user=locked).first()
            other = GoogleIdentity.objects.filter(subject=profile["subject"]).first()
            if (owned and owned.subject != profile["subject"]) or (other and other.user_id != locked.pk):
                raise GoogleSignInError("This Google account cannot be connected here. Please use your existing sign-in or contact support.")
            if not owned:
                GoogleIdentity.objects.create(user=locked, subject=profile["subject"])
            _mark_google_owned_email_verified(locked, profile)
            verified = not locked.needs_email_verification
    except User.DoesNotExist:
        return _failure(request, "This account is no longer available. Please contact support.")
    except GoogleSignInError as exc:
        return _failure(request, str(exc))
    except IntegrityError:
        return _failure(request, "This Google account could not be connected. Please sign in with your existing account or contact support.")
    except OperationalError:
        return _account_busy(request)
    if verified:
        messages.success(request, "Google sign-in is connected to your existing account. Your saved work and credits are unchanged.")
        return redirect("core:dashboard_router")
    messages.warning(request, "Google sign-in is connected, but this email address still needs a separate verification email.")
    return redirect("accounts:verification_pending")


def _sign_in_existing(request, profile, next_url):
    # Read only the identifier before taking the same account lock used by
    # profile changes, commercial starts and account deletion.
    try:
        user_id = GoogleIdentity.objects.filter(subject=profile["subject"]).values_list("user_id", flat=True).first()
        if user_id is None:
            return None
        with transaction.atomic():
            user = lock_practice_user(User(pk=user_id))
            if (not _eligible(user) or
                    not GoogleIdentity.objects.filter(user_id=user.pk, subject=profile["subject"]).exists()):
                raise GoogleSignInError("This account cannot use Google sign-in. Please use your existing sign-in or contact support.")
            _mark_google_owned_email_verified(user, profile)
    except User.DoesNotExist:
        return _failure(request, "This account is no longer available. Please contact support.")
    except GoogleSignInError as exc:
        return _failure(request, str(exc))
    except OperationalError:
        return _account_busy(request)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    if user.needs_email_verification:
        messages.warning(request, "Please verify the email currently on your profile before starting new work.")
        return redirect("accounts:verification_pending")
    messages.success(request, "Signed in with Google.")
    return redirect(next_url or "core:dashboard_router")


@private_flow()
@require_GET
def google_callback(request):
    config = configuration()
    if not config or not same_configured_origin(request, config):
        return _failure(request, "Google sign-in is not configured for this address. Please use your existing sign-in.")
    states = request.GET.getlist("state")
    try:
        flow = consume_authorization_state(request, states[0] if len(states) == 1 else "")
    except OperationalError:
        return _account_busy(request)
    if not flow:
        return _failure(request, "This Google sign-in request expired or was already used. Please start again.")
    mode = flow.get("mode")
    if (mode == "link" and (not request.user.is_authenticated or request.user.pk != flow.get("user_id")) or
            mode == "signin" and request.user.is_authenticated or mode not in ("signin", "link")):
        return _failure(request, "Your account session changed. Please start Google sign-in again.")
    if request.GET.getlist("error"):
        return _failure(request, "Google sign-in was cancelled or could not be completed. Your account was not changed.")
    codes = request.GET.getlist("code")
    try:
        profile = exchange_code(codes[0] if len(codes) == 1 else "", flow, config)
    except GoogleSignInError as exc:
        return _failure(request, str(exc))
    if mode == "link":
        return _connect_existing(request, profile)
    existing = _sign_in_existing(request, profile, _safe_next(request, flow.get("next", "")))
    if existing is not None:
        return existing
    # Never join or merge accounts solely because the provider sent a matching
    # email. The owner must first sign into that site account and connect it.
    try:
        email_exists = User.objects.filter(email__iexact=profile["email"]).exists()
    except OperationalError:
        return _account_busy(request)
    if email_exists:
        messages.warning(request, "An account may already exist for this email. Sign in with your existing password, then connect Google from your profile.")
        return redirect("accounts:login")
    request.session[SIGNUP_SESSION_KEY] = {"profile": profile, "created_at": int(time.time()),
                                           "next": _safe_next(request, flow.get("next", ""))}
    return redirect("accounts:google_signup")


def _pending_signup(request):
    pending = request.session.get(SIGNUP_SESSION_KEY)
    if not isinstance(pending, dict) or not isinstance(pending.get("profile"), dict):
        return None
    created = pending.get("created_at")
    if not isinstance(created, int) or not 0 <= time.time() - created <= SIGNUP_LIFETIME_SECONDS:
        request.session.pop(SIGNUP_SESSION_KEY, None)
        return None
    return pending


# This URL has no provider credentials. "same-origin" prevents cross-origin
# referrer leaks while allowing a real browser to submit the HTTPS CSRF form;
# "no-referrer" can produce Origin: null and break legitimate Django POSTs.
@private_flow(referrer_policy="same-origin")
@require_http_methods(["GET", "POST"])
def google_signup(request):
    if request.user.is_authenticated:
        return _failure(request, "You are already signed in. Connect Google from your profile instead.")
    pending = _pending_signup(request)
    config = configuration()
    if not pending or not config or not same_configured_origin(request, config):
        return _failure(request, "Your Google sign-up has expired. Please start again.")
    profile = pending["profile"]
    initial = {"first_name": profile["first_name"], "last_name": profile["last_name"], "role": User.Roles.STUDENT}
    form = GoogleSignupForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                # Recheck after onboarding: another browser may have registered
                # or connected this identity while this form was open.
                if (GoogleIdentity.objects.filter(subject=profile["subject"]).exists() or
                        User.objects.filter(email__iexact=profile["email"]).exists()):
                    raise GoogleSignInError("An account may already exist for this email. Sign in with your existing account and connect Google from your profile.")
                role = form.cleaned_data["role"]
                track = form.cleaned_data["target_exam"] if role == User.Roles.STUDENT else ""
                user = User.objects.create_user(
                    username="google_" + uuid.uuid4().hex,
                    email=profile["email"], password=None,
                    first_name=form.cleaned_data["first_name"], last_name=form.cleaned_data["last_name"],
                    role=role, target_exam=track, exam_track=track.lower(),
                    is_staff=False, is_superuser=False, verification_required=True,
                    is_verified=profile["authoritative"],
                )
                GoogleIdentity.objects.create(user=user, subject=profile["subject"])
        except GoogleSignInError as exc:
            request.session.pop(SIGNUP_SESSION_KEY, None)
            messages.warning(request, str(exc))
            return redirect("accounts:login")
        except IntegrityError:
            request.session.pop(SIGNUP_SESSION_KEY, None)
            messages.warning(request, "An account may already exist for this email. Sign in with your existing account and connect Google from your profile.")
            return redirect("accounts:login")
        except OperationalError:
            messages.warning(request, "Your account could not be created while the database was busy. Please try again.")
            return render(request, "accounts/google_signup.html", {"form": form, "google_email": profile["email"]}, status=503)
        request.session.pop(SIGNUP_SESSION_KEY, None)
        next_url = _safe_next(request, pending.get("next", ""))
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        if user.needs_email_verification:
            messages.warning(request, "This Google account uses an email Google does not host. Verify that address separately before starting new work.")
            status_message(request, send_verification(user))
            return redirect("accounts:verification_pending")
        messages.success(request, "Your account is ready. Welcome!")
        return redirect(next_url or "core:dashboard_router")
    return render(request, "accounts/google_signup.html", {"form": form, "google_email": profile["email"]})


@private_flow(referrer_policy="same-origin")
@login_required
@require_http_methods(["GET", "POST"])
def google_add_password(request):
    """Let a verified Google-only account add, but never replace, a password.

    A fresh locked check prevents concurrent submissions from replacing the
    password established by the first request. Existing password accounts must
    use their normal password-reset flow instead.
    """
    def permitted(user):
        return (_eligible(user) and not user.needs_email_verification and not user.has_usable_password()
                and GoogleIdentity.objects.filter(user_id=user.pk).exists())

    if request.method == "GET":
        if not permitted(request.user):
            messages.warning(request, "This account cannot add a password here. Existing passwords can be reset from the sign-in page.")
            return _return_to_account(request)
        form = GoogleSetPasswordForm(request.user)
    else:
        try:
            with transaction.atomic():
                user = lock_practice_user(request.user)
                if not permitted(user):
                    raise GoogleSignInError("This account cannot add a password here. Existing passwords can be reset from the sign-in page.")
                form = GoogleSetPasswordForm(user, request.POST)
                valid = form.is_valid()
                if valid:
                    form.save()
        except User.DoesNotExist:
            return _failure(request, "This account is no longer available. Please contact support.")
        except GoogleSignInError as exc:
            return _failure(request, str(exc))
        except OperationalError:
            return _account_busy(request)
        if valid:
            # Retain this session; other sessions with the previous password
            # hash are invalidated by Django on their next request.
            update_session_auth_hash(request, user)
            messages.success(request, "A password was added. You can now sign in using your email and password or Google.")
            return redirect("accounts:profile")
    return render(request, "accounts/google_add_password.html", {"form": form})
