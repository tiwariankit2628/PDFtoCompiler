"""Responsive commercial UI smoke test with synthetic series content only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category, Exam, ExamQuestion, SeriesSlot, TestSeries
from apps.exams.test_series_workflow import printed_answer_png
from apps.payments.models import CreditUse
from apps.questions.models import Question, QuestionFigure, QuestionOption


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium commercial UI workflow")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   ALLOW_TEST_PAYMENT_CREDITS=False, RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET="")
class CommercialBrowserTests(StaticLiveServerTestCase):
    def test_responsive_pricing_tracks_series_result_and_release(self):
        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install tests/browser dependencies first.")
        with tempfile.TemporaryDirectory(prefix="commercial-browser-") as root, override_settings(
            MEDIA_ROOT=os.path.join(root, "media"), PRIVATE_PRACTICE_ROOT=os.path.join(root, "private")):
            password = "CommercialBrowserPass987!"
            jee = User.objects.create_user("browser_commercial_jee", email="bcj@example.test", password=password, role="STUDENT", exam_track="jee")
            neet = User.objects.create_user("browser_commercial_neet", email="bcn@example.test", password=password, role="STUDENT", exam_track="neet")
            teacher = User.objects.create_user("browser_commercial_teacher", email="bct@example.test", password=password, role="TEACHER")
            admin = User.objects.create_user("browser_commercial_admin", email="bca@example.test", password=password, role="ADMIN")
            category = Category.objects.create(name="Commercial browser", slug="commercial-browser")
            exam = Exam.objects.create(title="Synthetic browser JEE test", slug="browser-series-jee", category=category,
                creator=admin, status="published", target_type="jee_main", duration_minutes=20, is_free=True,
                image_first_mode=True, allow_review_answers=False, shuffle_questions=False, shuffle_options=False)
            question = Question.objects.create(category=category, creator=admin, subject="Math", target_type="jee_main",
                question_text="What is four?", explanation="Reviewed browser explanation", is_classroom_snapshot=True,
                is_image_first=True, answer_status="manually_verified")
            QuestionOption.objects.create(question=question, option_key="A", option_text="Four", is_correct=True)
            QuestionOption.objects.create(question=question, option_key="B", option_text="Five")
            figure = QuestionFigure(question=question, source="question_crop", page_number=1)
            figure.private_image.save("browser-crop.png", ContentFile(printed_answer_png()), save=True)
            ExamQuestion.objects.create(exam=exam, question=question, order=1, positive_marks=4, negative_marks=1)
            exam.recalculate_marks()
            # TransactionTestCase flushes migration seed rows between classes;
            # create a self-contained fixture rather than depending on order.
            for track, title in (("jee", "JEE test series"), ("neet", "NEET test series")):
                series, _ = TestSeries.objects.get_or_create(track=track, defaults={"title": title})
                for position in range(1, 17):
                    SeriesSlot.objects.get_or_create(series=series, position=position)
            slot = TestSeries.objects.get(track="jee").slots.get(position=1)
            slot.exam = exam
            slot.full_clean()
            slot.save(update_fields=["exam"])
            data = {"password": password, "jee": jee.username, "neet": neet.username, "teacher": teacher.username,
                    "admin": admin.username, "login": reverse("accounts:login"), "series": reverse("exams:series"),
                    "plans": reverse("payments:plans"), "classroom": reverse("exams:classroom_hub"),
                    "instructions": reverse("exams:exam_instructions", args=[exam.slug]),
                    "operator": reverse("exams:series_operator")}
            env = os.environ.copy()
            env.update(COMMERCIAL_TEST_URL=self.live_server_url, COMMERCIAL_TEST_DATA=json.dumps(data))
            script = Path(settings.BASE_DIR) / "tests" / "browser" / "commercial-smoke.cjs"
            result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=180)
            self.assertEqual(result.returncode, 0, f"Commercial browser workflow failed:\n{result.stdout}\n{result.stderr}")
            self.assertIn("COMMERCIAL_BROWSER_OK", result.stdout)
            attempt = exam.attempts.get(student=jee)
            self.assertEqual(attempt.score_obtained, 4)
            self.assertEqual(exam.attempts.filter(student=jee).count(), 1)
            self.assertFalse(CreditUse.objects.filter(user=jee, kind="classroom").exists())
            self.assertFalse(exam.attempts.filter(student=neet).exists())
