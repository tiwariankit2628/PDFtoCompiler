"""Disposable file-based email for real-browser verification tests only."""
from contextlib import contextmanager
from tempfile import TemporaryDirectory

from django.test import override_settings


@contextmanager
def capture_browser_mail(base_url, env):
    # The browser reads the synthetic email and follows the real signed link;
    # no verification gate is disabled and no external mail is sent.
    with TemporaryDirectory(prefix="browser-test-mail-") as folder:
        env["BROWSER_TEST_EMAIL_DIR"] = folder
        # Django's test runner disables DEBUG, so use an HTTPS origin in the
        # generated mail and map only its path to the local HTTP test server.
        # The signed token and confirmation POST remain completely real.
        mail_base = "https://browser-verification.example.test"
        env["BROWSER_TEST_EMAIL_BASE"] = mail_base
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.filebased.EmailBackend",
                               EMAIL_FILE_PATH=folder, PUBLIC_BASE_URL=mail_base):
            yield
