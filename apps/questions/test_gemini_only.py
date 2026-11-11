"""Lower-cost cloud mode tests. Every provider call is mocked; no accuracy claims."""
import io
import json
import os
import tempfile
from unittest.mock import Mock, patch

import pymupdf as fitz
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.models import Category
from apps.payments.models import PracticePlan, PracticeUsage
from .test_helpers import response_body
from .cloud_extractor import CloudPDFQuestionExtractor, build_gemini_only_pages
from .cloud_providers import CloudExtractionError, GeminiClient, gemini_only_is_configured
from .forms import QuestionPaperUploadForm
from .models import ExtractedQuestion, QuestionPaperUpload
from .practice_forms import PracticeUploadForm
from .test_cloud_pipeline import Response, gemini_questions, synthetic_pdf


GEMINI_ONLY = {
    "PDF_GEMINI_ENABLED": True,
    "PDF_GEMINI_MODEL": "mock-low-cost-model",
    "GEMINI_API_KEY": "fake-key",
    "PDF_CLOUD_ENABLED": False,
    "MATHPIX_APP_ID": "",
    "MATHPIX_APP_KEY": "",
    "GEMINI_MODEL": "",
    "PDF_CLOUD_MAX_PAGES": 10,
    "PDF_GEMINI_MAX_OUTPUT_TOKENS": 32768,
    "PDF_GEMINI_THINKING_LEVEL": "minimal",
    "CELERY_TASK_ALWAYS_EAGER": True,
    "PASSWORD_HASHERS": ["django.contrib.auth.hashers.MD5PasswordHasher"],
}


def upload():
    return SimpleUploadedFile("paper.pdf", synthetic_pdf(), content_type="application/pdf")


def fake_google_request(calls, fail_generation=False):
    def handle(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if "mathpix" in url:
            raise AssertionError("Gemini-only mode must not contact Mathpix")
        if method == "GET" and url.endswith("/models/mock-low-cost-model"):
            return Response({"name": "models/mock-low-cost-model", "supportedGenerationMethods": ["generateContent"], "outputTokenLimit": 16384})
        if method == "POST" and url.endswith("mock-low-cost-model:generateContent"):
            if fail_generation:
                return Response({"error": "fake-key and document content"}, 403)
            return Response({
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"questions": gemini_questions()})}]}}],
                "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 500, "thoughtsTokenCount": 50},
            })
        raise AssertionError(f"Unexpected mock request: {method} {url}")
    return handle


@override_settings(**GEMINI_ONLY)
class GeminiOnlyProtocolTests(SimpleTestCase):
    def test_requires_own_switch_model_and_key_not_mathpix(self):
        self.assertTrue(gemini_only_is_configured())
        for changes in ({"PDF_GEMINI_ENABLED": False}, {"PDF_GEMINI_MODEL": ""},
                        {"PDF_GEMINI_MODEL": "../../model"}, {"PDF_GEMINI_MODEL": 123}, {"GEMINI_API_KEY": ""},
                        {"PDF_GEMINI_THINKING_LEVEL": "nonsense"}, {"PDF_GEMINI_MAX_OUTPUT_TOKENS": 256},
                        {"PDF_GEMINI_MIN_INTERVAL_SECONDS": -1}, {"PDF_GEMINI_MIN_INTERVAL_SECONDS": float("nan")},
                        {"PDF_GEMINI_MIN_INTERVAL_SECONDS": 121}):
            with self.subTest(changes=changes), override_settings(**changes):
                self.assertFalse(gemini_only_is_configured())

    def test_local_pages_keep_original_embedded_candidates_without_ocr(self):
        logs = []
        with fitz.open(stream=synthetic_pdf(), filetype="pdf") as document:
            pages, figures = build_gemini_only_pages(document, logs.append)
        self.assertEqual(len(pages), 2)
        self.assertIn("Q1", " ".join(line["text"] for line in pages[0]["lines"]))
        self.assertEqual(len(figures), 5)
        self.assertTrue(all(item["source"] == "embedded" for item in figures.values()))
        self.assertTrue(any("Mathpix is not contacted" in line for line in logs))

    def test_scanned_and_rotated_pages_warn_and_do_not_automatically_crop(self):
        image = io.BytesIO()
        Image.new("RGB", (60, 80), "white").save(image, format="PNG")
        doc = fitz.open()
        scan = doc.new_page(width=600, height=800)
        scan.insert_image(scan.rect, stream=image.getvalue())
        rotated = doc.new_page(width=600, height=800)
        rotated.insert_image(fitz.Rect(80, 100, 200, 220), stream=image.getvalue())
        rotated.set_rotation(90)
        data = doc.write(); doc.close()
        logs = []
        with fitz.open(stream=data, filetype="pdf") as document:
            pages, candidates = build_gemini_only_pages(document, logs.append)
        self.assertEqual(candidates, {})
        self.assertFalse(pages[0]["lines"])
        self.assertTrue(any("little embedded text" in line for line in logs))
        self.assertTrue(any("rotated page" in line for line in logs))

    def test_gemini_uses_local_source_prompt_budget_and_reports_usage(self):
        calls = []
        session = Mock()
        session.request.side_effect = fake_google_request(calls)
        client = GeminiClient(session=session, model="mock-low-cost-model", local_source=True)
        client.check_model()
        self.assertEqual(client.interpret(1, 2, [{"page": 1}], [(1, b"preview")]), gemini_questions())
        body = calls[-1][2]["json"]
        self.assertIn("locally extracted PDF text", body["contents"][0]["parts"][0]["text"])
        self.assertNotIn("Mathpix OCR", body["contents"][0]["parts"][0]["text"])
        self.assertEqual(body["generationConfig"]["thinkingConfig"]["thinkingLevel"], "minimal")
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 16384)
        self.assertIn("1200 input, 500 response and 50 thinking tokens", client.usage_summary())

    @override_settings(PDF_CLOUD_THINKING_LEVEL="high", PDF_CLOUD_MAX_OUTPUT_TOKENS=512,
                       PDF_GEMINI_THINKING_LEVEL="minimal", PDF_GEMINI_MAX_OUTPUT_TOKENS=8192)
    def test_generation_settings_do_not_change_the_existing_mathpix_mode(self):
        budget = GeminiClient(local_source=True)
        premium = GeminiClient(local_source=False)
        self.assertEqual((budget.thinking, budget.maximum), ("minimal", 8192))
        self.assertEqual((premium.thinking, premium.maximum), ("high", 512))

    def test_missing_usage_and_failed_request_are_never_reported_as_free(self):
        session = Mock()
        session.request.return_value = Response({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]})
        client = GeminiClient(session=session, model="mock-low-cost-model", local_source=True)
        with self.assertRaises(CloudExtractionError):
            client.interpret(1, 1, [], [])
        self.assertIn("did not report usable token counts", client.usage_summary())


@override_settings(**GEMINI_ONLY)
class GeminiOnlyWorkflowTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.override = override_settings(
            MEDIA_ROOT=os.path.join(self.root.name, "public"),
            STATIC_ROOT=os.path.join(self.root.name, "static"),
            PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private"),
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.category = Category.objects.create(name="Tests", slug="tests", is_active=True)
        self.teacher = User.objects.create_user(username="teacher_low", email="teacher_low@example.test", role=User.Roles.TEACHER, is_staff=True)
        self.student = User.objects.create_user(username="student_low", email="student_low@example.test", role=User.Roles.STUDENT)
        self.other = User.objects.create_user(username="other_low", email="other_low@example.test", role=User.Roles.STUDENT)
        self.client.force_login(self.teacher)

    def paper(self, **kwargs):
        data = dict(title="Gemini only", category=self.category, uploaded_by=self.teacher,
                    pdf_file=upload(), extraction_mode="gemini", cloud_consent_at=timezone.now())
        data.update(kwargs)
        return QuestionPaperUpload.objects.create(**data)

    def test_both_upload_forms_require_consent_configuration_and_page_limit(self):
        def teacher_form(**extra):
            return QuestionPaperUploadForm(data={"title": "Paper", "category": self.category.pk, "extraction_mode": "gemini", **extra}, files={"pdf_file": upload()}, user=self.teacher)
        def student_form(**extra):
            return PracticeUploadForm(data={"title": "Paper", "extraction_mode": "gemini", **extra}, files={"pdf_file": upload()})
        for factory in (teacher_form, student_form):
            form = factory()
            self.assertFalse(form.is_valid())
            self.assertIn("cloud_consent", form.errors)
            self.assertTrue(factory(cloud_consent="on").is_valid())
            with override_settings(PDF_GEMINI_ENABLED=False):
                self.assertIn("extraction_mode", factory(cloud_consent="on").errors)
            with override_settings(PDF_CLOUD_MAX_PAGES=1):
                self.assertIn("pdf_file", factory(cloud_consent="on").errors)

    def test_teacher_screen_and_queued_upload_record_selected_mode(self):
        response = self.client.get(reverse("questions:paper_upload"))
        self.assertContains(response, "Gemini only (lower-cost cloud)")
        self.assertContains(response, "Mathpix is not used")
        with override_settings(CELERY_TASK_ALWAYS_EAGER=False), patch("apps.questions.tasks.extract_cloud_paper.delay") as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("questions:paper_upload"), {
                "title": "Teacher low cost", "category": self.category.pk,
                "extraction_mode": "gemini", "cloud_consent": "on", "pdf_file": upload(),
            })
        self.assertEqual(response.status_code, 302)
        paper = QuestionPaperUpload.objects.get(title="Teacher low cost")
        self.assertEqual(paper.extraction_mode, "gemini")
        self.assertIsNotNone(paper.cloud_consent_at)
        delay.assert_called_once_with(paper.pk)

    def test_full_mocked_pipeline_sends_only_to_google_and_saves_reviewable_original_crops(self):
        paper = self.paper()
        calls = []
        with patch("apps.questions.cloud_providers.requests.Session.request", side_effect=fake_google_request(calls)), patch("apps.questions.cloud_extractor.MathpixClient") as mathpix:
            self.assertEqual(CloudPDFQuestionExtractor(paper).extract(), 2)
        mathpix.assert_not_called()
        self.assertEqual([method for method, _, _ in calls], ["GET", "POST"])
        parts = calls[-1][2]["json"]["contents"][0]["parts"]
        self.assertEqual(len([part for part in parts if "inlineData" in part]), 2)
        self.assertIn("Q1", parts[1]["text"])
        paper.refresh_from_db()
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.EXTRACTED)
        self.assertEqual(paper.mathpix_document_id, "")
        self.assertEqual(paper.figures.count(), 5)
        self.assertTrue(all(q.is_flagged and not q.correct_option for q in paper.extracted_questions.all()))
        self.assertIn("1200 input, 500 response and 50 thinking tokens", paper.extraction_log)
        self.assertIn("Mathpix is not contacted", paper.extraction_log)
        with patch("apps.questions.cloud_providers.requests.Session.request") as request:
            self.assertEqual(CloudPDFQuestionExtractor(paper).extract(), 0)
            request.assert_not_called()

    @override_settings(PDF_GEMINI_MIN_INTERVAL_SECONDS=1)
    def test_optional_per_job_pacing_waits_between_requests_without_retrying(self):
        doc = fitz.open()
        for _ in range(3):
            doc.new_page(width=600, height=800)
        data = doc.write(); doc.close()
        paper = self.paper(pdf_file=SimpleUploadedFile("three.pdf", data, content_type="application/pdf"))
        gemini = Mock()
        def item(number, page):
            return [{"number": number, "page_number": page, "end_page": page, "text": "Question",
                     "image_ids": [], "issues": [], "options": [
                         {"key": "A", "text": "Yes", "image_ids": []},
                         {"key": "B", "text": "No", "image_ids": []}]}]
        gemini.interpret.side_effect = [item(1, 1), item(2, 3)]
        with patch("apps.questions.cloud_extractor.time.monotonic", side_effect=[0.0, 0.2, 1.0]), patch("apps.questions.cloud_extractor.time.sleep") as sleep:
            self.assertEqual(CloudPDFQuestionExtractor(paper, gemini=gemini).extract(), 2)
        self.assertEqual(gemini.interpret.call_count, 2)
        sleep.assert_called_once_with(0.8)

    def test_worker_rechecks_consent_and_configuration_and_never_resends(self):
        for kwargs in ({"cloud_consent_at": None}, {"extraction_mode": "local"}):
            paper = self.paper(**kwargs)
            with patch("apps.questions.cloud_providers.requests.Session.request") as request:
                self.assertEqual(CloudPDFQuestionExtractor(paper).extract(), 0)
                request.assert_not_called()
        paper = self.paper()
        with override_settings(PDF_GEMINI_ENABLED=False), patch("apps.questions.cloud_providers.requests.Session.request") as request:
            self.assertEqual(CloudPDFQuestionExtractor(paper).extract(), 0)
            request.assert_not_called()
        paper = self.paper()
        ExtractedQuestion.objects.create(paper=paper, question_number=77, question_text="Preserved teacher edit")
        with patch("apps.questions.cloud_providers.requests.Session.request") as request:
            self.assertEqual(CloudPDFQuestionExtractor(paper).extract(), 0)
            request.assert_not_called()
        self.assertEqual(paper.extracted_questions.get().question_text, "Preserved teacher edit")

    def test_provider_failure_saves_no_partial_results_or_secrets(self):
        paper = self.paper()
        calls = []
        with patch("apps.questions.cloud_providers.requests.Session.request", side_effect=fake_google_request(calls, fail_generation=True)):
            self.assertEqual(CloudPDFQuestionExtractor(paper).extract(), 0)
        paper.refresh_from_db()
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.FAILED)
        self.assertFalse(paper.extracted_questions.exists())
        self.assertFalse(paper.figures.exists())
        self.assertNotIn("fake-key", paper.extraction_log)
        self.assertIn("token counts", paper.extraction_log)
        self.assertEqual([method for method, _, _ in calls], ["GET", "POST"])

    def test_student_private_cloud_feature_quota_and_storage(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("questions:practice_upload"))
        self.assertContains(response, "Gemini only (lower-cost cloud)")
        self.assertContains(response, "not Mathpix")
        plan = PracticePlan.objects.get(is_default=True)
        plan.allow_cloud_extraction = False
        plan.save(update_fields=["allow_cloud_extraction"])
        with patch("apps.questions.practice_views.extract_cloud_paper") as task:
            response = self.client.post(reverse("questions:practice_upload"), {
                "title": "Blocked", "extraction_mode": "gemini", "cloud_consent": "on", "pdf_file": upload(),
            })
            self.assertEqual(response.status_code, 302)
            task.assert_not_called()
        self.assertFalse(QuestionPaperUpload.objects.filter(uploaded_by=self.student).exists())
        self.assertFalse(PracticeUsage.objects.filter(user=self.student).exists())
        plan.allow_cloud_extraction = True
        plan.save(update_fields=["allow_cloud_extraction"])
        calls = []
        with patch("apps.questions.cloud_providers.requests.Session.request", side_effect=fake_google_request(calls)):
            response = self.client.post(reverse("questions:practice_upload"), {
                "title": "Private low cost", "extraction_mode": "gemini", "cloud_consent": "on", "pdf_file": upload(),
            })
        self.assertEqual(response.status_code, 302)
        paper = QuestionPaperUpload.objects.get(title="Private low cost")
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.EXTRACTED, paper.extraction_log)
        self.assertTrue(paper.is_student_private and paper.private_pdf_file and not paper.pdf_file)
        self.assertTrue(all(figure.private_image and not figure.image for figure in paper.figures.all()))
        self.assertEqual(PracticeUsage.objects.get(user=self.student).pdf_uploads, 1)
        review = self.client.get(reverse("questions:practice_settings", args=[paper.pk]))
        self.assertContains(review, "1200 input, 500 response and 50 thinking tokens")
        self.assertContains(review, "not a price or an amount you owe")
        figure = paper.figures.first()
        url = reverse("questions:practice_figure_file", args=[figure.pk])
        owner = self.client.get(url)
        self.assertEqual(owner.status_code, 200)
        self.assertTrue(response_body(owner))
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
