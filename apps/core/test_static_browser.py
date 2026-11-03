"""Optional Chromium checks against collected production assets and all roles.

Unlike StaticLiveServerTestCase, LiveServerTestCase does not insert Django's
staticfiles development handler, so WhiteNoise actually serves the manifest.
"""
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import LiveServerTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion, ExamResponse
from apps.questions.models import Question, QuestionOption


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium static/UI smoke test")
class ProductionStaticBrowserTests(LiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = TemporaryDirectory(prefix="production-static-browser-")
        cls.addClassCleanup(cls.workspace.cleanup)
        root = Path(cls.workspace.name)
        # Register settings before LiveServerTestCase's own settings context so
        # class cleanups restore them in reverse order, after the server stops.
        cls.enterClassContext(override_settings(
            DEBUG=False,
            ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1", "[::1]"],
            # These three are disabled only for the isolated HTTP test server.
            SECURE_SSL_REDIRECT=False,
            SESSION_COOKIE_SECURE=False,
            CSRF_COOKIE_SECURE=False,
            STATIC_ROOT=str(root / "staticfiles"),
            MEDIA_ROOT=str(root / "media"),
            PRIVATE_PRACTICE_ROOT=str(root / "private"),
            WHITENOISE_AUTOREFRESH=False,
            WHITENOISE_USE_FINDERS=False,
            STORAGES={
                "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
            },
            PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
            PDF_CLOUD_ENABLED=False,
            RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET="", RAZORPAY_WEBHOOK_SECRET="",
        ))
        call_command("collectstatic", interactive=False, verbosity=0)
        super().setUpClass()

    def test_public_pages_all_role_dashboards_mobile_navigation_and_vendor_assets(self):
        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install Node and tests/browser dependencies before enabling this test.")

        user_model = get_user_model()
        password = "StaticBrowserTestPass987!"
        roles = {}
        for role in ("STUDENT", "TEACHER", "ADMIN", "SUPER_ADMIN"):
            username = "browser_" + role.lower() + ("_with_a_long_name" if role == "TEACHER" else "")
            roles[role] = user_model.objects.create_user(
                username=username, email=username + "@example.test", password=password,
                role=role, is_active=True, is_staff=role in ("ADMIN", "SUPER_ADMIN"),
            )
        category = Category.objects.create(name="Browser Static Test", slug="browser-static-test")
        teacher, student = roles["TEACHER"], roles["STUDENT"]
        teacher.assigned_categories.add(category)
        long_formula = "$" + "+".join([r"\frac{12345}{67890}"] * 6) + "$"
        question = Question.objects.create(
            category=category, creator=teacher, subject="Mathematics",
            question_text=r"Find $x^2$ when $x=2$. " + long_formula,
        )
        QuestionOption.objects.create(question=question, option_key="A", option_text="4", is_correct=True)
        QuestionOption.objects.create(question=question, option_key="B", option_text=long_formula)
        exam = Exam.objects.create(
            title="Browser Static Exam", slug="browser-static-exam", category=category,
            creator=teacher, status=Exam.ExamStatus.PUBLISHED, is_free=True,
        )
        exam_question = ExamQuestion.objects.create(exam=exam, question=question, order=1)
        attempt = ExamAttempt.objects.create(
            student=student, exam=exam, expires_at=timezone.now() + timedelta(hours=2),
        )
        ExamResponse.objects.create(attempt=attempt, exam_question=exam_question, order=1)

        data = {
            "password": password,
            "roles": {
                "student": {"username": roles["STUDENT"].username, "dashboard": reverse("core:student_dashboard"), "nav": "My PDF Practice", "target": reverse("questions:practice_list")},
                "teacher": {"username": roles["TEACHER"].username, "dashboard": reverse("core:teacher_dashboard"), "nav": "PDF Papers", "target": reverse("questions:paper_list")},
                "admin": {"username": roles["ADMIN"].username, "dashboard": reverse("core:admin_dashboard"), "nav": "Teachers", "target": reverse("accounts:teacher_list")},
                "super_admin": {"username": roles["SUPER_ADMIN"].username, "dashboard": reverse("core:super_admin_dashboard"), "nav": "Admins", "target": reverse("accounts:admin_list")},
            },
            "paths": {
                "home": reverse("core:home"), "about": reverse("core:about"),
                "contact": reverse("core:contact"), "login": reverse("accounts:login"),
                "logout": reverse("accounts:logout"), "register": reverse("accounts:register"),
                "catalog": reverse("exams:exam_catalog"), "profile": reverse("accounts:profile"),
                "practice": reverse("questions:practice_list"),
                "bank": reverse("questions:practice_bank"), "history": reverse("questions:practice_history"),
                "plans": reverse("payments:plans"), "papers": reverse("questions:paper_list"),
                "questions": reverse("questions:question_bank"), "exams": reverse("exams:exam_list"),
                "categories": reverse("exams:category_list"), "teachers": reverse("accounts:teacher_list"),
                "students": reverse("accounts:student_list"), "admins": reverse("accounts:admin_list"),
                "audit": reverse("core:audit_log_list"),
                "question_preview": reverse("questions:question_preview", args=[question.pk]),
                "analytics": reverse("exams:teacher_exam_analytics", args=[exam.pk]),
                "session": reverse("exams:exam_session", args=[attempt.attempt_uuid]),
            },
        }
        env = os.environ.copy()
        env.update(STATIC_TEST_URL=self.live_server_url, STATIC_TEST_DATA=json.dumps(data))
        script = Path(settings.BASE_DIR) / "tests" / "browser" / "static-smoke.cjs"
        result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=180)
        self.assertEqual(result.returncode, 0, f"Static browser test failed:\n{result.stdout}\n{result.stderr}")
        self.assertIn("BROWSER_STATIC_OK", result.stdout)
