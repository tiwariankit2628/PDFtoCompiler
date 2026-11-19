"""Time-limited, email-bound verification without trusting a request Host."""
import logging
import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.db import OperationalError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from .models import User

logger = logging.getLogger(__name__)
SALT = "accounts.email-verification.v1"


def invalidate_changed_email(user, original_email):
    """A verified address cannot transfer its verification to a new address."""
    if (original_email or "").casefold() == (user.email or "").casefold():
        return False
    user.is_verified = False
    user.verification_required = True
    user.verification_token = uuid.uuid4()
    user.verification_sent_at = None
    return True


def base_url():
    value = settings.PUBLIC_BASE_URL
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
                or parts.path not in ("", "/") or parts.query or parts.fragment
                or (not settings.DEBUG and parts.scheme != "https")):
            return ""
        # Invalid port raises ValueError. A configured base URL, not the Host
        # header, determines the destination in every verification email.
        parts.port
        return value.rstrip("/")
    except ValueError:
        return ""


def send_verification(user):
    base = base_url()
    if not base:
        return "unavailable"
    from apps.payments.services import lock_practice_user
    try:
        with transaction.atomic():
            # Serialize with profile/admin edits and confirmation on SQLite as
            # well as PostgreSQL; never issue a link using a stale address.
            locked = lock_practice_user(user)
            if not locked.is_active:
                return "unavailable"
            if locked.is_verified:
                return "verified"
            if locked.verification_sent_at and (timezone.now() - locked.verification_sent_at).total_seconds() < settings.EMAIL_VERIFICATION_RESEND_SECONDS:
                return "throttled"
            locked.verification_token = uuid.uuid4()
            locked.verification_sent_at = timezone.now()
            locked.save(update_fields=["verification_token", "verification_sent_at"])
            token = signing.dumps({"uid": locked.pk, "email": locked.email, "nonce": str(locked.verification_token)}, salt=SALT, compress=True)
            address = locked.email
    except User.DoesNotExist:
        return "unavailable"
    except OperationalError as exc:
        if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
            raise
        return "unavailable"
    url = base + reverse("accounts:verify_email", kwargs={"token": token})
    try:
        count = send_mail(
            f"Verify your email for {settings.PLATFORM_NAME}",
            f"Please verify your email address for {settings.PLATFORM_NAME}.\n\n{url}\n\n"
            "This link expires after 24 hours. If you did not create this account, ignore this email.",
            settings.DEFAULT_FROM_EMAIL, [address], fail_silently=False,
        )
        if count != 1:
            return "unavailable"
    except Exception:
        # Never log the URL, token, password or provider error details.
        logger.warning("Verification email could not be submitted to the configured backend.")
        return "unavailable"
    if settings.EMAIL_BACKEND == "django.core.mail.backends.console.EmailBackend":
        return "console"
    return "sent"


def notify_admin_of_verification(request, user):
    """Never expose the verification URL or imply that mail was delivered."""
    from django.contrib import messages
    outcome = send_verification(user)
    if outcome == "sent":
        messages.info(request, "A verification email was submitted for the updated account.")
    elif outcome == "console":
        messages.warning(request, "The updated account's verification link was printed to the development console, not delivered.")
    elif outcome != "verified":
        messages.warning(request, "The updated address needs verification, but the email was not sent. The account can request another link.")


def token_user(token):
    if len(token) > 4096:
        return None
    try:
        data = signing.loads(token, salt=SALT, max_age=settings.EMAIL_VERIFICATION_MAX_AGE)
        if not isinstance(data, dict) or not isinstance(data.get("uid"), int):
            return None
        user = User.objects.filter(pk=data["uid"], is_active=True).first()
        if (not user or user.is_verified or user.email != data.get("email")
                or str(user.verification_token) != data.get("nonce")):
            return None
        return user
    except (signing.BadSignature, TypeError, ValueError):
        return None


def status_message(request, status):
    from django.contrib import messages
    if status == "sent":
        messages.info(request, "A verification email was submitted to the mail service. Check your inbox and spam folder.")
    elif status == "console":
        messages.warning(request, "Development mode: the verification link was printed to the server console, not delivered by email.")
    elif status == "throttled":
        messages.warning(request, "Please wait a minute before requesting another verification email.")
    elif status == "unavailable":
        messages.error(request, "Verification email is not configured or could not be sent. Please contact the site administrator or retry later.")
