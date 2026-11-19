"""Send one explicit operator-controlled SMTP diagnostic, never a fake success."""
from email.utils import parseaddr

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email


class Command(BaseCommand):
    help = "Send one diagnostic email to an inbox you control using the configured SMTP backend."

    def add_arguments(self, parser):
        parser.add_argument("recipient", help="A single email address you control (not a mailing list).")
        parser.add_argument("--confirm-send", action="store_true", help="Actually send one message; no message is sent without this flag.")

    def handle(self, *args, **options):
        if not options["confirm_send"]:
            raise CommandError("No email sent. Add --confirm-send only for an inbox you control.")
        recipient = options["recipient"].strip()
        try:
            validate_email(recipient)
        except ValidationError:
            raise CommandError("Enter a single valid recipient address.") from None
        if settings.EMAIL_BACKEND != "django.core.mail.backends.smtp.EmailBackend":
            raise CommandError("No email sent: configure the Django SMTP backend, not console/file/locmem.")
        if bool(settings.EMAIL_USE_TLS) == bool(settings.EMAIL_USE_SSL):
            raise CommandError("No email sent: enable exactly one of TLS or SSL.")
        if not settings.EMAIL_HOST or not settings.EMAIL_HOST_USER or not settings.EMAIL_HOST_PASSWORD:
            raise CommandError("No email sent: configure the SMTP host and credentials privately.")
        sender = parseaddr(settings.DEFAULT_FROM_EMAIL)[1]
        try:
            validate_email(sender)
        except ValidationError:
            raise CommandError("No email sent: configure a valid DEFAULT_FROM_EMAIL.") from None
        if settings.EMAIL_HOST.lower() == "smtp.gmail.com" and sender.casefold() != settings.EMAIL_HOST_USER.casefold():
            raise CommandError("No email sent: for this Gmail check, the From address must match the Gmail login.")
        try:
            count = send_mail(
                f"{settings.PLATFORM_NAME} SMTP delivery check",
                "This single diagnostic message was requested by the site operator. "
                "SMTP acceptance does not guarantee inbox delivery; check Inbox and Spam.\n\n"
                "This is not a verification link and does not change any account.\n",
                settings.DEFAULT_FROM_EMAIL, [recipient], fail_silently=False,
            )
        except Exception as exc:
            # Do not echo SMTP credentials, provider response, or the recipient.
            raise CommandError(f"SMTP submission failed ({type(exc).__name__}). No delivery is confirmed.") from None
        if count != 1:
            raise CommandError("SMTP backend did not report one accepted message; delivery is not confirmed.")
        self.stdout.write("SMTP server accepted one diagnostic message. Check the controlled inbox and Spam; acceptance is not delivery.")
