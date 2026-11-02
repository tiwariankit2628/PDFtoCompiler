"""Optional real-browser workflow, isolated from the development database.

Install tests/browser dependencies and set RUN_BROWSER_E2E=1 and CHROMIUM_PATH.
No real payment or cloud OCR provider is contacted.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest import skipUnless

import pymupdf as fitz
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from .test_pdf_layout import make_two_column_pdf


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium smoke test")
class BrowserPracticeSmokeTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix="practice-browser-test-")
        cls.addClassCleanup(cls.workspace.cleanup)
        root = Path(cls.workspace.name)
        # LiveServerTestCase also overrides settings; let class cleanups unwind
        # those contexts in reverse order instead of leaking the test settings.
        cls.enterClassContext(override_settings(
            PRIVATE_PRACTICE_ROOT=str(root / "private"),
            MEDIA_ROOT=str(root / "media"),
            CELERY_TASK_ALWAYS_EAGER=True,
            PDF_CLOUD_ENABLED=False,
            RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET="", RAZORPAY_WEBHOOK_SECRET="",
            PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
        ))
        super().setUpClass()

    def test_pdf_to_result_reattempt_report_and_bank_in_chromium(self):
        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install Node and tests/browser dependencies before enabling this test.")
        root = Path(self.workspace.name)
        source, report = root / "synthetic.pdf", root / "report.pdf"
        source.write_bytes(make_two_column_pdf())
        env = os.environ.copy()
        env.update(PRACTICE_TEST_URL=self.live_server_url, PRACTICE_TEST_PDF=str(source), PRACTICE_TEST_REPORT=str(report))
        script = Path(settings.BASE_DIR) / "tests" / "browser" / "practice-smoke.cjs"
        result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, f"Browser test failed:\n{result.stdout}\n{result.stderr}")
        self.assertIn("BROWSER_WORKFLOW_OK", result.stdout)
        with fitz.open(report) as document:
            self.assertGreater(len(document), 0)
            self.assertTrue(any(page.get_images() for page in document))
            fonts = [font[3] for page in document for font in page.get_fonts()]
            self.assertTrue(any("Devanagari" in font for font in fonts))
