"""The Google configuration diagnostic is local, read-only and secret-safe."""
import io
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from .google_signin import configuration, configuration_status
from .test_google_signin import CLIENT_ID, CLIENT_SECRET, GOOGLE_SETTINGS


@override_settings(**GOOGLE_SETTINGS)
class GoogleConfigurationCheckTests(SimpleTestCase):
    def command_output(self, *, expect_error=False):
        out = io.StringIO()
        if expect_error:
            with self.assertRaises(CommandError) as error:
                call_command("check_google_signin", stdout=out)
            self.assertNotIn(CLIENT_SECRET, str(error.exception))
        else:
            call_command("check_google_signin", stdout=out)
        return out.getvalue()

    @patch("socket.socket.connect", side_effect=AssertionError("Network access was attempted"))
    def test_ready_check_is_read_only_and_never_prints_credentials(self, connect):
        # SimpleTestCase also forbids database queries. No provider is contacted.
        output = self.command_output()
        connect.assert_not_called()
        self.assertIn("button is enabled", output)
        self.assertIn("http://127.0.0.1:8000/auth/google/callback/", output)
        self.assertIn("Local format check only", output)
        self.assertIn("were not verified", output)
        self.assertIn("environment variables take precedence", output)
        self.assertNotIn(CLIENT_ID, output)
        self.assertNotIn(CLIENT_SECRET, output)

    @override_settings(GOOGLE_SIGN_IN_ENABLED=False, GOOGLE_OAUTH_CLIENT_ID="", GOOGLE_OAUTH_CLIENT_SECRET="")
    def test_incomplete_configuration_reports_all_fixes_and_nonzero_status(self):
        output = self.command_output(expect_error=True)
        self.assertIn("GOOGLE_SIGN_IN_ENABLED=True", output)
        self.assertIn("GOOGLE_OAUTH_CLIENT_ID", output)
        self.assertIn("GOOGLE_OAUTH_CLIENT_SECRET", output)
        self.assertIn("http://127.0.0.1:8000/auth/google/callback/", output)
        self.assertIsNone(configuration())

    def test_diagnostic_and_login_use_the_same_validation(self):
        cases = (
            ({"GOOGLE_SIGN_IN_ENABLED": False}, ("disabled",)),
            ({"GOOGLE_OAUTH_CLIENT_ID": "not-a-web-client"}, ("client_id",)),
            ({"GOOGLE_OAUTH_CLIENT_ID": "x " + CLIENT_ID}, ("client_id",)),
            ({"GOOGLE_OAUTH_CLIENT_SECRET": ""}, ("client_secret",)),
            ({"GOOGLE_OAUTH_CLIENT_SECRET": "not a secret"}, ("client_secret",)),
            ({"PUBLIC_BASE_URL": ""}, ("origin",)),
            ({"PUBLIC_BASE_URL": "https://example.test/path"}, ("origin",)),
            ({"PUBLIC_BASE_URL": "http://public.example.test"}, ("origin",)),
            ({"PUBLIC_BASE_URL": "http://127.0.0.1:8000", "DEBUG": False}, ("origin",)),
            ({"PUBLIC_BASE_URL": "https://example.test:bad"}, ("origin",)),
            ({"PUBLIC_BASE_URL": "https://example.test\n.evil.test"}, ("origin",)),
            ({"PUBLIC_BASE_URL": None}, ("origin",)),
            ({"PUBLIC_BASE_URL": "https://example.test", "DEBUG": False}, ()),
            ({"PUBLIC_BASE_URL": "http://localhost:8000"}, ()),
            ({"PUBLIC_BASE_URL": "http://[::1]:8000"}, ()),
        )
        for changes, expected in cases:
            with self.subTest(changes=changes), override_settings(**changes):
                origin, issues = configuration_status()
                self.assertEqual(issues, expected)
                self.assertEqual(configuration() is not None, not issues)
                self.assertEqual(bool(origin), "origin" not in issues)
                output = self.command_output(expect_error=bool(issues))
                self.assertNotIn(CLIENT_ID, output)
                self.assertNotIn(CLIENT_SECRET, output)

    def test_callback_output_hides_accidentally_embedded_credentials(self):
        secret = "unique-synthetic-sensitive-value"
        cases = (
            (f"https://{secret}.example.test", secret, False),
            (f"https://{secret.upper()}.example.test", secret, False),
            (f"https://user:{secret}@example.test", secret, True),
            ("https://unique%24synthetic.example.test", "unique$synthetic", False),
            (f"https://{CLIENT_ID}", secret, False),
        )
        for url, value, has_issue in cases:
            with self.subTest(url=url), override_settings(PUBLIC_BASE_URL=url, GOOGLE_OAUTH_CLIENT_SECRET=value):
                output = self.command_output(expect_error=has_issue)
                self.assertNotIn(value, output)
                self.assertNotIn(CLIENT_ID, output)
                if not has_issue:
                    self.assertIn("Callback URL hidden", output)

    @override_settings(PUBLIC_BASE_URL="https://example.test\n.evil.test")
    def test_callback_output_does_not_reflect_terminal_control_characters(self):
        output = self.command_output(expect_error=True)
        self.assertIn("Set a valid PUBLIC_BASE_URL", output)
        self.assertNotIn("example.test\n.evil.test", output)
