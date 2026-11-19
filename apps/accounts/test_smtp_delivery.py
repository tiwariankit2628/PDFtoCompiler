"""Safety checks for the opt-in SMTP diagnostic; provider calls are mocked."""
import io
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings


@override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST="smtp.gmail.com",
                   EMAIL_PORT=587, EMAIL_USE_TLS=True, EMAIL_USE_SSL=False,
                   EMAIL_HOST_USER="sender@gmail.com", EMAIL_HOST_PASSWORD="synthetic-password",
                   DEFAULT_FROM_EMAIL="Sender <sender@gmail.com>")
class SMTPDeliveryCheckTests(SimpleTestCase):
    command = "apps.accounts.management.commands.check_smtp_delivery.send_mail"

    @patch(command, return_value=1)
    def test_explicit_send_reports_acceptance_not_delivery(self, send):
        out = io.StringIO()
        call_command("check_smtp_delivery", "controlled@example.test", confirm_send=True, stdout=out)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[3], ["controlled@example.test"])
        self.assertIn("accepted", out.getvalue())
        self.assertIn("not delivery", out.getvalue())
        self.assertNotIn("synthetic-password", out.getvalue())

    @patch(command)
    def test_no_confirmation_or_bad_recipient_never_sends(self, send):
        for address, confirm in (("controlled@example.test", False), ("bad-address", True),
                                 ("a@example.test,b@example.test", True)):
            with self.assertRaises(CommandError):
                call_command("check_smtp_delivery", address, confirm_send=confirm)
        send.assert_not_called()

    @patch(command)
    def test_console_insecure_or_missing_credentials_never_sends(self, send):
        for change in ({"EMAIL_BACKEND": "django.core.mail.backends.console.EmailBackend"},
                       {"EMAIL_USE_TLS": False}, {"EMAIL_USE_SSL": True},
                       {"EMAIL_HOST_PASSWORD": ""}, {"DEFAULT_FROM_EMAIL": "other@gmail.com"}):
            with self.subTest(change=change), override_settings(**change), self.assertRaises(CommandError):
                call_command("check_smtp_delivery", "controlled@example.test", confirm_send=True)
        send.assert_not_called()

    @patch(command, side_effect=RuntimeError("synthetic-password recipient@example.test"))
    def test_provider_errors_are_sanitized(self, send):
        with self.assertRaises(CommandError) as result:
            call_command("check_smtp_delivery", "controlled@example.test", confirm_send=True)
        self.assertIn("RuntimeError", str(result.exception))
        self.assertNotIn("synthetic-password", str(result.exception))
        self.assertNotIn("recipient@example.test", str(result.exception))
        send.assert_called_once()

    @patch(command, return_value=0)
    def test_zero_accepted_is_not_success(self, send):
        with self.assertRaises(CommandError):
            call_command("check_smtp_delivery", "controlled@example.test", confirm_send=True)
        send.assert_called_once()
