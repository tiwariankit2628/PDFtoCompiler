"""Structured PDF, reviewer and opt-in AI safety regressions.

The optional real-PDF regression reads a user-supplied path, never a fixture in
Git or an external service. Provider tests below use an in-memory fake.
"""
import json
import os
import tempfile
from decimal import Decimal
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

import pymupdf as fitz
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category, ExamAttempt
from apps.payments.models import PracticePlan, PracticeUsage
from .answer_ai import GeminiAnswerClient, SuggestionError
from .cloud_extractor import validate_questions
from .models import AIAnswerSuggestion, AnswerStatus, ExtractedQuestion, QuestionPaperUpload
from .pdf_layout import DigitalQuestionLayout
from .structured_content import validate_tables
from .templatetags.question_content import render_question
from .test_pdf_layout import make_two_column_pdf


def cell(text, colspan=1, rowspan=1, header=False):
    return {"text": text, "colspan": colspan, "rowspan": rowspan, "header": header}


def sample_table():
    return [{"columns": 2, "rows": [[cell("Head <script>alert(1)</script>", 2, header=True)],
                                     [cell("भारत"), cell(r"$\\frac{1}{2}$")]]}]


def ruled_table_pdf():
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((30, 80), "Q1")
    page.insert_text((60, 80), "Match the column:")
    page.draw_rect(fitz.Rect(60, 100, 300, 180))
    page.draw_line((60, 125), (300, 125))
    page.draw_line((60, 152), (300, 152))
    page.draw_line((180, 125), (180, 180))
    page.insert_text((70, 116), "Column - I and II")
    page.insert_text((70, 144), "A")
    page.insert_text((190, 144), "Alpha")
    page.insert_text((70, 171), "B")
    page.insert_text((190, 171), "Beta")
    page.insert_text((60, 210), "Choose the correct answer:")
    for index, word in enumerate(("First", "Second", "Third", "Fourth")):
        page.insert_text((60, 235 + index * 20), f"({index + 1}) {word}")
    data = doc.write(); doc.close(); return data


class StructuredContentTests(SimpleTestCase):
    def test_table_validation_rejects_overlaps_bad_markers_and_hostile_shapes(self):
        data = sample_table()
        self.assertEqual(validate_tables(data, "Before [[table:1]] after")[0]["rows"][0][0]["colspan"], 2)
        with self.assertRaises(ValidationError):
            validate_tables([], "[[table:" + "9" * 5000 + "]]")
        for bad, text in ((data, "No marker"), (data, "[[table:2]]"),
                          ([{"columns": 2, "rows": [[cell("x", 3)]]}], "[[table:1]]"),
                          ([{"columns": 2, "rows": [[cell("x")]]}], "[[table:1]]"),
                          ([{"columns": 1, "rows": [[{"text": "x", "onmouseover": "evil"}]]}], "[[table:1]]")):
            with self.subTest(bad=bad, text=text), self.assertRaises(ValidationError):
                validate_tables(bad, text)

    def test_renderer_escapes_untrusted_text_and_keeps_merged_cells_and_labels(self):
        question = SimpleNamespace(question_text='Intro <img src=x onerror=alert(1)> [[table:1]] Assertion A: true Reason R: false', tables=sample_table())
        rendered = str(render_question(question))
        self.assertIn('<table class="question-table">', rendered)
        self.assertIn('colspan="2"', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('<img src=x', rendered)
        self.assertIn('<strong class="statement-label">Assertion A:</strong>', rendered)
        self.assertIn('<strong class="statement-label">Reason R:</strong>', rendered)
        self.assertIn('भारत', rendered)

    def test_detects_ruled_table_and_keeps_its_position_and_source(self):
        with fitz.open(stream=ruled_table_pdf(), filetype="pdf") as doc:
            question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        self.assertEqual(question.number, 1)
        self.assertEqual(len(question.options), 4)
        self.assertIn("Match the column: [[table:1]] Choose", question.text)
        self.assertEqual(question.tables[0]["columns"], 2)
        self.assertEqual(question.tables[0]["rows"][0][0]["colspan"], 2)
        self.assertEqual(question.tables[0]["rows"][2][1]["text"], "Beta")
        self.assertEqual(question.source_regions[0]["page"], 1)
        self.assertTrue(question.issues)  # detection is not a verification

    def test_simple_raised_digits_remain_visible_superscripts(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 Find 10")
        # Superscript is actually positioned immediately after the final glyph.
        words = page.get_text("words"); end = words[-1][2]
        page.insert_text((end, 76), "2", fontsize=8)
        page.insert_text((60, 110), "(1) Yes")
        page.insert_text((60, 130), "(2) No")
        question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        doc.close()
        self.assertIn("10²", question.text)


REGRESSION_PDF = os.environ.get("REGRESSION_USER_PDF", "")


@skipUnless(REGRESSION_PDF and os.path.isfile(REGRESSION_PDF), "Optional local original-PDF regression")
class OriginalPdfTests(SimpleTestCase):
    def test_user_paper_geometry_tables_and_reconstructed_math(self):
        with fitz.open(REGRESSION_PDF) as doc:
            questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual(len(questions), 180)
        by_number = {q.number: q for q in questions}
        self.assertEqual(len(by_number), 180)
        self.assertEqual(by_number[55].page_number, 9)
        table = by_number[55].tables[0]
        self.assertEqual((table["columns"], len(table["rows"])), (4, 5))
        self.assertEqual(table["rows"][4][3]["text"], "n²")
        self.assertEqual(table["rows"][0][0]["colspan"], 2)
        self.assertIn("10⁻³⁴", by_number[52].text)
        self.assertEqual(by_number[16].options[3]["text"], r"\(50\sqrt{3}N\)")
        self.assertTrue(by_number[39].text.endswith("origin."), by_number[39].text)
        self.assertEqual([o["text"] for o in by_number[39].options], [
            r"\(\frac{a}{10}\sqrt{74}\)", r"\(\frac{a}{5}\sqrt{74}\)",
            r"\(\frac{a}{10}\sqrt{34}\)", r"\(\frac{a}{5}\sqrt{34}\)",
        ])
        self.assertEqual(by_number[44].options[1]["text"], r"\(\frac{g}{\sqrt{3}}\)")
        self.assertEqual(by_number[45].options[0]["text"], r"\(\frac{3L}{4}\)")
        self.assertEqual(by_number[56].options[1]["text"], r"\(\frac{RT}{2F}\ln \frac{P_{1}}{P_{2}}\)")
        cells = [row[-1]["text"] for row in by_number[80].tables[0]["rows"][1:]]
        self.assertEqual(cells, [r"\(\frac{Z}{n}\)", r"\(\frac{n^{2}}{Z}\)", r"\(\frac{Z^{2}}{n^{2}}\)", "n"])
        for number in (94, 150):
            self.assertEqual(len(by_number[number].tables), 1)
            table = by_number[number].tables[0]
            self.assertEqual((table["columns"], len(table["rows"])), (4, 5))
            self.assertEqual([cell["colspan"] for cell in table["rows"][0]], [2, 2])
        self.assertTrue(any("Mathematical" in issue for issue in by_number[45].issues))


class FakeResponse:
    def __init__(self, value, status=200): self.value, self.status_code = value, status
    def json(self): return self.value


class FakeSession:
    def __init__(self, responses): self.responses, self.requests = list(responses), []
    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


def provider_reply(key="A", confidence=.92, needs_visual=False):
    data = {"option_key": key, "confidence": confidence, "explanation": "Reasoned suggestion.",
            "uncertainty": "Verify against the original.", "requires_visual": needs_visual}
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(data)}]}}],
            "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200, "thoughtsTokenCount": 100}}


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   AI_ANSWER_ENABLED=True, AI_ANSWER_MODEL="gemini-test-model", GEMINI_API_KEY="test-only-secret",
                   AI_INPUT_USD_PER_MILLION="2", AI_OUTPUT_USD_PER_MILLION="4")
class AISuggestionTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(); self.addCleanup(self.root.cleanup)
        self.settings_override = override_settings(PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private"), MEDIA_ROOT=os.path.join(self.root.name, "media"))
        self.settings_override.enable(); self.addCleanup(self.settings_override.disable)
        self.student = User.objects.create_user(username="ai-owner", email="ai-owner@example.test", role=User.Roles.STUDENT, password="x")
        self.other = User.objects.create_user(username="ai-other", email="ai-other@example.test", role=User.Roles.STUDENT, password="x")
        self.category = Category.objects.create(name="AI test category", slug="ai-test-category")
        self.paper = QuestionPaperUpload.objects.create(title="Private source", uploaded_by=self.student,
            category=self.category, is_student_private=True, status=QuestionPaperUpload.ProcessingStatus.EXTRACTED,
            private_pdf_file=SimpleUploadedFile("source.pdf", make_two_column_pdf(), content_type="application/pdf"))
        self.question = ExtractedQuestion.objects.create(paper=self.paper, question_number=1,
            question_text="Match [[table:1]]", tables=sample_table(),
            options=[{"key": "A", "text": r"$\\frac{1}{2}$"}, {"key": "B", "text": "2"}],
            source_regions=[{"page": 2, "box": [0.05, .08, .48, .32]}])
        self.client.force_login(self.student)
        self.free = PracticePlan.objects.get(is_default=True)
        self.request_url = reverse("questions:practice_answer_request", args=[self.question.pk])
        self.decision_url = reverse("questions:practice_answer_decision", args=[self.question.pk])
        self.workspace_url = reverse("questions:practice_answer_suggestion", args=[self.question.pk])

    def fake_result(self):
        return {"option_key": "A", "confidence": Decimal("0.920"), "explanation": "Suggested reasoning",
                "uncertainty": "Check it", "prompt_tokens": 1000, "output_tokens": 300,
                "estimated_cost_usd": Decimal("0.003200")}

    def test_provider_checks_model_validates_output_and_keeps_secrets_out_of_url(self):
        session = FakeSession([FakeResponse({"name": "models/gemini-test-model", "supportedGenerationMethods": ["generateContent"]}), FakeResponse(provider_reply())])
        client = GeminiAnswerClient(session=session, sleep=lambda _: None)
        result = client.suggest(self.question, [])
        self.assertEqual(result["option_key"], "A")
        self.assertEqual(result["output_tokens"], 300)
        self.assertEqual(result["estimated_cost_usd"], Decimal("0.003200"))
        self.assertEqual(client.calls, 2)
        self.assertNotIn("test-only-secret", session.requests[1][1])
        self.assertEqual(session.requests[1][2]["headers"]["x-goog-api-key"], "test-only-secret")
        self.assertNotIn("inlineData", str(session.requests[1][2]["json"]))

    def test_low_confidence_or_missing_vision_never_produces_a_key(self):
        for reply in (provider_reply(confidence=.2), provider_reply(needs_visual=True)):
            with self.subTest(reply=reply):
                session = FakeSession([FakeResponse({"supportedGenerationMethods": ["generateContent"]}), FakeResponse(reply)])
                result = GeminiAnswerClient(session=session).suggest(self.question, [])
                self.assertEqual(result["option_key"], "")
                self.assertIn("withheld", result["uncertainty"])

    def test_provider_invalid_answer_and_model_are_rejected(self):
        session = FakeSession([FakeResponse({"supportedGenerationMethods": ["generateContent"]}), FakeResponse(provider_reply(key="Z"))])
        with self.assertRaises(SuggestionError): GeminiAnswerClient(session=session).suggest(self.question, [])
        session = FakeSession([FakeResponse({"supportedGenerationMethods": ["countTokens"]})])
        with self.assertRaises(SuggestionError): GeminiAnswerClient(session=session).suggest(self.question, [])
        self.assertEqual(len(session.requests), 1)

    def test_no_external_call_without_consent_or_configuration(self):
        with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
            self.client.post(self.request_url, {})
            self.assertFalse(provider.called)
            with self.settings(AI_ANSWER_ENABLED=False): self.client.post(self.request_url, {"consent": "yes"})
            self.assertFalse(provider.called)
        self.assertFalse(AIAnswerSuggestion.objects.exists())
        self.assertFalse(PracticeUsage.objects.filter(user=self.student).exists())

    def test_suggestion_is_separate_deduplicated_then_explicitly_edited_and_confirmed(self):
        with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
            provider.return_value.suggest.return_value = self.fake_result()
            provider.return_value.calls = 2
            self.client.post(self.request_url, {"consent": "yes"})
            self.client.post(self.request_url, {"consent": "yes"})
            self.assertEqual(provider.return_value.suggest.call_count, 1)
        suggestion = AIAnswerSuggestion.objects.get()
        self.question.refresh_from_db()
        self.assertEqual(self.question.correct_option, "")
        self.assertEqual(self.question.answer_status, AnswerStatus.NEEDS_REVIEW)
        self.assertEqual(suggestion.status, AIAnswerSuggestion.Status.SUGGESTED)
        self.assertEqual(PracticeUsage.objects.get(user=self.student).ai_suggestions, 1)
        self.assertContains(self.client.get(self.workspace_url), "AI suggested")
        data = {"suggestion_id": suggestion.pk, "action": "confirm", "option_key": "B", "explanation": "Human edited"}
        self.client.post(self.decision_url, data)
        self.question.refresh_from_db(); self.assertEqual(self.question.correct_option, "")
        data["confirm"] = "yes"; self.client.post(self.decision_url, data)
        suggestion.refresh_from_db(); self.question.refresh_from_db()
        self.assertEqual(suggestion.status, AIAnswerSuggestion.Status.ACCEPTED)
        self.assertEqual(self.question.correct_option, "B")
        self.assertEqual(self.question.answer_status, AnswerStatus.STUDENT_CONFIRMED)
        self.assertEqual(self.question.explanation, "Human edited")

    def test_stale_suggestion_cannot_confirm_and_reject_does_not_change_existing_key(self):
        with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
            provider.return_value.suggest.return_value = self.fake_result(); provider.return_value.calls = 2
            self.client.post(self.request_url, {"consent": "yes"})
        suggestion = AIAnswerSuggestion.objects.get()
        self.question.question_text = "Changed [[table:1]]"; self.question.save(update_fields=["question_text"])
        self.client.post(self.decision_url, {"suggestion_id": suggestion.pk, "action": "confirm", "confirm": "yes", "option_key": "A"})
        self.question.refresh_from_db(); self.assertEqual(self.question.correct_option, "")
        self.client.post(self.decision_url, {"suggestion_id": suggestion.pk, "action": "reject"})
        suggestion.refresh_from_db(); self.assertEqual(suggestion.status, AIAnswerSuggestion.Status.REJECTED)

    def test_owner_privacy_quota_and_source_excerpt(self):
        response = self.client.get(reverse("questions:practice_source_region", args=[self.question.pk, 0]))
        self.assertEqual(response.status_code, 200); self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("no-store", response["Cache-Control"])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.workspace_url).status_code, 404)
        self.assertEqual(self.client.get(reverse("questions:practice_source_region", args=[self.question.pk, 0])).status_code, 404)
        with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
            self.assertEqual(self.client.post(self.request_url, {"consent": "yes"}).status_code, 404)
            self.assertFalse(provider.called)
        self.client.force_login(self.student); self.free.monthly_ai_suggestions = 0; self.free.save()
        with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
            self.client.post(self.request_url, {"consent": "yes"})
            self.assertFalse(provider.called)
        self.assertFalse(AIAnswerSuggestion.objects.exists())

    def test_table_survives_staging_bank_cbt_and_result(self):
        self.question.correct_option = "A"; self.question.answer_status = AnswerStatus.STUDENT_CONFIRMED
        self.question.save(update_fields=["correct_option", "answer_status"])
        review = self.client.get(reverse("questions:practice_settings", args=[self.paper.pk]))
        self.assertContains(review, '<table class="question-table">', html=False)
        response = self.client.post(reverse("questions:practice_settings", args=[self.paper.pk]), {"duration_minutes": 30})
        self.assertEqual(response.status_code, 302)
        self.paper.refresh_from_db(); exam = self.paper.practice_exam
        bank = exam.questions.get(); self.assertEqual(bank.tables, sample_table()); self.assertTrue(bank.key_verified)
        self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        attempt = ExamAttempt.objects.get(exam=exam)
        session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertContains(session, '"tables"')
        self.assertContains(session, 'question-content.js')
        response_row = attempt.responses.first(); answer = bank.options.get(option_key="A")
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]),
            data=json.dumps({"last_response": {"response_id": response_row.pk, "selected_option_id": answer.pk, "time_spent_delta": 1}}), content_type="application/json")
        attempt.refresh_from_db(); self.assertEqual(attempt.correct_count, 1)
        result = self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        self.assertContains(result, '<table class="question-table">', html=False)
        self.assertContains(result, 'colspan="2"', html=False)

    def test_active_attempt_blocks_suggestion_and_source_without_call(self):
        self.client.post(reverse("questions:practice_settings", args=[self.paper.pk]), {"duration_minutes": 30})
        self.paper.refresh_from_db(); exam = self.paper.practice_exam
        self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        attempt = ExamAttempt.objects.get(exam=exam)
        with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
            for response in (self.client.post(self.request_url, {"consent": "yes"}),
                             self.client.get(self.workspace_url),
                             self.client.get(reverse("questions:practice_source_region", args=[self.question.pk, 0]))):
                self.assertEqual(response.status_code, 302)
                self.assertIn(str(attempt.attempt_uuid), response.url)
            provider.assert_not_called()
        self.assertFalse(AIAnswerSuggestion.objects.exists())

    def test_failed_provider_call_is_logged_without_a_key_or_retry(self):
        with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
            provider.return_value.suggest.side_effect = SuggestionError("Mocked provider failure")
            provider.return_value.calls = 1
            self.client.post(self.request_url, {"consent": "yes"})
        suggestion = AIAnswerSuggestion.objects.get()
        self.assertEqual(suggestion.status, AIAnswerSuggestion.Status.FAILED)
        self.assertEqual(suggestion.provider_calls, 1)
        self.question.refresh_from_db(); self.assertEqual(self.question.correct_option, "")
        self.assertEqual(PracticeUsage.objects.get(user=self.student).ai_suggestions, 1)

    def test_invalid_id_and_unconfigured_model_are_safe(self):
        self.assertEqual(self.client.post(self.decision_url, {"suggestion_id": "invalid", "action": "confirm"}).status_code, 404)
        with self.settings(AI_ANSWER_MODEL=""):
            with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
                self.client.post(self.request_url, {"consent": "yes"})
                provider.assert_not_called()

    def test_unconfirmed_suggestion_is_not_used_by_cbt_grading(self):
        with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
            provider.return_value.suggest.return_value = self.fake_result(); provider.return_value.calls = 2
            self.client.post(self.request_url, {"consent": "yes"})
        self.client.post(reverse("questions:practice_settings", args=[self.paper.pk]), {"duration_minutes": 30})
        self.paper.refresh_from_db(); exam = self.paper.practice_exam; bank = exam.questions.get()
        self.assertFalse(bank.key_verified)
        self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        attempt = ExamAttempt.objects.get(exam=exam); response_row = attempt.responses.first()
        answer = bank.options.get(option_key="A")
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]),
            data=json.dumps({"last_response": {"response_id": response_row.pk, "selected_option_id": answer.pk, "time_spent_delta": 1}}), content_type="application/json")
        attempt.refresh_from_db(); self.assertEqual(attempt.correct_count, 0); self.assertEqual(attempt.incorrect_count, 0)
        result = self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        self.assertEqual(result.context["counts"]["ungraded"], 1)
        self.assertEqual(result.context["attempted_total"], 1)
        self.assertContains(result, "1 / 1 attempted")

    def test_teacher_category_authorization_and_private_isolation(self):
        teacher = User.objects.create_user(username="ai-teacher", email="ai-teacher@example.test", role=User.Roles.TEACHER, is_staff=True)
        outsider = User.objects.create_user(username="ai-outsider", email="ai-outsider@example.test", role=User.Roles.TEACHER, is_staff=True)
        paper = QuestionPaperUpload.objects.create(title="Teacher paper", uploaded_by=teacher, category=self.category,
            pdf_file=SimpleUploadedFile("source.pdf", make_two_column_pdf()), status=QuestionPaperUpload.ProcessingStatus.EXTRACTED)
        question = ExtractedQuestion.objects.create(paper=paper, question_number=1, question_text="Which?",
            options=[{"key": "A", "text": "Yes"}, {"key": "B", "text": "No"}])
        url = reverse("questions:answer_request", args=[question.pk])
        self.client.force_login(outsider)
        with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
            self.assertEqual(self.client.post(url, {"consent": "yes"}).status_code, 403)
            provider.assert_not_called()
        self.assertEqual(self.client.get(reverse("questions:answer_suggestion", args=[self.question.pk])).status_code, 404)
        outsider.assigned_categories.add(self.category)
        with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
            provider.return_value.suggest.return_value = self.fake_result(); provider.return_value.calls = 2
            self.assertEqual(self.client.post(url, {"consent": "yes"}).status_code, 302)
        suggestion = AIAnswerSuggestion.objects.get(question=question)
        question.refresh_from_db(); self.assertEqual(question.correct_option, "")
        self.client.post(reverse("questions:answer_decision", args=[question.pk]), {"suggestion_id": suggestion.pk,
            "action": "confirm", "confirm": "yes", "option_key": "A", "explanation": "Reviewed by teacher"})
        question.refresh_from_db(); self.assertEqual(question.answer_status, AnswerStatus.MANUALLY_VERIFIED)
        self.assertEqual(question.correct_option, "A")

    def test_table_edit_and_option_figure_provenance_propagate(self):
        from .models import ExtractedFigure
        from .test_pdf_layout import image_bytes
        fig = ExtractedFigure.objects.create(paper=self.paper, question=self.question, option_key="B", page_number=2,
            bounding_box=[.1, .1, .3, .2], source="manual_crop",
            private_image=SimpleUploadedFile("figure.png", image_bytes("red"), content_type="image/png"))
        updated = sample_table(); updated[0]["rows"][1][0]["text"] = "Edited भारत"
        response = self.client.post(reverse("questions:practice_question_edit", args=[self.question.pk]), {
            "question_text": self.question.question_text, "option_A": "one", "option_B": "two", "option_C": "", "option_D": "", "option_E": "",
            "tables_json": json.dumps(updated), "positive_marks": "4", "negative_marks": "1", "correct_option": "A", "reviewed": "on"})
        self.assertEqual(response.status_code, 302)
        self.question.refresh_from_db(); self.assertEqual(self.question.tables, updated)
        self.client.post(reverse("questions:practice_settings", args=[self.paper.pk]), {"duration_minutes": 30})
        self.paper.refresh_from_db(); bank = self.paper.practice_exam.questions.get()
        self.assertEqual(bank.tables, updated)
        copied = bank.figures.get(); self.assertEqual(copied.option.option_key, "B")
        self.assertEqual(copied.bounding_box, fig.bounding_box); self.assertEqual(copied.page_number, 2)
        self.assertEqual(copied.source, "manual_crop")

    def test_cloud_table_payload_is_validated_not_executed(self):
        from .cloud_providers import CloudExtractionError
        raw = {"number": 1, "page_number": 1, "end_page": 1, "text": "Match [[table:1]]",
               "image_ids": [], "issues": [], "options": [{"key": "A", "text": "yes", "image_ids": []},
                                                              {"key": "B", "text": "no", "image_ids": []}],
               "tables": sample_table()}
        result = validate_questions([raw], 1, 1, lambda _: None, 1)
        self.assertEqual(result[0]["tables"], sample_table())
        raw["tables"][0]["rows"][0][0]["colspan"] = 200
        with self.assertRaises(CloudExtractionError): validate_questions([raw], 1, 1, lambda _: None, 1)

    def test_vision_is_separate_and_transient_retries_are_bounded(self):
        from .answer_ai import vision_images
        images = vision_images(self.question)
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0][1].startswith(b"\xff\xd8\xff"))
        session = FakeSession([
            FakeResponse({}, 503), FakeResponse({"supportedGenerationMethods": ["generateContent"]}),
            FakeResponse({}, 429), FakeResponse(provider_reply(needs_visual=True)),
        ])
        client = GeminiAnswerClient(session=session, sleep=lambda _: None)
        result = client.suggest(self.question, images)
        self.assertEqual(result["option_key"], "A")
        self.assertEqual(client.calls, 4)
        self.assertIn("inlineData", str(session.requests[-1][2]["json"]))

    def test_attached_figure_without_vision_withholds_even_confident_model_choice(self):
        self.question.private_image = "private/figure.png"  # Only the presence is relevant for text-only safety.
        session = FakeSession([FakeResponse({"supportedGenerationMethods": ["generateContent"]}), FakeResponse(provider_reply())])
        result = GeminiAnswerClient(session=session).suggest(self.question, [])
        self.assertEqual(result["option_key"], "")
        self.assertIn("withheld", result["uncertainty"])

    def test_cloud_source_regions_are_bounded_and_persisted(self):
        from unittest.mock import Mock
        from django.utils import timezone
        from .cloud_extractor import CloudPDFQuestionExtractor
        from .cloud_providers import CloudExtractionError
        from .test_cloud_pipeline import CLOUD_SETTINGS, synthetic_pdf, mathpix_result, gemini_questions
        paper = QuestionPaperUpload.objects.create(title="Mock cloud", category=self.category, uploaded_by=self.student,
            is_student_private=True, extraction_mode="cloud", cloud_consent_at=timezone.now(),
            private_pdf_file=SimpleUploadedFile("cloud.pdf", synthetic_pdf(), content_type="application/pdf"))
        data = gemini_questions(); data[0]["source_regions"] = [{"page": 1, "box": [.05, .08, .49, .65]}, {"page": 2, "box": [.05, .05, .49, .2]}]
        mathpix, gemini = Mock(pdf_id="mock-id"), Mock()
        mathpix.read_pdf.return_value = mathpix_result(); mathpix.delete.return_value = True
        gemini.interpret.return_value = data
        with self.settings(**CLOUD_SETTINGS):
            self.assertEqual(CloudPDFQuestionExtractor(paper, mathpix, gemini).extract(), 2)
        saved = paper.extracted_questions.get(question_number=1)
        self.assertEqual(saved.source_regions, data[0]["source_regions"])
        self.assertTrue(saved.is_flagged)
        data[0]["source_regions"] = [{"page": 3, "box": [0, 0, 1, 1]}]
        with self.assertRaises(CloudExtractionError): validate_questions(data, 1, 2, lambda _: None, 2)
        data[0]["source_regions"] = [{"page": 1, "box": [0, 0, 10, 1]}]
        with self.assertRaises(CloudExtractionError): validate_questions(data, 1, 2, lambda _: None, 2)

    def test_legacy_answer_status_backfill_never_marks_ambiguous_keys(self):
        from importlib import import_module
        from django.apps import apps
        from .models import Question, QuestionOption
        teacher = User.objects.create_user(username="legacy_teacher", email="legacy_teacher@example.test", role=User.Roles.TEACHER)
        valid = Question.objects.create(category=self.category, creator=teacher, subject="Science", question_text="Valid")
        ambiguous = Question.objects.create(category=self.category, creator=teacher, subject="Science", question_text="Ambiguous")
        QuestionOption.objects.create(question=valid, option_key="A", option_text="x", is_correct=True)
        QuestionOption.objects.create(question=ambiguous, option_key="A", option_text="x", is_correct=True)
        QuestionOption.objects.create(question=ambiguous, option_key="B", option_text="y", is_correct=True)
        migration = import_module("apps.questions.migrations.0007_extractedquestion_answer_status_and_more")
        migration.preserve_existing_confirmations(apps, None)
        valid.refresh_from_db(); ambiguous.refresh_from_db()
        self.assertEqual(valid.answer_status, AnswerStatus.MANUALLY_VERIFIED)
        self.assertEqual(ambiguous.answer_status, AnswerStatus.NEEDS_REVIEW)
        self.assertEqual(ambiguous.options.filter(is_correct=True).count(), 2)  # no old data changed

    def test_reasoning_budget_is_configurable_and_respects_model_metadata(self):
        session = FakeSession([FakeResponse({"supportedGenerationMethods": ["generateContent"], "outputTokenLimit": 4096}), FakeResponse(provider_reply())])
        with self.settings(AI_ANSWER_THINKING_LEVEL="high", AI_ANSWER_MAX_OUTPUT_TOKENS=32768):
            GeminiAnswerClient(session=session).suggest(self.question, [])
        config = session.requests[-1][2]["json"]["generationConfig"]
        self.assertEqual(config["thinkingConfig"]["thinkingLevel"], "high")
        self.assertEqual(config["maxOutputTokens"], 4096)
        self.assertEqual(config["responseFormat"]["text"]["mimeType"], "APPLICATION_JSON")
        self.assertNotIn("temperature", config)
        with self.settings(AI_ANSWER_THINKING_LEVEL="unknown"):
            with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
                self.client.post(self.request_url, {"consent": "yes"})
                provider.assert_not_called()
