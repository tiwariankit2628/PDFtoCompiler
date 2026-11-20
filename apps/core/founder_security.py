"""Founder-only authorization, short-lived step-up and response protections.

Founder access is independent of roles: an explicitly enrolled active super
administrator must re-enter their password and a TOTP in each browser session.
Nothing here grants access to student PDFs, answers, or card information.
"""
import hashlib
import hmac
import re
import uuid
from datetime import timedelta

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.cache import patch_cache_control, patch_vary_headers

from .models import AuditLog, FounderAccess
from .permissions import ActiveUserRequiredMixin

SESSION_KEY = "founder_step_up"
SESSION_SECONDS = 15 * 60
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60


class FounderConfigurationError(Exception):
    pass


def _cipher():
    try:
        key = settings.FOUNDER_TOTP_ENCRYPTION_KEY
        if key == settings.SECRET_KEY or key in getattr(settings, "SECRET_KEY_FALLBACKS", ()):
            raise ValueError("Founder key must be independent of Django signing keys")
        return Fernet(key.encode("ascii"))
    except (AttributeError, TypeError, ValueError, UnicodeError):
        raise FounderConfigurationError("Founder authentication is not configured.") from None


def founder_role_eligible(user):
    """Require the actual founder-eligible role, not just a stray superuser flag.

    Other parts of the application retain their existing privilege rules. A
    malformed legacy STUDENT/TEACHER/ADMIN row with is_superuser=True must not
    become founder-eligible, even if someone manually inserts an access row.
    """
    return bool(user.is_active and user.role == get_user_model().Roles.SUPER_ADMIN
                and user.has_usable_password() and not user.needs_email_verification)


def encrypt_secret(secret):
    return _cipher().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_secret(access):
    try:
        secret = _cipher().decrypt(access.encrypted_totp_secret.encode("ascii")).decode("ascii")
        # Enrollment always generates a 32-character Base32 seed. Reject a
        # malformed/tampered value before it can reach the TOTP decoder.
        if not re.fullmatch(r"[A-Z2-7]{32}", secret):
            raise ValueError("Invalid authenticator seed")
        return secret
    except (InvalidToken, ValueError, UnicodeError, AttributeError):
        raise FounderConfigurationError("Founder authentication is not configured.") from None


def key_fingerprint():
    # A key replacement invalidates already-unlocked sessions immediately.
    _cipher()
    return hashlib.sha256(settings.FOUNDER_TOTP_ENCRYPTION_KEY.encode("ascii")).hexdigest()


def session_is_unlocked(request, access, *, now=None):
    token = request.session.get(SESSION_KEY)
    if not isinstance(token, dict):
        return False
    now = now or timezone.now()
    timestamp = token.get("verified_at")
    if type(timestamp) is not int:
        return False
    age = now.timestamp() - timestamp
    return bool(0 <= age < SESSION_SECONDS
                and token.get("user_id") == request.user.pk
                and token.get("access_version") == str(access.access_version)
                and token.get("key_fingerprint") == key_fingerprint())


def founder_audit(user, action, description):
    # Reverse-proxy headers are deliberately not trusted as client IPs. Never
    # put passwords, TOTP codes, private search terms or filenames in this log.
    return AuditLog.objects.create(user=user, action=action, description=description)


class FounderHeadersMiddleware:
    """Protect all founder responses, including redirects and error pages."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path_info == "/founder" or request.path_info.startswith("/founder/"):
            patch_cache_control(response, private=True, no_store=True, no_cache=True,
                                must_revalidate=True, max_age=0)
            patch_vary_headers(response, ("Cookie",))
            response["Pragma"] = "no-cache"
            response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
            # Browsers can send Origin: null on same-origin form POSTs when
            # no-referrer is used, causing HTTPS CSRF failures. same-origin
            # preserves CSRF's origin check without leaking paths cross-site.
            response["Referrer-Policy"] = "same-origin"
            response["X-Frame-Options"] = "DENY"
            response["X-Content-Type-Options"] = "nosniff"
            response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            response["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; font-src 'self'; connect-src 'none'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
        return response


class FounderRequiredMixin(ActiveUserRequiredMixin):
    require_step_up = True

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        user = request.user
        if not founder_role_eligible(user):
            raise PermissionDenied("Founder access is not available for this account.")
        # A separately provisioned row is required even for Django superusers.
        access = FounderAccess.objects.filter(user_id=user.pk, is_enabled=True).first()
        if not access:
            raise PermissionDenied("Founder access is not available for this account.")
        if not settings.DEBUG and not request.is_secure():
            return HttpResponse("Founder access requires HTTPS.", status=403)
        try:
            decrypt_secret(access)  # missing, changed or invalid key must fail closed
            unlocked = session_is_unlocked(request, access)
        except FounderConfigurationError:
            return HttpResponse("Founder authentication is unavailable. Check the server configuration.", status=503)
        self.founder_access = access
        self.founder_unlocked = unlocked
        if self.require_step_up and not unlocked:
            request.session.pop(SESSION_KEY, None)
            return redirect("founder:verify")
        return super().dispatch(request, *args, **kwargs)


def verify_step_up(request, password, code):
    """Serialize lockout and TOTP replay prevention across sessions/workers.

    Returns (success, seconds_until_retry). A correct password never bypasses
    the authenticator and accepted time steps can never be reused.
    """
    from apps.payments.services import lock_practice_user

    with transaction.atomic():
        try:
            user = lock_practice_user(request.user)
        except get_user_model().DoesNotExist:
            raise PermissionDenied("Founder access is not available for this account.") from None
        if not founder_role_eligible(user):
            raise PermissionDenied("Founder access is not available for this account.")
        # SQLite's select_for_update is a no-op. lock_practice_user already
        # holds its write lock, so the subsequent read is serialized too.
        access = (FounderAccess.objects.filter(user_id=user.pk, is_enabled=True)
                  .select_for_update(no_key=connection.vendor == "postgresql").first())
        if not access:
            raise PermissionDenied("Founder access is not available for this account.")
        now = timezone.now()  # evaluate only after acquiring both locks
        secret = decrypt_secret(access)
        if access.locked_until and access.locked_until > now:
            return False, max(1, int((access.locked_until - now).total_seconds()) + 1)
        if access.locked_until:
            access.locked_until = None
            access.failed_attempts = 0
        password_valid = isinstance(password, str) and len(password) <= 1024 and user.check_password(password)
        accepted_step = None
        if isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code):
            current = int(now.timestamp()) // 30
            totp = pyotp.TOTP(secret, digits=6, interval=30)
            # Allow one time-step of drift, but never accept a used or older
            # step. If adjacent steps happen to generate the same six digits,
            # consume the latest match so that value cannot be replayed in a
            # second session against another currently valid matching step.
            for step in range(current + 1, current - 2, -1):
                if step > access.last_accepted_step and hmac.compare_digest(totp.at(step * 30), code):
                    accepted_step = step
                    break
        if not password_valid or accepted_step is None:
            access.failed_attempts += 1
            retry = 0
            if access.failed_attempts >= MAX_FAILURES:
                access.locked_until = now + timedelta(seconds=LOCKOUT_SECONDS)
                retry = LOCKOUT_SECONDS
            access.save(update_fields=["failed_attempts", "locked_until", "updated_at"])
            founder_audit(user, "FOUNDER_UNLOCK_FAILED", "Founder verification was not accepted.")
            return False, retry
        access.last_accepted_step = accepted_step
        access.failed_attempts = 0
        access.locked_until = None
        access.save(update_fields=["last_accepted_step", "failed_attempts", "locked_until", "updated_at"])
        founder_audit(user, "FOUNDER_UNLOCKED", "Founder workspace unlocked after password and authenticator verification.")
        # check_password may transparently upgrade a legacy password hash.
        # Rotate the session and preserve the freshly verified auth hash so a
        # successful step-up is not immediately logged out by that upgrade.
        update_session_auth_hash(request, user)
        request.session[SESSION_KEY] = {"user_id": user.pk, "access_version": str(access.access_version),
                                        "key_fingerprint": key_fingerprint(), "verified_at": int(now.timestamp())}
        return True, 0


def new_access_version():
    return uuid.uuid4()
