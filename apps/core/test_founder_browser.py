"""Optional real-Chromium HTTPS founder workflow using synthetic accounts only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest import skipUnless

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.test_google_browser import LocalTLSProxy
from apps.core.founder_security import encrypt_secret
from apps.core.models import ActivityTrackingState, FounderAccess


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium founder HTTPS test")
@override_settings(DEBUG=True, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
                   SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
class FounderBrowserTests(StaticLiveServerTestCase):
    def test_founder_second_factor_and_responsive_private_workspace_over_https(self):
        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install tests/browser dependencies first.")
        key = Fernet.generate_key().decode()
        secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # synthetic only
        password = "BrowserFounderPassword987!"
        with override_settings(FOUNDER_TOTP_ENCRYPTION_KEY=key):
            # A preceding TransactionTestCase can flush migration-created rows.
            # This browser fixture explicitly starts its own synthetic tracking.
            ActivityTrackingState.objects.update_or_create(key="sign_in", defaults={"started_at": timezone.now()})
            owner = User.objects.create_superuser("browser-founder", "browser-founder@example.test", password, first_name="Anika")
            other = User.objects.create_superuser("browser-other-super", "browser-super@example.test", password)
            student = User.objects.create_user("browser-support-student", "browser-student@example.test", password,
                                               first_name="Synthetic", last_name="Student", role="STUDENT", exam_track="jee")
            FounderAccess.objects.create(user=owner, is_enabled=True, encrypted_totp_secret=encrypt_secret(secret))
            with tempfile.TemporaryDirectory(prefix="founder-browser-tls-") as folder:
                proxy = LocalTLSProxy(self.live_server_url, folder)
                self.addCleanup(proxy.close)
                data = {"base": proxy.origin, "password": password, "secret": secret, "owner": owner.username,
                        "other": other.username, "student_email": student.email, "student_pk": student.pk,
                        "login": reverse("accounts:login"), "verify": reverse("founder:verify"),
                        "overview": reverse("founder:overview"), "accounts": reverse("founder:accounts"),
                        "series": reverse("founder:series"), "payments": reverse("founder:payments"),
                        "activity": reverse("founder:activity"), "lock": reverse("founder:lock")}
                env = os.environ.copy()
                env["FOUNDER_BROWSER_DATA"] = json.dumps(data)
                script = Path(settings.BASE_DIR) / "tests" / "browser" / "founder-smoke.cjs"
                result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=180)
                self.assertEqual(result.returncode, 0, f"Founder browser test failed:\n{result.stdout}\n{result.stderr}")
                self.assertIn("FOUNDER_BROWSER_OK", result.stdout)
