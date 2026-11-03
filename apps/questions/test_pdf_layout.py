"""Regression tests for real-world PDF layout and review failures.

The fixtures are generated in memory: no user PDF or third-party content is
stored in the repository.
"""
import io
import tempfile
from unittest.mock import patch

import pymupdf as fitz
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category
from .extractor import PDFQuestionExtractor
from .models import ExtractedQuestion, Question, QuestionPaperUpload
from .pdf_layout import DigitalQuestionLayout, parse_options


def image_bytes(color):
    buffer = io.BytesIO()
    Image.new("RGB", (80, 60), color).save(buffer, format="PNG")
    return buffer.getvalue()


def make_two_column_pdf():
    doc = fitz.open()
    logo = image_bytes("black")
    red = image_bytes("red")
    green = image_bytes("green")
    blue = image_bytes("blue")

    cover = doc.new_page(width=600, height=800)
    cover.insert_text((30, 120), "General Instructions:")
    cover.insert_text((30, 145), "1. Fill in the particulars.")
    cover.insert_text((30, 160), "2. Use a black pen.")
    cover.insert_image(fitz.Rect(280, 750, 320, 780), stream=logo)

    page = doc.new_page(width=600, height=800)
    page.insert_text((550, 35), "NEET")
    page.insert_text((230, 60), "PRACTICE TEST", fontsize=18)
    page.insert_text((30, 90), "Q1")
    page.insert_text((55, 90), "Which graph is red?")
    page.insert_image(fitz.Rect(55, 105, 135, 165), stream=red)
    page.insert_text((55, 190), "(1) Yes")
    page.insert_text((180, 190), "(2) No")
    page.insert_text((55, 207), "(3) Maybe")
    page.insert_text((180, 207), "(4) Both (2) and (3)")
    page.insert_text((30, 260), "Q2")
    page.insert_text((55, 260), "The diagram continues in the next column.")
    page.insert_text((55, 277), "The constant is 0. 01287 units.")
    page.insert_text((174, 272), "2", fontsize=6)  # A superscript needs review.
    page.insert_image(fitz.Rect(335, 90, 415, 150), stream=green)
    page.insert_text((335, 175), "(1) First")
    page.insert_text((460, 175), "(2) Second")
    page.insert_text((335, 192), "(3) Third")
    page.insert_text((460, 192), "(4) Fourth")
    page.insert_text((310, 250), "Q3")
    page.insert_text((335, 250), "Which figure is shown on the next page?")
    page.insert_text((180, 750), "Repeated footer branding")
    page.insert_image(fitz.Rect(280, 750, 320, 780), stream=logo)

    page = doc.new_page(width=600, height=800)
    page.insert_text((550, 35), "NEET")
    page.insert_image(fitz.Rect(55, 75, 135, 135), stream=blue)
    page.insert_text((55, 160), "(1) One")
    page.insert_text((180, 160), "(2) Two")
    page.insert_text((55, 177), "(3) Three")
    page.insert_text((180, 177), "(4) Four")
    page.insert_text((30, 240), "Q4")
    page.insert_text((55, 240), "Match the List-I with List-II.")
    page.insert_text((55, 257), "(A) Calyx")
    page.insert_text((55, 274), "(B) Corolla")
    page.insert_text((55, 291), "(C) Androecium")
    page.insert_text((55, 308), "(D) Gynoecium")
    page.insert_text((55, 325), "Choose the correct answer.")
    page.insert_text((55, 342), "(1) A-IV")
    page.insert_text((55, 359), "(2) A-III")
    page.insert_text((55, 376), "(3) A-II")
    page.insert_text((55, 393), "(4) A-I")
    page.insert_text((180, 750), "Repeated footer branding")
    page.insert_image(fitz.Rect(280, 750, 320, 780), stream=logo)
    content = doc.write()
    doc.close()
    return content


def make_numbered_pdf():
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((40, 80), "1. The figure below shows an example.")
    page.insert_text((40, 100), "(A) First")
    page.insert_text((40, 120), "(B) Second")
    page.insert_text((60, 140), "0. 01287")
    page.insert_text((60, 160), "2. 574")
    page.insert_image(fitz.Rect(250, 730, 310, 775), stream=image_bytes("black"))
    result = doc.write()
    doc.close()
    return result


class OptionParsingTests(SimpleTestCase):
    def test_matching_list_precedes_real_answer_options(self):
        statement, options, _, _ = parse_options(
            "Match the lists:\n(A) alpha\n(B) beta\n(C) gamma\n(D) delta\n"
            "(1) A-II\n(2) A-III\n(3) A-IV\n(4) A-I"
        )
        self.assertIn("(A) alpha", statement)
        self.assertEqual([o["text"] for o in options], ["A-II", "A-III", "A-IV", "A-I"])

    def test_references_inside_option_do_not_become_more_options(self):
        _, options, _, _ = parse_options(
            "Statement\n(1) First (2) Second\n(3) Third (4) Both (2) and (3)"
        )
        self.assertEqual(len(options), 4)
        self.assertEqual(options[-1]["text"], "Both (2) and (3)")

    def test_chemical_letter_dot_is_not_an_option_label(self):
        _, options, _, _ = parse_options(
            "Pigment?\n(1) Chlorophyll a.     (2) Chlorophyll b.\n"
            "(3) Chlorophyll d.     (4) phycoerythrin."
        )
        self.assertEqual([o["key"] for o in options], ["A", "B", "C", "D"])
        self.assertEqual(options[2]["text"], "Chlorophyll d.")


class DigitalLayoutTests(SimpleTestCase):
    def test_columns_cross_page_diagrams_and_numbered_instructions(self):
        with fitz.open(stream=make_two_column_pdf(), filetype="pdf") as doc:
            questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([q.number for q in questions], [1, 2, 3, 4])
        q1, q2, q3, q4 = questions
        self.assertEqual([q.page_number for q in questions], [2, 2, 2, 3])
        self.assertTrue(all(len(q.options) == 4 for q in questions))
        self.assertEqual(q1.options[-1]["text"], "Both (2) and (3)")
        self.assertNotIn("PRACTICE", q2.text)
        self.assertNotIn("footer", q2.text.lower())
        self.assertIn("Math", " ".join(q2.issues))
        self.assertIn("(A) Calyx", q4.text)
        self.assertEqual(q4.options[0]["text"], "A-IV")
        self.assertTrue(any("matching" in issue.lower() for issue in q4.issues))
        self.assertIsNone(q4.image)  # No repeated footer logo.
        for question, color, page_number in [(q1, (255, 0, 0), 2), (q2, (0, 128, 0), 2), (q3, (0, 0, 255), 3)]:
            self.assertEqual(question.image[0], page_number)
            with Image.open(io.BytesIO(question.image[1])) as crop:
                self.assertEqual(crop.convert("RGB").getpixel((crop.width // 2, crop.height // 2)), color)

    def test_number_gaps_are_reported_without_fabricating_questions(self):
        doc = fitz.open()
        page = doc.new_page()
        for index, number in enumerate((1, 3)):
            y = 80 + index * 100
            page.insert_text((30, y), f"Q{number} Choose an answer.")
            page.insert_text((50, y + 20), "(1) Yes")
            page.insert_text((50, y + 40), "(2) No")
        logs = []
        questions = DigitalQuestionLayout(doc, logs.append).extract()
        self.assertEqual([question.number for question in questions], [1, 3])
        self.assertTrue(any("numbers jump" in message for message in logs))
        doc.close()

    def test_image_crossing_question_boundary_is_flagged(self):
        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 First question?")
        page.insert_text((55, 100), "(1) Yes")
        page.insert_text((55, 120), "(2) No")
        page.insert_image(fitz.Rect(55, 185, 135, 245), stream=image_bytes("red"))
        page.insert_text((30, 210), "Q2 Second question?")
        page.insert_text((55, 270), "(1) Yes")
        page.insert_text((55, 290), "(2) No")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertTrue(any("crosses" in issue for issue in questions[1].issues))
        doc.close()

    def test_possible_option_image_is_flagged_not_misassigned(self):
        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 Identify the correct option.")
        page.insert_text((55, 120), "(1) First")
        page.insert_image(fitz.Rect(80, 135, 160, 195), stream=image_bytes("red"))
        page.insert_text((55, 220), "(2) Second")
        page.insert_text((55, 240), "(3) Third")
        page.insert_text((55, 260), "(4) Fourth")
        question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        self.assertIsNone(question.image)
        self.assertTrue(any("option image" in issue for issue in question.issues))
        doc.close()

    def test_reused_diagram_in_body_is_not_dropped_as_a_logo(self):
        doc = fitz.open()
        diagram = image_bytes("red")
        for number in (1, 2):
            page = doc.new_page(width=600, height=800)
            page.insert_text((30, 120), f"Q{number} Identify this figure.")
            page.insert_image(fitz.Rect(55, 150, 135, 210), stream=diagram)
            page.insert_text((55, 240), "(1) Yes")
            page.insert_text((55, 260), "(2) No")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertTrue(all(question.image for question in questions))
        doc.close()

    def test_multiple_stem_images_are_not_silently_discarded(self):
        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 Compare the two diagrams.")
        page.insert_image(fitz.Rect(55, 100, 135, 160), stream=image_bytes("red"))
        page.insert_image(fitz.Rect(155, 100, 235, 160), stream=image_bytes("blue"))
        page.insert_text((55, 190), "(1) First")
        page.insert_text((55, 210), "(2) Second")
        question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        self.assertIsNotNone(question.image)
        self.assertEqual(len(question.extra_figures), 1)
        self.assertEqual(question.extra_figures[0]["option_key"], "")
        self.assertTrue(any("Multiple original diagrams" in issue for issue in question.issues))
        doc.close()

    def test_mixed_numbering_keeps_numbered_question_before_first_prefixed_question(self):
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 80), "1. First numbered question?")
        page.insert_text((50, 100), "(A) Yes")
        page.insert_text((50, 120), "(B) No")
        page.insert_text((50, 160), "Q2 Second question?")
        page.insert_text((50, 180), "(A) Yes")
        page.insert_text((50, 200), "(B) No")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([question.number for question in questions], [1, 2])
        self.assertEqual([len(question.options) for question in questions], [2, 2])
        doc.close()

    def test_digit_only_document_uses_fallback(self):
        with fitz.open(stream=make_numbered_pdf(), filetype="pdf") as doc:
            self.assertIsNone(DigitalQuestionLayout(doc, lambda _: None).extract())


class ExtractionAndApprovalSafetyTests(TestCase):
    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.category = Category.objects.create(name="Science", slug="science", is_active=True)
        self.teacher = User.objects.create_user(
            username="layout_teacher", password="Password123!", role=User.Roles.TEACHER, is_staff=True
        )

    def paper(self, content, title="Example"):
        return QuestionPaperUpload.objects.create(
            title=title, category=self.category, subject="Science", uploaded_by=self.teacher,
            pdf_file=SimpleUploadedFile("paper.pdf", content, content_type="application/pdf"),
        )

    def test_persist_correct_diagrams_and_source_links(self):
        paper = self.paper(make_two_column_pdf())
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 4)
        paper.refresh_from_db()
        q1 = paper.extracted_questions.get(question_number=1)
        q3 = paper.extracted_questions.get(question_number=3)
        self.assertEqual(q1.page_number, 2)
        self.assertTrue(q1.image.name.endswith("_q1_p2.png"))
        self.assertTrue(q3.image.name.endswith("_q3_p3.png"))
        self.assertFalse(paper.extracted_questions.get(question_number=4).image)
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("questions:paper_review", args=[paper.pk]))
        self.assertContains(response, f"/questions/papers/{paper.pk}/pdf/#page=2")
        self.assertContains(response, "Flagged or incomplete questions are skipped")
        # The correct original crop is carried into the approved question.
        self.assertFalse(q1.is_flagged, q1.flag_reason)
        self.client.post(reverse("questions:extracted_approve", args=[q1.pk]))
        q1.refresh_from_db()
        self.assertEqual(q1.approved_question.image.name, q1.image.name)

    def test_basic_fallback_does_not_attach_unrelated_page_logo(self):
        paper = self.paper(make_numbered_pdf())
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 1)
        item = paper.extracted_questions.get()
        self.assertFalse(item.image)
        self.assertTrue(item.is_flagged)
        self.assertIn("no question diagram", item.flag_reason)
        paper.refresh_from_db()
        self.assertIn("cannot be reliably assigned", paper.extraction_log)
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("questions:paper_review", args=[paper.pk]))
        self.assertContains(response, "Extraction limitations:")
        self.assertContains(response, "diagrams cannot be reliably assigned")

    def test_scanned_page_without_ocr_is_not_reported_as_success(self):
        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        page.insert_image(page.rect, stream=image_bytes("white"))
        content = doc.write()
        doc.close()
        paper = self.paper(content)
        extractor = PDFQuestionExtractor(paper)
        extractor.ocr_available = False
        self.assertEqual(extractor.extract(), 0)
        paper.refresh_from_db()
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.FAILED)
        self.assertIn("Tesseract OCR is not installed", paper.extraction_log)

    def test_failure_does_not_delete_prior_review_work(self):
        paper = self.paper(make_two_column_pdf())
        original = ExtractedQuestion.objects.create(
            paper=paper, question_number=99, question_text="Previously reviewed question", options=[]
        )
        with patch.object(DigitalQuestionLayout, "extract", side_effect=RuntimeError("bad layout")):
            self.assertEqual(PDFQuestionExtractor(paper).extract(), 0)
        self.assertTrue(ExtractedQuestion.objects.filter(pk=original.pk).exists())
        paper.refresh_from_db()
        self.assertIn("bad layout", paper.extraction_log)

    def test_database_failure_rolls_back_replacing_existing_staging_items(self):
        paper = self.paper(make_two_column_pdf())
        original = ExtractedQuestion.objects.create(
            paper=paper, question_number=99, question_text="Previous work", options=[]
        )
        extractor = PDFQuestionExtractor(paper)
        original_save = extractor._save_item
        calls = []

        def fail_on_second(item):
            calls.append(item)
            if len(calls) == 2:
                raise RuntimeError("simulated save failure")
            return original_save(item)

        with patch.object(extractor, "_save_item", side_effect=fail_on_second):
            self.assertEqual(extractor.extract(), 0)
        self.assertEqual(list(paper.extracted_questions.values_list("pk", flat=True)), [original.pk])

    def test_flagged_or_invalid_questions_cannot_be_approved(self):
        paper = self.paper(make_two_column_pdf())
        good = ExtractedQuestion.objects.create(
            paper=paper, question_number=1, question_text="Good?",
            options=[{"key": "A", "text": "Yes"}, {"key": "B", "text": "No"}],
        )
        flagged = ExtractedQuestion.objects.create(
            paper=paper, question_number=2, question_text="Needs review?", is_flagged=True,
            flag_reason="Check diagram", options=good.options,
        )
        incomplete = ExtractedQuestion.objects.create(
            paper=paper, question_number=3, question_text="Incomplete?", options=[{"key": "A", "text": "Only"}]
        )
        self.client.force_login(self.teacher)
        self.client.post(reverse("questions:extracted_approve", args=[flagged.pk]))
        self.client.post(reverse("questions:extracted_approve", args=[incomplete.pk]))
        self.assertEqual(Question.objects.count(), 0)
        self.client.post(reverse("questions:paper_bulk_approve", args=[paper.pk]))
        good.refresh_from_db()
        flagged.refresh_from_db()
        incomplete.refresh_from_db()
        paper.refresh_from_db()
        self.assertEqual(good.status, ExtractedQuestion.Status.APPROVED)
        self.assertEqual(flagged.status, ExtractedQuestion.Status.EXTRACTED)
        self.assertEqual(incomplete.status, ExtractedQuestion.Status.EXTRACTED)
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.EXTRACTED)
        self.assertEqual(Question.objects.count(), 1)
