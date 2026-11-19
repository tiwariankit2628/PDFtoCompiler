"""Optional real-browser check of JEE/NEET registration and isolation."""
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse

from apps.accounts.browser_test_mail import capture_browser_mail
from apps.accounts.models import User
from apps.exams.models import Category, Exam, ExamQuestion
from apps.questions.models import Question, QuestionOption


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium target isolation workflow")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ExamTargetBrowserTests(StaticLiveServerTestCase):
    def test_mobile_registration_catalog_direct_access_and_active_track_change(self):
        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install tests/browser dependencies first.")
        teacher = User.objects.create_user("target_browser_teacher", email="target-browser-t@example.test", role="TEACHER")
        category = Category.objects.create(name="Target browser", slug="target-browser")
        exams = {}
        for target, title in (("jee_main", "Browser JEE Main paper"),
                              ("jee_advanced", "Browser JEE Advanced paper"), ("neet", "Browser NEET paper")):
            exam = Exam.objects.create(title=title, slug="browser-" + target.replace("_", "-"), creator=teacher,
                category=category, target_type=target, status="published", is_free=True, duration_minutes=20,
                shuffle_questions=False)
            q = Question.objects.create(category=category, creator=teacher, subject="Science", target_type=target,
                                        question_text=f"{title} question")
            QuestionOption.objects.create(question=q, option_key="A", option_text="A", is_correct=True)
            QuestionOption.objects.create(question=q, option_key="B", option_text="B")
            ExamQuestion.objects.create(exam=exam, question=q, order=1, positive_marks=4, negative_marks=1)
            exam.recalculate_marks(); exams[target] = exam
        data = {"register": reverse("accounts:register"), "catalog": reverse("exams:exam_catalog"),
                "profile": reverse("accounts:profile"),
                "main": reverse("exams:exam_instructions", args=[exams["jee_main"].slug]),
                "advanced": reverse("exams:exam_instructions", args=[exams["jee_advanced"].slug]),
                "neet": reverse("exams:exam_instructions", args=[exams["neet"].slug])}
        env = os.environ.copy(); env.update(TARGET_TEST_URL=self.live_server_url, TARGET_TEST_DATA=json.dumps(data))
        script = Path(settings.BASE_DIR) / "tests" / "browser" / "exam-target-smoke.cjs"
        with capture_browser_mail(self.live_server_url, env):
            result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, f"Target browser workflow failed:\n{result.stdout}\n{result.stderr}")
        self.assertIn("EXAM_TARGET_BROWSER_OK", result.stdout)
        student = User.objects.get(username="browser_target_student")
        self.assertEqual(student.exam_track, "jee")
        attempt = exams["jee_main"].attempts.get(student=student)
        self.assertEqual(attempt.expires_at - attempt.started_at, timedelta(minutes=20))
        self.assertEqual(attempt.responses.count(), 1)
        self.assertFalse(exams["neet"].attempts.exists())
