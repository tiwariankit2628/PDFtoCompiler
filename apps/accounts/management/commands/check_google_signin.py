"""Read-only, local configuration check for the optional Google sign-in flow."""
from urllib.parse import unquote

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from apps.accounts.google_signin import configuration_status


ISSUE_MESSAGES = {
    "disabled": "Set GOOGLE_SIGN_IN_ENABLED=True in the site's private environment.",
    "client_id": "Set GOOGLE_OAUTH_CLIENT_ID to the Google OAuth Web application client ID.",
    "client_secret": "Set GOOGLE_OAUTH_CLIENT_SECRET to that client's secret privately.",
    "origin": (
        "Set PUBLIC_BASE_URL to the exact site origin without a path or query. "
        "Use HTTPS, or loopback HTTP only for local development with DEBUG=True."
    ),
}


def _safe_callback_to_display(callback):
    """Do not reflect a credential or terminal control characters from settings."""
    if not callback.isascii() or any(not "!" <= char <= "~" for char in callback):
        return False
    credentials = (settings.GOOGLE_OAUTH_CLIENT_ID, settings.GOOGLE_OAUTH_CLIENT_SECRET)
    decoded = unquote(callback).casefold()
    return not any(isinstance(value, str) and value and value.casefold() in decoded for value in credentials)


class Command(BaseCommand):
    help = "Check Google Sign-In settings locally without contacting Google or changing data."
    requires_system_checks = []

    def handle(self, *args, **options):
        origin, issues = configuration_status()
        if issues:
            self.stdout.write("Google Sign-In is not ready. Check these settings:")
            for issue in issues:
                self.stdout.write(f"- {ISSUE_MESSAGES[issue]}")
        else:
            self.stdout.write("The Google button is enabled by the current local settings.")

        if origin:
            callback = origin + reverse("accounts:google_callback")
            if _safe_callback_to_display(callback):
                self.stdout.write(f"Current callback URL to register in Google Cloud: {callback}")
            else:
                self.stdout.write("Callback URL hidden: check PUBLIC_BASE_URL for misplaced credentials or unusual characters.")
        else:
            self.stdout.write("Set a valid PUBLIC_BASE_URL to determine the callback URL.")

        self.stdout.write(
            "Local format check only: Google credentials, consent, network access and database migrations "
            "were not verified. Restart the site after changes and test with a Google account you control."
        )
        self.stdout.write("Existing process environment variables take precedence over values in .env.")
        if issues:
            raise CommandError("Google Sign-In is not configured; correct the settings listed above.")
