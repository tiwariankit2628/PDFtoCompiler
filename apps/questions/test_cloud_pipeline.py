"""Provider and teacher-workflow tests: all PDFs and provider responses are synthetic.

No paid service is contacted and these tests do not measure extraction accuracy.
"""
import io
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pymupdf as fitz
import requests
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion, ExamResponse
from .cloud_extractor import CloudPDFQuestionExtractor, build_pages, match_figures, validate_questions
from .cloud_providers import CloudExtractionError, GeminiClient, MathpixClient
from .extractor import PDFQuestionExtractor
from .forms import QuestionPaperUploadForm
from .models import ExtractedFigure, ExtractedQuestion, Question, QuestionFigure, QuestionOption, QuestionPaperUpload


CLOUD_SETTINGS = dict(PDF_CLOUD_ENABLED=True, MATHPIX_APP_ID="fake-id", MATHPIX_APP_KEY="fake-mathpix-key",
                      GEMINI_API_KEY="fake-gemini-key", GEMINI_MODEL="mock-vision-model", PDF_CLOUD_MAX_PAGES=10,
                      PDF_CLOUD_POLL_SECONDS=0.1, PDF_CLOUD_TIMEOUT_SECONDS=10)


def png(color):
    out = io.BytesIO()
    Image.new("RGB", (50, 40), color).save(out, format="PNG")
    return out.getvalue()


def synthetic_pdf():
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((30, 90), "Q1 Identify the figures.")
    page.insert_image(fitz.Rect(60, 120, 160, 180), stream=png("red"))
    page.insert_image(fitz.Rect(60, 240, 160, 300), stream=png("green"))
    page.insert_image(fitz.Rect(180, 240, 280, 300), stream=png("blue"))
    page.insert_image(fitz.Rect(270, 740, 320, 780), stream=png("black"))
    page = doc.new_page(width=600, height=800)
    page.insert_image(fitz.Rect(60, 70, 160, 130), stream=png("yellow"))
    page.insert_text((30, 250), "Q2 A second question?")
    page.insert_text((50, 280), "(1) Yes (2) No")
    result = doc.write()
    doc.close()
    return result


def region(x0, y0, x1, y1):
    # Mathpix rendered the same page at twice the PDF point dimensions.
    return {"top_left_x": x0 * 2, "top_left_y": y0 * 2, "width": (x1 - x0) * 2, "height": (y1 - y0) * 2}


def mathpix_result():
    return {"pages": [
        {"page": 1, "page_width": 1200, "page_height": 1600, "lines": [
            {"type": "text", "text": "Q1 plain", "text_display": "Q1 $x^2$ आंकड़ा", "confidence_rate": .7, "region": region(30, 70, 250, 100)},
            {"type": "diagram", "region": region(60, 120, 160, 180)},
            {"type": "diagram", "region": region(60, 240, 160, 300)},
            {"type": "diagram", "region": region(180, 240, 280, 300)},
            {"type": "diagram", "region": region(270, 740, 320, 780)},
        ]},
        {"page": 2, "page_width": 1200, "page_height": 1600, "lines": [
            {"type": "diagram", "region": region(60, 70, 160, 130)},
            {"type": "text", "text": "Q2 second", "confidence_rate": .99, "region": region(30, 230, 250, 260)},
        ]},
    ]}


def gemini_questions():
    return [
        {"number": 1, "page_number": 1, "end_page": 2, "text": "Identify $x^2$ / पहचानें",
         "image_ids": ["p1-r1", "p2-r1"], "issues": ["Check the continuation"], "options": [
             {"key": "A", "text": "", "image_ids": ["p1-r2"]},
             {"key": "B", "text": "", "image_ids": ["p1-r3"]},
         ]},
        {"number": 2, "page_number": 2, "end_page": 2, "text": "A second question?",
         "image_ids": [], "issues": [], "options": [
             {"key": "A", "text": "Yes", "image_ids": []},
             {"key": "B", "text": "No", "image_ids": []},
         ]},
    ]


class Response:
    def __init__(self, data=None, status=200):
        self.status_code, self.data = status, data

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


@override_settings(**CLOUD_SETTINGS)
class ProviderProtocolTests(SimpleTestCase):
    def test_mathpix_posts_generic_filename_waits_for_completed_and_deletes(self):
        session = Mock()
        session.request.side_effect = [Response({"pdf_id": "abc_123"}), Response({"status": "split", "percent_done": 100}),
                                       Response({"status": "completed"}), Response(mathpix_result()), Response({}, 204)]
        callback = Mock()
        client = MathpixClient(session=session, sleep=lambda _: None, on_submitted=callback)
        self.assertEqual(client.read_pdf(b"fake-pdf"), mathpix_result())
        callback.assert_called_once_with("abc_123")
        self.assertTrue(client.delete())
        calls = session.request.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["POST", "GET", "GET", "GET", "DELETE"])
        self.assertEqual(calls[0].kwargs["files"]["file"][0], "document.pdf")
        self.assertIn("math_inline_delimiters", json.loads(calls[0].kwargs["data"]["options_json"]))
        self.assertTrue(calls[3].args[1].endswith("abc_123.lines.json"))
        self.assertFalse(calls[0].kwargs["allow_redirects"])

    def test_provider_errors_do_not_disclose_key_or_response_body(self):
        session = Mock()
        session.request.return_value = Response({"error": "secret document fake-mathpix-key"}, 403)
        client = MathpixClient(session=session)
        with self.assertRaises(CloudExtractionError) as exc:
            client.read_pdf(b"pdf")
        self.assertIn("403", str(exc.exception))
        self.assertNotIn("fake-mathpix-key", str(exc.exception))
        self.assertNotIn("secret document", str(exc.exception))
        self.assertEqual(session.request.call_count, 1)  # no duplicate billable submission

    def test_mathpix_network_failure_is_safe_and_does_not_retry(self):
        session = Mock()
        session.request.side_effect = requests.RequestException("fake-mathpix-key")
        with self.assertRaises(CloudExtractionError) as exc:
            MathpixClient(session=session).read_pdf(b"pdf")
        self.assertNotIn("fake-mathpix-key", str(exc.exception))
        self.assertEqual(session.request.call_count, 1)

    def test_gemini_sends_multimodal_json_schema_and_parses_output(self):
        session = Mock()
        session.request.return_value = Response({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"questions": gemini_questions()})}]}}]})
        result = GeminiClient(session=session).interpret(1, 2, [{"page": 1}], [(1, b"image-bytes")])
        self.assertEqual(result, gemini_questions())
        args, kwargs = session.request.call_args
        self.assertIn("mock-vision-model:generateContent", args[1])
        self.assertNotIn("fake-gemini-key", args[1])
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "fake-gemini-key")
        self.assertEqual(kwargs["json"]["generationConfig"]["responseFormat"]["text"]["mimeType"], "APPLICATION_JSON")
        self.assertEqual(kwargs["json"]["contents"][0]["parts"][-1]["inlineData"]["mimeType"], "image/png")
        self.assertFalse(kwargs["allow_redirects"])

    @override_settings(PDF_CLOUD_THINKING_LEVEL="high", PDF_CLOUD_MAX_OUTPUT_TOKENS=32768)
    def test_gemini_preflight_limits_budget_and_uses_optional_thinking(self):
        session = Mock()
        session.request.side_effect = [Response({"supportedGenerationMethods": ["generateContent"], "outputTokenLimit": 4096}),
            Response({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"questions": []})}]}}]})]
        client = GeminiClient(session=session); client.check_model(); client.interpret(1, 1, [], [])
        config = session.request.call_args.kwargs["json"]["generationConfig"]
        self.assertEqual(config["maxOutputTokens"], 4096)
        self.assertEqual(config["thinkingConfig"]["thinkingLevel"], "high")
        self.assertNotIn("temperature", config)

    def test_gemini_rejects_truncation_and_invalid_json(self):
        session = Mock()
        session.request.return_value = Response({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{}"}]}}]})
        with self.assertRaisesRegex(CloudExtractionError, "incomplete"):
            GeminiClient(session=session).interpret(1, 1, [], [])
        session.request.return_value = Response({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "not json"}]}}]})
        with self.assertRaises(CloudExtractionError):
            GeminiClient(session=session).interpret(1, 1, [], [])

    def test_geometry_and_shared_or_unknown_image_ids_are_conservative(self):
        logs = []
        with fitz.open(stream=synthetic_pdf(), filetype="pdf") as doc:
            pages, candidates = build_pages(doc, mathpix_result(), logs.append)
        self.assertEqual(len(candidates), 5)  # embedded image duplicates were merged
        self.assertIn("$x^2$", pages[0]["lines"][0]["text"])
        self.assertTrue(pages[0]["low_confidence"])
        items = validate_questions(gemini_questions(), 1, 2, logs.append, 2)
        items[1]["image_ids"] = ["p1-r1", "invented"]
        assignments = match_figures(items, candidates)
        self.assertNotIn("p1-r1", assignments)  # shared IDs remain unassigned
        self.assertTrue(any("invalid or shared" in issue for issue in items[0]["issues"]))
        self.assertTrue(any("invalid or shared" in issue for issue in items[1]["issues"]))

    def test_mismatched_mathpix_page_geometry_withholds_ocr_crops(self):
        data = mathpix_result()
        data["pages"][0]["page_width"] = 1600
        logs = []
        with fitz.open(stream=synthetic_pdf(), filetype="pdf") as doc:
            pages, candidates = build_pages(doc, data, logs.append)
        self.assertFalse(pages[0]["coordinates_align"])
        self.assertTrue(any("coordinates do not align" in log for log in logs))
        # The independent embedded-image geometry is still available.
        self.assertTrue(all(c["source"] == "embedded" for c in candidates.values() if c["page"] == 1))

    def test_invalid_continuation_page_is_rejected(self):
        questions = gemini_questions()
        questions[0]["end_page"] = 3
        with self.assertRaises(CloudExtractionError):
            validate_questions(questions, 1, 2, lambda _: None, 2)


@override_settings(**CLOUD_SETTINGS)
class CloudWorkflowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_media = tempfile.TemporaryDirectory()
        cls.media_override = override_settings(MEDIA_ROOT=cls.temp_media.name)
        cls.media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.media_override.disable()
        cls.temp_media.cleanup()

    def setUp(self):
        self.category = Category.objects.create(name="Science", slug="science", is_active=True)
        self.teacher = User.objects.create_user(username="cloud_teacher", email="cloud_teacher@example.test", role=User.Roles.TEACHER, is_staff=True)
        self.teacher.assigned_categories.add(self.category)
        self.other = User.objects.create_user(username="other_teacher", email="other_teacher@example.test", role=User.Roles.TEACHER, is_staff=True)
        self.student = User.objects.create_user(username="cloud_student", email="cloud_student@example.test", role=User.Roles.STUDENT)
        self.client.force_login(self.teacher)

    def paper(self, **kwargs):
        defaults = dict(title="Synthetic paper", category=self.category, subject="Science", uploaded_by=self.teacher,
                        pdf_file=SimpleUploadedFile("synthetic.pdf", synthetic_pdf(), content_type="application/pdf"),
                        total_pages=2, extraction_mode=QuestionPaperUpload.ExtractionMode.CLOUD,
                        cloud_consent_at=timezone.now())
        defaults.update(kwargs)
        return QuestionPaperUpload.objects.create(**defaults)

    def run_cloud(self, paper=None, questions=None):
        paper = paper or self.paper()
        mathpix = Mock(pdf_id="mock-id")
        mathpix.read_pdf.return_value = mathpix_result()
        mathpix.delete.return_value = True
        gemini = Mock()
        gemini.interpret.return_value = gemini_questions() if questions is None else questions
        count = CloudPDFQuestionExtractor(paper, mathpix, gemini).extract()
        paper.refresh_from_db()
        return paper, mathpix, gemini, count

    def test_upload_requires_consent_configuration_and_page_limit(self):
        base = {"title": "Cloud", "category": self.category.pk, "extraction_mode": "cloud"}
        def form(data):
            return QuestionPaperUploadForm(data=data, files={"pdf_file": SimpleUploadedFile("file.pdf", synthetic_pdf(), content_type="application/pdf")}, user=self.teacher)
        missing_consent = form(base)
        self.assertFalse(missing_consent.is_valid())
        self.assertIn("cloud_consent", missing_consent.errors)
        with override_settings(PDF_CLOUD_ENABLED=False):
            not_enabled = form({**base, "cloud_consent": "on"})
            self.assertFalse(not_enabled.is_valid())
            self.assertIn("extraction_mode", not_enabled.errors)
        with override_settings(PDF_CLOUD_MAX_PAGES=1):
            too_long = form({**base, "cloud_consent": "on"})
            self.assertFalse(too_long.is_valid())
            self.assertIn("pdf_file", too_long.errors)
        self.assertTrue(form({**base, "cloud_consent": "on"}).is_valid())

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_cloud_upload_records_consent_and_queues_only_after_commit(self):
        with patch("apps.questions.tasks.extract_cloud_paper.delay") as delay, self.captureOnCommitCallbacks(execute=True):
            result = self.client.post(reverse("questions:paper_upload"), {
                "title": "Cloud", "category": self.category.pk, "extraction_mode": "cloud", "cloud_consent": "on",
                "pdf_file": SimpleUploadedFile("file.pdf", synthetic_pdf(), content_type="application/pdf"),
            })
        self.assertEqual(result.status_code, 302)
        paper = QuestionPaperUpload.objects.get(title="Cloud")
        self.assertIsNotNone(paper.cloud_consent_at)
        self.assertEqual(paper.total_pages, 2)
        delay.assert_called_once_with(paper.pk)

    def test_full_pipeline_uses_provider_protocols_with_mocked_http(self):
        paper = self.paper()
        requests_seen = []
        def fake_request(method, url, **kwargs):
            requests_seen.append((method, url))
            if method == "GET" and url.endswith("/models/mock-vision-model"):
                return Response({"name": "models/mock-vision-model", "supportedGenerationMethods": ["generateContent"]})
            if method == "POST" and url.endswith("/v3/pdf"):
                return Response({"pdf_id": "fake_document_123"})
            if method == "GET" and url.endswith("/fake_document_123"):
                return Response({"status": "completed"})
            if method == "GET" and url.endswith("/fake_document_123.lines.json"):
                return Response(mathpix_result())
            if method == "DELETE" and url.endswith("/fake_document_123"):
                return Response({}, 204)
            if method == "POST" and ":generateContent" in url:
                return Response({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"questions": gemini_questions()})}]}}]})
            raise AssertionError(f"Unexpected request {method} {url}")
        with patch("apps.questions.cloud_providers.requests.Session.request", side_effect=fake_request):
            self.assertEqual(CloudPDFQuestionExtractor(paper).extract(), 2)
        paper.refresh_from_db()
        self.assertEqual(paper.mathpix_document_id, "fake_document_123")
        self.assertIn("accepted the document deletion", paper.extraction_log)
        self.assertEqual([method for method, _ in requests_seen], ["GET", "POST", "GET", "GET", "DELETE", "POST"])

    def test_model_preflight_failure_does_not_submit_the_pdf(self):
        paper = self.paper()
        mathpix, gemini = Mock(pdf_id=None), Mock()
        gemini.check_model.side_effect = CloudExtractionError("Unavailable model")
        self.assertEqual(CloudPDFQuestionExtractor(paper, mathpix, gemini).extract(), 0)
        mathpix.read_pdf.assert_not_called()
        paper.refresh_from_db()
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.FAILED)

    def test_provider_reference_survives_failure_after_submission(self):
        paper = self.paper()
        def fake_request(method, url, **kwargs):
            if method == "GET" and url.endswith("/models/mock-vision-model"):
                return Response({"name": "models/mock-vision-model", "supportedGenerationMethods": ["generateContent"]})
            if method == "POST":
                return Response({"pdf_id": "known_document_id"})
            if method == "DELETE":
                return Response({}, 204)
            raise requests.RequestException("connection failed with fake-mathpix-key")
        with patch("apps.questions.cloud_providers.requests.Session.request", side_effect=fake_request):
            self.assertEqual(CloudPDFQuestionExtractor(paper).extract(), 0)
        paper.refresh_from_db()
        self.assertEqual(paper.mathpix_document_id, "known_document_id")
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.FAILED)
        self.assertNotIn("fake-mathpix-key", paper.extraction_log)
        self.assertIn("deletion", paper.extraction_log)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_queue_failure_is_visible_and_does_not_trigger_fallback(self):
        with patch("apps.questions.tasks.extract_cloud_paper.delay", side_effect=RuntimeError("broker secret")), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("questions:paper_upload"), {
                "title": "Queue failure", "category": self.category.pk, "extraction_mode": "cloud", "cloud_consent": "on",
                "pdf_file": SimpleUploadedFile("file.pdf", synthetic_pdf(), content_type="application/pdf"),
            })
        paper = QuestionPaperUpload.objects.get(title="Queue failure")
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.FAILED)
        self.assertIn("Could not queue", paper.extraction_log)
        self.assertNotIn("broker secret", paper.extraction_log)
        self.assertFalse(paper.extracted_questions.exists())

    def test_no_consent_existing_work_or_duplicate_task_never_calls_provider(self):
        paper = self.paper(cloud_consent_at=None)
        mathpix, gemini = Mock(pdf_id=None), Mock()
        self.assertEqual(CloudPDFQuestionExtractor(paper, mathpix, gemini).extract(), 0)
        mathpix.read_pdf.assert_not_called()
        paper.refresh_from_db()
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.FAILED)
        paper = self.paper()
        prior = ExtractedQuestion.objects.create(paper=paper, question_number=99, question_text="Teacher's edit")
        self.assertEqual(CloudPDFQuestionExtractor(paper, mathpix, gemini).extract(), 0)
        self.assertTrue(ExtractedQuestion.objects.filter(pk=prior.pk).exists())
        mathpix.read_pdf.assert_not_called()
        paper = self.paper(status=QuestionPaperUpload.ProcessingStatus.PROCESSING)
        self.assertEqual(CloudPDFQuestionExtractor(paper, mathpix, gemini).extract(), 0)
        mathpix.read_pdf.assert_not_called()

    def test_worker_rechecks_pdf_size_before_sending_anything(self):
        paper = self.paper()
        mathpix, gemini = Mock(pdf_id=None), Mock()
        with patch("apps.questions.cloud_extractor.MAX_PDF_BYTES", 100):
            self.assertEqual(CloudPDFQuestionExtractor(paper, mathpix, gemini).extract(), 0)
        mathpix.read_pdf.assert_not_called()
        paper.refresh_from_db()
        self.assertIn("no provider request was made", paper.extraction_log)

    def test_full_mock_pipeline_saves_multiple_original_images_options_and_review_notes(self):
        paper, mathpix, gemini, count = self.run_cloud()
        self.assertEqual(count, 2)
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.EXTRACTED)
        self.assertTrue(paper.ocr_used)
        mathpix.delete.assert_called_once()
        self.assertEqual(gemini.interpret.call_count, 1)
        self.assertEqual(len(gemini.interpret.call_args.args[3]), 2)  # original page images
        q = paper.extracted_questions.get(question_number=1)
        self.assertEqual(q.source_pages, [1, 2])
        self.assertEqual(q.correct_option, "")
        self.assertTrue(q.is_flagged)
        self.assertTrue(any("Low OCR confidence" in issue for issue in q.extraction_issues))
        self.assertEqual(q.figures.filter(option_key="").count(), 2)
        self.assertEqual(q.figures.filter(option_key="A").count(), 1)
        self.assertEqual(q.figures.filter(option_key="B").count(), 1)
        self.assertEqual(paper.figures.filter(question__isnull=True).count(), 1)
        red = q.figures.get(page_number=1, option_key="")
        with red.image.open("rb") as file, Image.open(file) as image:
            self.assertEqual(image.convert("RGB").getpixel((image.width // 2, image.height // 2)), (255, 0, 0))
        response = self.client.get(reverse("questions:paper_review", args=[paper.pk]))
        self.assertContains(response, "Original extraction notes")
        self.assertContains(response, "Option A image")
        self.assertContains(response, "Image pool (1)")

    def test_neighbor_pages_are_included_as_context_for_each_batch(self):
        doc = fitz.open()
        for _ in range(5):
            doc.new_page(width=600, height=800)
        five_pages = doc.write(); doc.close()
        paper = self.paper(pdf_file=SimpleUploadedFile("five.pdf", five_pages, content_type="application/pdf"), total_pages=5)
        mathpix = Mock(pdf_id="mock-id")
        mathpix.read_pdf.return_value = {"pages": [
            {"page": n, "page_width": 1200, "page_height": 1600, "lines": []} for n in range(1, 6)
        ]}
        mathpix.delete.return_value = True
        gemini = Mock()
        def make_item(number, page, end):
            return [{"number": number, "page_number": page, "end_page": end, "text": f"Question {number}",
                     "image_ids": [], "issues": [], "options": [
                         {"key": "A", "text": "Yes", "image_ids": []},
                         {"key": "B", "text": "No", "image_ids": []}]}]
        gemini.interpret.side_effect = [make_item(1, 2, 3), make_item(2, 3, 3), make_item(3, 5, 5)]
        self.assertEqual(CloudPDFQuestionExtractor(paper, mathpix, gemini).extract(), 3)
        calls = gemini.interpret.call_args_list
        self.assertEqual([(c.args[0], c.args[1]) for c in calls], [(1, 2), (3, 4), (5, 5)])
        self.assertEqual([[n for n, _ in c.args[3]] for c in calls], [[1, 2, 3], [2, 3, 4, 5], [4, 5]])
        self.assertEqual(paper.extracted_questions.get(question_number=1).source_pages, [2, 3])

    def test_explicit_ocr_question_missing_from_gemini_is_reported(self):
        paper, _, _, count = self.run_cloud(questions=gemini_questions()[:1])
        self.assertEqual(count, 1)
        self.assertIn("Explicit OCR labels missing from cloud output: 2", paper.extraction_log)

    def test_provider_or_schema_failure_saves_no_partial_results_and_deletes_mathpix(self):
        paper = self.paper()
        mathpix = Mock(pdf_id="mock-id")
        mathpix.read_pdf.return_value = mathpix_result()
        mathpix.delete.return_value = True
        gemini = Mock()
        gemini.interpret.side_effect = CloudExtractionError("Gemini request failed")
        self.assertEqual(CloudPDFQuestionExtractor(paper, mathpix, gemini).extract(), 0)
        paper.refresh_from_db()
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.FAILED)
        self.assertIn("Gemini request failed", paper.extraction_log)
        self.assertFalse(paper.extracted_questions.exists())
        self.assertFalse(paper.figures.exists())
        mathpix.delete.assert_called_once()

    def test_mathpix_error_still_attempts_deletion_and_warns_if_unsuccessful(self):
        paper = self.paper()
        mathpix = Mock(pdf_id="mock-id")
        mathpix.read_pdf.side_effect = CloudExtractionError("Mathpix failed")
        mathpix.delete.return_value = False
        self.assertEqual(CloudPDFQuestionExtractor(paper, mathpix, Mock()).extract(), 0)
        paper.refresh_from_db()
        self.assertIn("deletion request failed", paper.extraction_log)
        self.assertFalse(paper.extracted_questions.exists())

    def test_review_work_added_during_cloud_run_is_not_overwritten(self):
        paper = self.paper()
        mathpix = Mock(pdf_id="mock-id")
        mathpix.read_pdf.return_value = mathpix_result()
        mathpix.delete.return_value = True
        gemini = Mock()
        def concurrent_edit(*args):
            ExtractedQuestion.objects.create(paper=paper, question_number=88, question_text="Teacher work")
            return gemini_questions()
        gemini.interpret.side_effect = concurrent_edit
        self.assertEqual(CloudPDFQuestionExtractor(paper, mathpix, gemini).extract(), 0)
        self.assertEqual(list(paper.extracted_questions.values_list("question_text", flat=True)), ["Teacher work"])
        self.assertFalse(paper.figures.exists())

    def test_storage_and_database_rollback_on_figure_save_failure(self):
        paper = self.paper()
        mathpix = Mock(pdf_id="mock-id")
        mathpix.read_pdf.return_value = mathpix_result()
        mathpix.delete.return_value = True
        gemini = Mock()
        gemini.interpret.return_value = gemini_questions()
        original = ExtractedFigure.save
        calls = []
        def fail_on_second(instance, *args, **kwargs):
            calls.append(instance)
            if len(calls) == 2:
                raise RuntimeError("simulated disk/DB failure")
            return original(instance, *args, **kwargs)
        with patch.object(ExtractedFigure, "save", fail_on_second):
            self.assertEqual(CloudPDFQuestionExtractor(paper, mathpix, gemini).extract(), 0)
        self.assertFalse(paper.extracted_questions.exists())
        self.assertFalse(paper.figures.exists())
        for instance in calls:
            self.assertFalse(Path(instance.image.path).exists())

    def test_local_repeat_extraction_preserves_existing_staging(self):
        paper = self.paper(extraction_mode="local", cloud_consent_at=None,
                           status=QuestionPaperUpload.ProcessingStatus.EXTRACTED)
        original = ExtractedQuestion.objects.create(paper=paper, question_number=99, question_text="Already edited")
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 0)
        paper.refresh_from_db()
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.EXTRACTED)
        self.assertEqual(paper.extracted_questions.get().pk, original.pk)
        self.assertIn("preserved", paper.extraction_log)

    def test_manual_crop_upload_replace_pool_and_permissions(self):
        paper = self.paper()
        q = ExtractedQuestion.objects.create(paper=paper, question_number=1, question_text="Image question", page_number=1,
            options=[{"key": "A", "text": "Yes"}, {"key": "B", "text": "No"}])
        url = reverse("questions:figure_manage", args=[q.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        preview = self.client.get(reverse("questions:source_page_image", args=[q.pk, 1]))
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview["Content-Type"], "image/png")
        crop = {"action": "crop", "page_number": 1, "target": "A", "x0": ".1", "y0": ".15", "x1": ".2667", "y1": ".225"}
        self.assertEqual(self.client.post(url, crop).status_code, 302)
        figure = q.figures.get()
        self.assertEqual(figure.option_key, "A")
        self.assertEqual(figure.source, "manual_crop")
        with figure.image.open("rb") as file, Image.open(file) as image:
            self.assertEqual(image.convert("RGB").getpixel((image.width // 2, image.height // 2)), (255, 0, 0))
        q.refresh_from_db()
        self.assertTrue(q.is_flagged)
        self.client.post(url, {"action": "upload", "target": "B", "replace_id": figure.pk,
                               "image": SimpleUploadedFile("new.png", png("green"), content_type="image/png")})
        figure.refresh_from_db()
        self.assertEqual((figure.option_key, figure.source), ("B", "upload"))
        self.client.post(url, {"action": "unassign", "figure_id": figure.pk})
        figure.refresh_from_db()
        self.assertIsNone(figure.question)
        pool = reverse("questions:figure_pool", args=[paper.pk])
        self.assertContains(self.client.get(pool), "Unassigned image candidates")
        self.client.post(pool, {"action": "assign", "figure_id": figure.pk, "question_id": q.pk, "target": "C"})
        figure.refresh_from_db(); q.refresh_from_db()
        self.assertEqual((figure.question_id, figure.option_key), (q.pk, "C"))
        self.assertIn({"key": "C", "text": ""}, q.options)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(reverse("questions:figure_file", args=[figure.pk])).status_code, 403)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("questions:source_page_image", args=[q.pk, 1])).status_code, 403)

    def test_invalid_crops_do_not_create_figures(self):
        paper = self.paper()
        q = ExtractedQuestion.objects.create(paper=paper, question_number=1, question_text="Test", page_number=1)
        url = reverse("questions:figure_manage", args=[q.pk])
        self.client.post(url, {"action": "crop", "page_number": 1, "x0": "nan", "y0": "0", "x1": "1", "y1": "1"})
        self.client.post(url, {"action": "crop", "page_number": 3, "x0": "0", "y0": "0", "x1": "1", "y1": "1"})
        self.assertEqual(q.figures.count(), 0)

    def test_manual_add_missing_question_does_not_infer_key(self):
        paper = self.paper()
        result = self.client.post(reverse("questions:extracted_add", args=[paper.pk]),
                                  {"question_number": 42, "page_number": 2, "question_text": "Missing question"})
        self.assertEqual(result.status_code, 302)
        q = paper.extracted_questions.get()
        self.assertEqual((q.correct_option, q.source_pages), ("", [2]))
        self.assertTrue(q.is_flagged)
        self.assertEqual(self.client.get(reverse("questions:extracted_edit", args=[q.pk])).status_code, 200)

    def test_approval_and_bank_edit_preserve_all_figures_and_blank_key(self):
        paper, _, _, _ = self.run_cloud()
        q = paper.extracted_questions.get(question_number=1)
        self.client.post(reverse("questions:extracted_toggle_flag", args=[q.pk]))
        self.client.post(reverse("questions:extracted_approve", args=[q.pk]))
        q.refresh_from_db()
        self.assertEqual(q.status, ExtractedQuestion.Status.APPROVED)
        bank = q.approved_question
        self.assertEqual(bank.language, Question.Language.BILINGUAL)
        self.assertEqual(bank.figures.filter(option__isnull=True).count(), 2)
        self.assertEqual(bank.options.get(option_key="A").figures.count(), 1)
        self.assertEqual(bank.options.get(option_key="B").figures.count(), 1)
        self.assertFalse(bank.options.filter(is_correct=True).exists())
        self.assertContains(self.client.get(reverse("questions:question_preview", args=[bank.pk])), "Option A image")
        result = self.client.post(reverse("questions:question_edit", args=[bank.pk]), {
            "category": self.category.pk, "subject": "Science", "difficulty": "medium", "language": "en",
            "question_type": "mcq_single", "question_text": "Edited image question", "positive_marks": "4",
            "negative_marks": "1", "option_text_A": "", "option_text_B": "", "correct_option": "",
        })
        self.assertEqual(result.status_code, 302)
        bank.refresh_from_db()
        self.assertEqual(bank.question_text, "Edited image question")
        self.assertEqual(bank.figures.count(), 4)
        self.assertEqual(bank.options.count(), 2)
        self.assertFalse(bank.options.filter(is_correct=True).exists())
        self.assertEqual(self.client.get(reverse("questions:bank_figure_manage", args=[bank.pk])).status_code, 200)

    def test_editing_staging_preserves_image_only_options(self):
        paper, _, _, _ = self.run_cloud()
        q = paper.extracted_questions.get(question_number=1)
        result = self.client.post(reverse("questions:extracted_edit", args=[q.pk]), {
            "question_number": 1, "question_text": "Updated question", "positive_marks": "4", "negative_marks": "1",
            "option_text_A": "", "option_text_B": "", "correct_option": "", "is_flagged": "on", "flag_reason": "Review",
        })
        self.assertEqual(result.status_code, 302)
        q.refresh_from_db()
        self.assertEqual(q.options, [{"key": "A", "text": ""}, {"key": "B", "text": ""}])
        self.assertEqual(q.figures.filter(option_key__in=["A", "B"]).count(), 2)

    def test_bank_figure_management_prevents_removing_only_option_content(self):
        paper, _, _, _ = self.run_cloud()
        q = paper.extracted_questions.get(question_number=1)
        q.is_flagged = False; q.save(update_fields=["is_flagged"])
        self.client.post(reverse("questions:extracted_approve", args=[q.pk]))
        q.refresh_from_db(); bank = q.approved_question
        option = bank.options.get(option_key="A")
        figure = option.figures.get()
        url = reverse("questions:bank_figure_manage", args=[bank.pk])
        self.client.post(url, {"action": "delete", "figure_id": figure.pk})
        self.assertTrue(QuestionFigure.objects.filter(pk=figure.pk).exists())
        self.client.post(url, {"action": "upload", "target": "A", "image": SimpleUploadedFile("extra.png", png("yellow"), content_type="image/png")})
        self.assertEqual(option.figures.count(), 2)
        self.client.post(url, {"action": "delete", "figure_id": figure.pk})
        self.assertEqual(option.figures.count(), 1)

    def test_bank_crop_and_approved_staging_mutation_guard(self):
        paper, _, _, _ = self.run_cloud()
        q = paper.extracted_questions.get(question_number=1)
        q.is_flagged = False; q.save(update_fields=["is_flagged"])
        self.client.post(reverse("questions:extracted_approve", args=[q.pk]))
        q.refresh_from_db(); bank = q.approved_question
        url = reverse("questions:bank_figure_manage", args=[bank.pk])
        before = bank.figures.count()
        self.client.post(url, {"action": "crop", "page_number": 1, "target": "", "x0": ".1", "y0": ".15", "x1": ".2667", "y1": ".225"})
        self.assertEqual(bank.figures.count(), before + 1)
        self.assertEqual(self.client.post(reverse("questions:figure_manage", args=[q.pk]), {
            "action": "delete", "figure_id": q.figures.first().pk,
        }).status_code, 403)
        self.client.post(reverse("questions:extracted_delete", args=[q.pk]))
        self.assertTrue(ExtractedQuestion.objects.filter(pk=q.pk).exists())

    def test_candidate_receives_multiple_images_without_answer_keys_or_script_injection(self):
        paper, _, _, _ = self.run_cloud()
        q = paper.extracted_questions.get(question_number=1)
        q.is_flagged = False; q.save(update_fields=["is_flagged"])
        self.client.post(reverse("questions:extracted_approve", args=[q.pk]))
        q.refresh_from_db(); bank = q.approved_question
        bank.question_text = '</script><script>alert("xss")</script>'
        bank.save(update_fields=["question_text"])
        exam = Exam.objects.create(title="Test", slug="cloud-exam", category=self.category, creator=self.teacher)
        eq = ExamQuestion.objects.create(exam=exam, question=bank, order=1)
        attempt = ExamAttempt.objects.create(exam=exam, student=self.student, expires_at=timezone.now() + timedelta(minutes=30))
        chosen = bank.options.get(option_key="A")
        saved_response = ExamResponse.objects.create(attempt=attempt, exam_question=eq, order=1, selected_option=chosen)
        self.client.post(reverse("questions:question_edit", args=[bank.pk]), {
            "category": self.category.pk, "subject": "Science", "difficulty": "medium", "language": "en",
            "question_type": "mcq_single", "question_text": bank.question_text, "positive_marks": "4",
            "negative_marks": "1", "option_text_A": "", "option_text_B": "", "correct_option": "",
        })
        saved_response.refresh_from_db()
        self.assertEqual(saved_response.selected_option_id, chosen.id)
        self.client.force_login(self.student)
        response = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(response.status_code, 200)
        data = response.context["questions_data"][0]
        self.assertEqual(len(data["image_urls"]), 2)
        self.assertEqual(len(data["options"][0]["image_urls"]), 1)
        self.assertNotIn("is_correct", data["options"][0])
        self.assertNotIn(b'</script><script>alert("xss")</script>', response.content)
        self.assertContains(response, 'questions-data')
