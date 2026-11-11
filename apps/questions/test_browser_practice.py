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


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium upgrade test")
class BrowserUpgradeSmokeTests(StaticLiveServerTestCase):
    """Mobile table and opt-in AI workflow; provider and PDF are synthetic."""
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix="upgrade-browser-test-")
        cls.addClassCleanup(cls.workspace.cleanup)
        root = Path(cls.workspace.name)
        cls.enterClassContext(override_settings(
            PRIVATE_PRACTICE_ROOT=str(root / "private"), MEDIA_ROOT=str(root / "media"),
            CELERY_TASK_ALWAYS_EAGER=True, PDF_CLOUD_ENABLED=False,
            AI_ANSWER_ENABLED=True, AI_ANSWER_MODEL="mock-model", GEMINI_API_KEY="test-only-key",
            RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET="", RAZORPAY_WEBHOOK_SECRET="",
            PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
        ))
        super().setUpClass()

    def test_synthetic_table_ai_consent_editor_and_cbt_in_chromium(self):
        import json
        from decimal import Decimal
        from unittest.mock import patch
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.urls import reverse
        from apps.accounts.models import User
        from apps.exams.models import Category
        from .models import ExtractedQuestion, QuestionPaperUpload
        from .test_upgrade import ruled_table_pdf, cell

        root = Path(self.workspace.name)
        source = root / "ruled.pdf"
        with fitz.open(stream=ruled_table_pdf(), filetype="pdf") as document:
            page = document[0]
            for number, y in ((2, 390), (3, 490)):
                page.insert_text((30, y), f"Q{number} Another question?")
                page.insert_text((60, y + 22), "(1) First")
                page.insert_text((60, y + 42), "(2) Second")
            source.write_bytes(document.write())
        teacher = User.objects.create_user(username="upgrade_teacher", email="upgrade_teacher@example.test",
            password="TeacherBrowserPass987!", role=User.Roles.TEACHER, is_staff=True)
        category = Category.objects.create(name="Browser teacher", slug="browser-teacher")
        teacher.assigned_categories.add(category)
        paper = QuestionPaperUpload.objects.create(title="Teacher table", category=category, uploaded_by=teacher,
            pdf_file=SimpleUploadedFile("teacher.pdf", ruled_table_pdf()), total_pages=1,
            status=QuestionPaperUpload.ProcessingStatus.EXTRACTED)
        question = ExtractedQuestion.objects.create(paper=paper, question_number=1, page_number=1,
            question_text="Teacher prompt [[table:1]]", options=[{"key": "A", "text": "yes"}, {"key": "B", "text": "no"}],
            tables=[{"columns": 2, "rows": [[cell("Teacher header", 2, header=True)], [cell("Left"), cell("Right")]]}])
        env = os.environ.copy()
        env.update(UPGRADE_TEST_URL=self.live_server_url, UPGRADE_TEST_PDF=str(source), UPGRADE_TEST_TEACHER=json.dumps({
            "username": teacher.username, "password": "TeacherBrowserPass987!", "review": reverse("questions:paper_review", args=[paper.pk]),
            "edit": reverse("questions:extracted_edit", args=[question.pk]), "ai": reverse("questions:answer_suggestion", args=[question.pk]),
        }))
        script = Path(settings.BASE_DIR) / "tests" / "browser" / "upgrade-smoke.cjs"
        with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
            provider.return_value.calls = 2
            provider.return_value.suggest.return_value = {"option_key": "A", "confidence": Decimal("0.910"),
                "explanation": "Mock reasoning", "uncertainty": "Check the original.", "prompt_tokens": 100, "output_tokens": 50,
                "estimated_cost_usd": None}
            result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, f"Upgrade browser test failed:\n{result.stdout}\n{result.stderr}")
            self.assertIn("BROWSER_UPGRADE_OK", result.stdout)
            self.assertEqual(provider.return_value.suggest.call_count, 4)
            self.assertTrue(all(call.args[1] == [] for call in provider.return_value.suggest.call_args_list))  # text-only consent


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium Gemini-only test")
class BrowserGeminiOnlySmokeTests(StaticLiveServerTestCase):
    """Mobile consent and private-practice flow; all provider responses are fake."""
    @classmethod
    def setUpClass(cls):
        from .test_gemini_only import GEMINI_ONLY
        cls.workspace = tempfile.TemporaryDirectory(prefix="gemini-only-browser-")
        cls.addClassCleanup(cls.workspace.cleanup)
        root = Path(cls.workspace.name)
        cls.enterClassContext(override_settings(
            **{**GEMINI_ONLY, "PRIVATE_PRACTICE_ROOT": str(root / "private"), "MEDIA_ROOT": str(root / "media"),
               "RAZORPAY_KEY_ID": "", "RAZORPAY_KEY_SECRET": "", "RAZORPAY_WEBHOOK_SECRET": ""}
        ))
        super().setUpClass()

    def test_lower_cost_consent_review_and_practice_creation_in_chromium(self):
        from unittest.mock import patch
        from .test_cloud_pipeline import synthetic_pdf
        from .test_gemini_only import fake_google_request

        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install Node and browser dependencies first.")
        source = Path(self.workspace.name) / "synthetic.pdf"
        source.write_bytes(synthetic_pdf())
        env = os.environ.copy()
        env.update(GEMINI_ONLY_TEST_URL=self.live_server_url, GEMINI_ONLY_TEST_PDF=str(source))
        script = Path(settings.BASE_DIR) / "tests" / "browser" / "gemini-only-smoke.cjs"
        calls = []
        with patch("apps.questions.cloud_providers.requests.Session.request", side_effect=fake_google_request(calls)):
            result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, f"Gemini-only browser test failed:\n{result.stdout}\n{result.stderr}")
        self.assertIn("GEMINI_ONLY_BROWSER_OK", result.stdout)
        self.assertEqual([method for method, _, _ in calls], ["GET", "POST"])
        self.assertTrue(all("mathpix" not in url for _, url, _ in calls))


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium local-improvements test")
class BrowserLocalImprovementsTests(StaticLiveServerTestCase):
    """Cloud-error switching and local estimate UI with a synthetic loopback stub."""
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix="local-improvements-browser-")
        cls.addClassCleanup(cls.workspace.cleanup)
        root = Path(cls.workspace.name)
        cls.enterClassContext(override_settings(
            PRIVATE_PRACTICE_ROOT=str(root / "private"), MEDIA_ROOT=str(root / "media"),
            CELERY_TASK_ALWAYS_EAGER=True, PDF_CLOUD_ENABLED=False, PDF_GEMINI_ENABLED=False,
            LOCAL_PRACTICE_AI_ENABLED=True, LOCAL_PRACTICE_AI_MODEL="test-local:latest",
            LOCAL_PRACTICE_AI_TIMEOUT="5", LOCAL_PRACTICE_AI_MAX_QUESTIONS="300",
            PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
        ))
        super().setUpClass()

    def test_local_upload_switch_and_unverified_ai_ui(self):
        from django.urls import reverse
        from apps.accounts.models import User
        from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion, ExamResponse
        from .models import Question, QuestionOption, PracticeLocalEstimate
        from .test_local_practice_ai import LocalStub

        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install Node and browser dependencies first.")
        stub = LocalStub(); self.addCleanup(stub.close)
        student = User.objects.create_user(username="local_browser_student", email="localbrowser@example.test",
                    password="BrowserPracticePass987!", role=User.Roles.STUDENT)
        category = Category.objects.create(name="Browser local", slug="browser-local")
        exam = Exam.objects.create(title="Browser ungraded test", slug="browser-ungraded-test", category=category,
                                   creator=student, is_private_practice=True)
        question = Question.objects.create(category=category, creator=student, is_private_practice=True,
                                           subject="Math", question_text="What is two plus two?")
        QuestionOption.objects.create(question=question, option_key="A", option_text="Three", order=0)
        selected = QuestionOption.objects.create(question=question, option_key="B", option_text="Four", order=1)
        exam_question = ExamQuestion.objects.create(exam=exam, question=question, order=1)
        attempt = ExamAttempt.objects.create(exam=exam, student=student, status=ExamAttempt.AttemptStatus.SUBMITTED)
        response = ExamResponse.objects.create(attempt=attempt, exam_question=exam_question, selected_option=selected, order=1)
        source = Path(self.workspace.name) / "synthetic.pdf"; source.write_bytes(make_two_column_pdf())
        env = os.environ.copy()
        env.update(LOCAL_IMPROVE_URL=self.live_server_url, LOCAL_IMPROVE_PDF=str(source),
                   LOCAL_IMPROVE_RESULT_PATH=reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        script = Path(settings.BASE_DIR) / "tests" / "browser" / "local-improvements-smoke.cjs"
        with override_settings(LOCAL_PRACTICE_AI_PORT=str(stub.port)):
            result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, f"Local browser test failed:\n{result.stdout}\n{result.stderr}")
        self.assertIn("LOCAL_IMPROVEMENTS_BROWSER_OK", result.stdout)
        self.assertEqual([entry[1] for entry in stub.calls], ["/api/status", "/api/tags", "/api/generate"])
        self.assertTrue(PracticeLocalEstimate.objects.get(attempt=attempt).results[0]["match"])
        response.refresh_from_db(); question.refresh_from_db()
        self.assertIsNone(response.is_correct); self.assertFalse(question.key_verified)


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium source-image test")
class BrowserSourceSnapshotTests(StaticLiveServerTestCase):
    """A locally extracted synthetic PDF reaches the mobile CBT as source crops."""
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix="source-browser-test-")
        cls.addClassCleanup(cls.workspace.cleanup)
        root = Path(cls.workspace.name)
        cls.enterClassContext(override_settings(
            PRIVATE_PRACTICE_ROOT=str(root / "private"), MEDIA_ROOT=str(root / "media"),
            CELERY_TASK_ALWAYS_EAGER=True, PDF_CLOUD_ENABLED=False,
            PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
        ))
        super().setUpClass()

    def test_source_snapshots_render_after_extraction_and_resume(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.urls import reverse
        from apps.accounts.models import User
        from apps.exams.models import Category, ExamAttempt
        from .extractor import PDFQuestionExtractor
        from .models import QuestionPaperUpload
        from .test_source_snapshots import math_pdf

        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install Node and browser dependencies first.")
        student = User.objects.create_user(username="source_browser_student", email="source-browser@example.test",
            password="SourceBrowserPass987!", role=User.Roles.STUDENT)
        category = Category.objects.create(name="Browser source", slug="browser-source")
        doc = math_pdf(); content = doc.write(); doc.close()
        paper = QuestionPaperUpload.objects.create(title="Browser source", category=category, uploaded_by=student,
            is_student_private=True, subject="Math",
            private_pdf_file=SimpleUploadedFile("source.pdf", content, content_type="application/pdf"))
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 2)
        self.assertTrue(paper.extracted_questions.get(question_number=1).figures.filter(source="option-source").exists())
        self.client.force_login(student)
        self.assertEqual(self.client.post(reverse("questions:practice_settings", args=[paper.pk]),
                         {"duration_minutes": 20, "acknowledge_review": "on"}).status_code, 302)
        paper.refresh_from_db()
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[paper.practice_exam.slug])).status_code, 302)
        attempt = ExamAttempt.objects.get(exam=paper.practice_exam)
        expires_at = attempt.expires_at
        env = os.environ.copy()
        env.update(SOURCE_TEST_URL=self.live_server_url,
                   SOURCE_TEST_SESSION_PATH=reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        script = Path(settings.BASE_DIR) / "tests" / "browser" / "source-snapshot-smoke.cjs"
        result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, f"Source browser test failed:\n{result.stdout}\n{result.stderr}")
        self.assertIn("SOURCE_SNAPSHOT_BROWSER_OK", result.stdout)
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, expires_at)

    def test_parallel_source_renders_and_original_choice_key_survives_reload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.urls import reverse
        from apps.accounts.models import User
        from apps.exams.models import Category, ExamAttempt
        from .extractor import PDFQuestionExtractor
        from .models import QuestionPaperUpload
        from .test_source_snapshots import parallel_pdf

        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install Node and browser dependencies first.")
        student = User.objects.create_user(username="source_browser_student", email="source-parallel@example.test",
            password="SourceBrowserPass987!", role=User.Roles.STUDENT)
        category = Category.objects.create(name="Browser parallel", slug="browser-parallel")
        doc = parallel_pdf(); content = doc.write(); doc.close()
        paper = QuestionPaperUpload.objects.create(title="Browser parallel", category=category, uploaded_by=student,
            is_student_private=True, subject="Math",
            private_pdf_file=SimpleUploadedFile("parallel.pdf", content, content_type="application/pdf"))
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 3)
        self.assertTrue(paper.extracted_questions.get(question_number=1).figures.filter(source="parallel-source").exists())
        self.client.force_login(student)
        self.assertEqual(self.client.post(reverse("questions:practice_settings", args=[paper.pk]),
                         {"duration_minutes": 20, "acknowledge_review": "on", "answer_key_text": "1: 2"}).status_code, 302)
        paper.refresh_from_db()
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[paper.practice_exam.slug])).status_code, 302)
        attempt = ExamAttempt.objects.get(exam=paper.practice_exam)
        expires_at = attempt.expires_at
        env = os.environ.copy()
        env.update(SOURCE_TEST_URL=self.live_server_url, SOURCE_TEST_PARALLEL="1",
                   SOURCE_TEST_SESSION_PATH=reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        script = Path(settings.BASE_DIR) / "tests" / "browser" / "source-snapshot-smoke.cjs"
        result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, f"Parallel-source browser test failed:\n{result.stdout}\n{result.stderr}")
        self.assertIn("SOURCE_SNAPSHOT_BROWSER_OK", result.stdout)
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, expires_at)
        response = attempt.responses.order_by("order").first()
        self.assertEqual(response.selected_option.option_key, "B")
        self.assertTrue(response.exam_question.question.key_verified)
