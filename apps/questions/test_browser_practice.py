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
