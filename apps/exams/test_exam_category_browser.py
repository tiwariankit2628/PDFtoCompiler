"""Optional real-browser exercise of an empty category catalog and recovery."""
import os
from pathlib import Path
import shutil
import subprocess
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import User
from .models import Category, Exam


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium category test")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ExamCategoryBrowserTests(StaticLiveServerTestCase):
    def test_admin_creates_category_and_both_roles_refresh_without_losing_drafts(self):
        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install tests/browser dependencies first.")
        Category.objects.all().delete()
        User.objects.create_superuser("category_browser_admin", "category-admin@example.test", "CategoryBrowserPass987!")
        User.objects.create_user("category_browser_teacher", email="category-teacher@example.test",
                                 password="CategoryBrowserPass987!", role=User.Roles.TEACHER)
        env = os.environ.copy()
        env.update(CATEGORY_TEST_URL=self.live_server_url,
                   CATEGORY_TEST_CREATE_PATH=reverse("exams:exam_create"),
                   CATEGORY_TEST_CATEGORY_CREATE_PATH=reverse("exams:category_create"))
        script = Path(settings.BASE_DIR) / "tests" / "browser" / "exam-category-smoke.cjs"
        result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, f"Category browser test failed:\n{result.stdout}\n{result.stderr}")
        self.assertIn("EXAM_CATEGORY_BROWSER_OK", result.stdout)
        category = Category.objects.get(name="NEET & JEE")
        self.assertEqual(category.slug, "neet-jee")
        self.assertTrue(category.is_active)
        self.assertEqual(set(Exam.objects.filter(category=category).values_list("title", flat=True)),
                         {"Admin category draft", "Teacher category draft"})
