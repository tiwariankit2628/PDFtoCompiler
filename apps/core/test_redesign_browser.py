"""Optional Chromium tests for public interactions and accessibility fallbacks."""
import os
from pathlib import Path
import shutil
import subprocess
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium redesign test")
class RedesignBrowserTests(StaticLiveServerTestCase):
    def test_interactions_rotation_reduced_motion_and_no_js(self):
        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install tests/browser dependencies first.")
        env = os.environ.copy()
        env["REDESIGN_TEST_URL"] = self.live_server_url
        script = Path(settings.BASE_DIR) / "tests" / "browser" / "redesign-smoke.cjs"
        result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, f"Redesign browser test failed:\n{result.stdout}\n{result.stderr}")
        self.assertIn("REDESIGN_BROWSER_OK", result.stdout)
