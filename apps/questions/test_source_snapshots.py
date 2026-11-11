"""Source-backed visual fallbacks for uncertain local PDF extraction."""
import io
import json
import os
import tempfile
from unittest import skipUnless
from unittest.mock import patch

import pymupdf as fitz
from PIL import Image, ImageChops
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category, ExamAttempt
from .test_helpers import response_body
from .extractor import PDFQuestionExtractor
from .models import QuestionPaperUpload
from .pdf_layout import DigitalQuestionLayout, _find_geometric_tables
from .test_upgrade import ruled_table_pdf


def math_pdf(*, continuation=False, fraction_size=7):
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((40, 80), "Q1 Choose the value of the fraction.")
    coordinates = [(1, 140), (2, 180)] if continuation else [(n, 140 + (n - 1) * 40) for n in range(1, 5)]
    for number, y in coordinates:
        page.insert_text((50, y), f"({number})")
        page.insert_text((78, y - 5), str(number), fontsize=fraction_size)
        page.draw_line((77, y - 2), (88, y - 2), width=.6)
        page.insert_text((80, y + 6), "x", fontsize=fraction_size)
    if continuation:
        page = doc.new_page(width=600, height=800)
        for number, y in ((3, 90), (4, 130)):
            page.insert_text((50, y), f"({number})")
            page.insert_text((78, y - 5), str(number), fontsize=fraction_size)
            page.draw_line((77, y - 2), (88, y - 2), width=.6)
            page.insert_text((80, y + 6), "x", fontsize=fraction_size)
    page.insert_text((40, 350), "Q2 A separate question must not be captured.")
    for index, word in enumerate(("A", "B", "C", "D"), 1):
        page.insert_text((50, 350 + index * 25), f"({index}) {word}")
    return doc


def parallel_pdf():
    doc = fitz.open()
    page = doc.new_page(width=600, height=850)
    for number, y in ((1, 80), (2, 310), (3, 540)):
        page.insert_text((30, y), f"{number}. H$m$m {{{number}}} $` localized content")
        page.insert_text((330, y), f"{number}. Which of the following is item {number}?")
        for option in range(1, 5):
            page.insert_text((45, y + 25 * option), f"({option}) alternate {number}-{option}")
            page.insert_text((345, y + 25 * option), f"({option}) English {number}-{option}")
    return doc


def raster_table_pdf():
    source = fitz.open(); original = source.new_page(width=600, height=800)
    lines = [(40, 75, "Q1 Which value appears in the table?"),
             (70, 115, "Item"), (230, 115, "Value"),
             (70, 145, "Apple"), (230, 145, "10"),
             (70, 175, "Pear"), (230, 175, "20"),
             (50, 220, "(1) Ten"), (50, 245, "(2) Twenty"),
             (50, 270, "(3) Thirty"), (50, 295, "(4) Forty")]
    for x, y, text in lines:
        original.insert_text((x, y), text)
    original.draw_rect(fitz.Rect(60, 95, 320, 190))
    original.draw_line((210, 95), (210, 190)); original.draw_line((60, 125), (320, 125))
    raster = original.get_pixmap(dpi=144).tobytes("png"); source.close()
    doc = fitz.open(); scan = doc.new_page(width=600, height=800)
    scan.insert_image(scan.rect, stream=raster)
    # A PDF with an existing invisible OCR layer should still use the original
    # raster pixels for snapshots, not render an invented table or answer.
    for x, y, text in lines:
        scan.insert_text((x, y), text, render_mode=3)
    return doc


def source_text(page, figure):
    x0, y0, x1, y1 = figure["box"]
    rect = fitz.Rect(x0 * page.rect.width, y0 * page.rect.height,
                     x1 * page.rect.width, y1 * page.rect.height)
    return page.get_text("text", clip=rect), rect


class TableDetectorCompatibilityTests(SimpleTestCase):
    def test_geometric_tables_do_not_depend_on_a_global_layout_model(self):
        class ModernPage:
            def find_tables(self, **kwargs):
                self.kwargs = kwargs
                return "geometry"
        page = ModernPage()
        self.assertEqual(_find_geometric_tables(page, strategy="text"), "geometry")
        self.assertIs(page.kwargs["use_layout"], False)
        self.assertEqual(page.kwargs["strategy"], "text")

    def test_older_pymupdf_retries_only_for_the_unsupported_keyword(self):
        class LegacyPage:
            def find_tables(self, **kwargs):
                if "use_layout" in kwargs:
                    raise TypeError("find_tables() got an unexpected keyword argument 'use_layout'")
                return kwargs
        self.assertEqual(_find_geometric_tables(LegacyPage(), strategy="text"), {"strategy": "text"})
        class BrokenPage:
            def find_tables(self, **kwargs):
                raise TypeError("damaged table geometry")
        with self.assertRaisesRegex(TypeError, "damaged"):
            _find_geometric_tables(BrokenPage())


class SourceSnapshotTests(SimpleTestCase):
    def test_complex_options_and_stem_preserve_original_pixels_and_boundaries(self):
        doc = math_pdf(); self.addCleanup(doc.close)
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([q.number for q in questions], [1, 2])
        self.assertEqual([o["key"] for o in questions[0].options], list("ABCD"))
        sources = {figure["source"]: figure for figure in questions[0].extra_figures}
        self.assertIn("stem-source", sources)
        self.assertIn("option-source", sources)
        self.assertFalse(any(f["source"].endswith("-source") for f in questions[1].extra_figures))
        text, rect = source_text(doc[0], sources["option-source"])
        for label in ("(1)", "(2)", "(3)", "(4)"):
            self.assertIn(label, text)
        self.assertNotIn("Q2", text)
        self.assertLess(rect.y1, 340)
        image = Image.open(io.BytesIO(sources["option-source"]["data"])).convert("RGB")
        self.assertLessEqual(max(image.size), 2200)
        self.assertIsNotNone(ImageChops.difference(image, Image.new("RGB", image.size, "white")).getbbox())
        self.assertTrue(any("Original answer choices" in issue for issue in questions[0].issues))
        self.assertTrue(any("Original question region" in issue for issue in questions[0].issues))

    def test_incomplete_visual_choices_keep_bounded_source_without_inventing_options(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "Q1 Which graph matches the relationship?")
        # Vector-only choices and labels have no extractable option text.
        for y in (130, 180, 230, 280):
            page.draw_rect(fitz.Rect(80, y, 140, y + 30))
            page.draw_line((85, y + 24), (135, y + 5))
        page.insert_text((40, 360), "Q2 A separate question must not be captured.")
        for index, word in enumerate(("One", "Two", "Three", "Four"), 1):
            page.insert_text((50, 360 + index * 25), f"({index}) {word}")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([q.number for q in questions], [1, 2])
        self.assertEqual(questions[0].options, [])
        source = next(f for f in questions[0].extra_figures if f["source"] == "stem-source")
        text, rect = source_text(page, source)
        self.assertIn("Q1", text); self.assertNotIn("Q2", text)
        self.assertLess(rect.y1, 350)
        image = Image.open(io.BytesIO(source["data"])).convert("RGB")
        self.assertIsNotNone(ImageChops.difference(image, Image.new("RGB", image.size, "white")).getbbox())
        self.assertTrue(any("manually repair" in issue for issue in questions[0].issues))

    def test_incomplete_source_rejects_unbounded_or_printed_answer_regions(self):
        for with_next, with_answer in ((False, False), (True, True)):
            with self.subTest(with_next=with_next, with_answer=with_answer):
                doc = fitz.open(); page = doc.new_page(width=600, height=800)
                page.insert_text((40, 80), "Q1 Which graph matches?")
                page.draw_rect(fitz.Rect(80, 130, 140, 160))
                if with_answer:
                    page.insert_text((40, 200), "Answer: B")
                if with_next:
                    page.insert_text((40, 350), "Q2 Another question")
                    page.insert_text((50, 380), "(1) First")
                    page.insert_text((50, 410), "(2) Second")
                question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
                self.assertEqual(question.options, [])
                self.assertFalse(any(f["source"] == "stem-source" for f in question.extra_figures))
                if with_answer:
                    self.assertTrue(any("printed answer" in issue for issue in question.issues))
                doc.close()

    def test_incomplete_source_uses_a_sequential_next_page_boundary(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        first = doc.new_page(width=600, height=800)
        first.insert_text((40, 480), "Q1 Which diagram matches?")
        first.draw_rect(fitz.Rect(80, 520, 140, 550))
        second = doc.new_page(width=600, height=800)
        second.insert_text((40, 80), "Q2 Another question")
        second.insert_text((50, 110), "(1) First")
        second.insert_text((50, 140), "(2) Second")
        question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        self.assertEqual(question.options, [])
        source = next(f for f in question.extra_figures if f["source"] == "stem-source")
        self.assertEqual(source["page"], 1)
        self.assertLessEqual(source["box"][1], .6)

    def test_same_sized_positioned_fractions_are_preserved_as_source(self):
        doc = math_pdf(fraction_size=11); self.addCleanup(doc.close)
        question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        self.assertTrue(any(f["source"] == "option-source" for f in question.extra_figures))
        self.assertTrue(any("fraction-like" in issue for issue in question.issues))

    def test_option_snapshots_follow_the_source_across_pages(self):
        doc = math_pdf(continuation=True); self.addCleanup(doc.close)
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([q.number for q in questions], [1, 2])
        option_sources = [f for f in questions[0].extra_figures if f["source"] == "option-source"]
        self.assertEqual([f["page"] for f in option_sources], [1, 2])
        first, _ = source_text(doc[0], option_sources[0])
        second, _ = source_text(doc[1], option_sources[1])
        self.assertIn("(1)", first); self.assertIn("(2)", first)
        self.assertIn("(3)", second); self.assertIn("(4)", second)
        self.assertNotIn("Q2", second)

    def test_structured_table_keeps_original_table_region_too(self):
        with fitz.open(stream=ruled_table_pdf(), filetype="pdf") as doc:
            question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
            self.assertTrue(question.tables)
            figures = [f for f in question.extra_figures if f["source"] == "table-source"]
            self.assertEqual(len(figures), 1)
            text, rect = source_text(doc[0], figures[0])
            for cell in ("Column", "Alpha", "Beta"):
                self.assertIn(cell, text)
            self.assertNotIn("(1)", text)
            self.assertLess(rect.y1, 200)
            self.assertTrue(any("Original table" in issue for issue in question.issues))

    def test_scanned_page_with_text_layer_preserves_unstructured_table_and_choices(self):
        doc = raster_table_pdf(); self.addCleanup(doc.close)
        question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        self.assertEqual(len(question.options), 4)
        self.assertFalse(question.tables)  # no invented cell structure from a raster
        table = next(f for f in question.extra_figures if f["source"] == "table-source")
        choices = next(f for f in question.extra_figures if f["source"] == "option-source")
        table_text, _ = source_text(doc[0], table)
        choice_text, _ = source_text(doc[0], choices)
        self.assertIn("Apple", table_text); self.assertIn("20", table_text)
        self.assertNotIn("(1)", table_text)
        self.assertIn("(1)", choice_text); self.assertIn("(4)", choice_text)
        with Image.open(io.BytesIO(table["data"])) as im:
            self.assertLess(min(im.convert("L").getdata()), 50)

    @skipUnless(os.environ.get("LOCAL_OCR_TESSDATA"), "Local Tesseract language data not configured")
    def test_actual_local_ocr_preserves_scanned_question_and_choice_pixels(self):
        original = fitz.open(); page = original.new_page(width=600, height=800)
        page.insert_text((40, 90), "Q1 Which answer is correct?", fontsize=16)
        page.insert_text((60, 160), "(1) First", fontsize=16)
        page.insert_text((60, 195), "(2) Second", fontsize=16)
        raster = page.get_pixmap(dpi=180).tobytes("png"); original.close()
        doc = fitz.open(); self.addCleanup(doc.close)
        scan = doc.new_page(width=600, height=800); scan.insert_image(scan.rect, stream=raster)
        extractor = PDFQuestionExtractor(QuestionPaperUpload(title="OCR fixture"))
        with patch.dict(os.environ, {"TESSDATA_PREFIX": os.environ["LOCAL_OCR_TESSDATA"]}):
            pages, failed = extractor._ocr_textpages(doc, extractor._scanned_page_indices(doc))
            self.assertFalse(failed)
            self.assertIn(0, pages)
            question = DigitalQuestionLayout(doc, lambda _: None, pages).extract()[0]
        self.assertEqual([o["key"] for o in question.options], ["A", "B"])
        self.assertEqual({f["source"] for f in question.extra_figures}, {"stem-source", "option-source"})
        self.assertTrue(any("Local OCR" in issue for issue in question.issues))
        for figure in question.extra_figures:
            with Image.open(io.BytesIO(figure["data"])) as image:
                self.assertLess(min(image.convert("L").getdata()), 50)

    @skipUnless(os.environ.get("LOCAL_OCR_TESSDATA"), "Local Tesseract language data not configured")
    def test_real_local_ocr_separates_parallel_scanned_question_columns(self):
        original = fitz.open(); page = original.new_page(width=900, height=1000)
        for number, y in ((1, 100), (2, 380), (3, 660)):
            for x, localized in ((35, True), (485, False)):
                title = "Alternate statement" if localized else "Which of the following English answers"
                page.insert_text((x, y), f"{number}. {title} {number}?", fontsize=14)
                for option in range(1, 5):
                    page.insert_text((x + 18, y + option * 31),
                                     f"({option}) {'Alternate' if localized else 'English'} {number}-{option}", fontsize=14)
        raster = page.get_pixmap(dpi=180).tobytes("png"); original.close()
        doc = fitz.open(); self.addCleanup(doc.close)
        scan = doc.new_page(width=900, height=1000); scan.insert_image(scan.rect, stream=raster)
        extractor = PDFQuestionExtractor(QuestionPaperUpload(title="Parallel scan"))
        with patch.dict(os.environ, {"TESSDATA_PREFIX": os.environ["LOCAL_OCR_TESSDATA"]}):
            pages, failed = extractor._ocr_textpages(doc, extractor._scanned_page_indices(doc))
            self.assertFalse(failed)
            questions = DigitalQuestionLayout(doc, lambda _: None, pages).extract()
        self.assertEqual([question.number for question in questions], [1, 2, 3])
        self.assertTrue(all(len(question.options) == 4 for question in questions))
        self.assertTrue(all(any(figure["source"] == "parallel-source" for figure in question.extra_figures)
                            for question in questions))
        self.assertTrue(all(any("Local OCR" in issue for issue in question.issues) for question in questions))

    def test_failed_snapshot_render_preserves_parsed_text_and_warns(self):
        doc = math_pdf(); self.addCleanup(doc.close)
        with patch.object(fitz.Page, "get_pixmap", side_effect=ValueError("render error")):
            questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([q.number for q in questions], [1, 2])
        self.assertEqual(len(questions[0].options), 4)
        self.assertTrue(any("Could not save an original source image" in issue for issue in questions[0].issues))

    def test_visible_answer_key_blocks_source_snapshot_and_flags_the_question(self):
        doc = math_pdf(); self.addCleanup(doc.close)
        doc[0].insert_text((50, 300), "Answer: B")
        question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        self.assertFalse(any(f["source"] == "option-source" for f in question.extra_figures))
        self.assertTrue(any("printed answer or solution" in issue for issue in question.issues))
        self.assertTrue(any("source image was omitted" in issue for issue in question.issues))

    def test_printed_answer_without_colon_is_also_blocked(self):
        doc = math_pdf(); self.addCleanup(doc.close)
        doc[0].insert_text((50, 300), "Answer (B) and (D)")
        question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        self.assertFalse(any(f["source"] == "option-source" for f in question.extra_figures))
        self.assertTrue(any("printed answer or solution" in issue for issue in question.issues))
        self.assertTrue(any("source image was omitted" in issue for issue in question.issues))

    def test_no_reliable_option_boundary_does_not_create_wide_stem_snapshot(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "Q1 A formula without identifiable choices")
        page.insert_text((120, 100), "2", fontsize=5)
        question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        self.assertFalse(any(f["source"] in ("stem-source", "option-source") for f in question.extra_figures))

    def test_incomplete_marker_geometry_does_not_create_a_choice_group(self):
        doc = math_pdf(); self.addCleanup(doc.close)
        original = DigitalQuestionLayout._option_label_boxes
        def incomplete(parser, question, text, offset):
            markers = original(parser, question, text, offset)
            if question.number == 1:
                for value in markers.values():
                    del value[1:]
            return markers
        with patch.object(DigitalQuestionLayout, "_option_label_boxes", incomplete):
            question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        self.assertFalse(any(f["source"] == "option-source" for f in question.extra_figures))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SnapshotPrivateWorkflowTests(TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=root.name + "/media", PRIVATE_PRACTICE_ROOT=root.name + "/private",
                                           STATIC_ROOT=root.name + "/static")
        self.settings_override.enable(); self.addCleanup(self.settings_override.disable)
        self.owner = User.objects.create_user(username="snap-owner", email="snap-owner@example.test", password="x", role="STUDENT")
        self.other = User.objects.create_user(username="snap-other", email="snap-other@example.test", password="x", role="STUDENT")
        self.category = Category.objects.create(name="Snapshot Physics", slug="snapshot-physics")

    def test_all_extraction_notes_remain_visible_beyond_short_flag_summary(self):
        doc = math_pdf(); data = doc.write(); doc.close()
        paper = QuestionPaperUpload.objects.create(title="Warnings", category=self.category,
            uploaded_by=self.owner, is_student_private=True, subject="Physics",
            private_pdf_file=SimpleUploadedFile("warnings.pdf", data, content_type="application/pdf"))
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 2)
        item = paper.extracted_questions.get(question_number=1)
        message = "Printed instructions may allow multiple correct choices; review or exclude."
        item.extraction_issues = ["Earlier warning " * 30, message]
        item.flag_reason = "Earlier warning " * 10
        item.save(update_fields=["extraction_issues", "flag_reason"])
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("questions:practice_settings", args=[paper.pk])), message)
        self.assertContains(self.client.get(reverse("questions:practice_question_edit", args=[item.pk])), message)

    def test_incomplete_visual_choices_require_manual_repair_and_stay_ungraded(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "Q1 Which graph matches?")
        for y in (130, 180, 230, 280):
            page.draw_rect(fitz.Rect(80, y, 140, y + 30))
            page.draw_line((85, y + 24), (135, y + 5))
        page.insert_text((40, 360), "Q2 Which value?")
        for index, word in enumerate(("One", "Two", "Three", "Four"), 1):
            page.insert_text((50, 360 + index * 25), f"({index}) {word}")
        data = doc.write(); doc.close()
        paper = QuestionPaperUpload.objects.create(title="Visual choices", category=self.category,
            uploaded_by=self.owner, is_student_private=True, subject="Physics",
            private_pdf_file=SimpleUploadedFile("visual-choices.pdf", data, content_type="application/pdf"))
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 2)
        item = paper.extracted_questions.get(question_number=1)
        self.assertEqual(item.options, [])
        self.assertTrue(item.is_flagged)
        source = item.figures.get(source="stem-source")
        self.assertTrue(source.private_image and not source.image)
        url = reverse("questions:practice_figure_file", args=[source.pk])
        self.client.force_login(self.other)
        self.assertIn(self.client.get(url).status_code, (403, 404))
        self.client.force_login(self.owner)
        review = self.client.get(reverse("questions:practice_settings", args=[paper.pk]))
        self.assertContains(review, "manually repair")
        self.assertContains(review, "At least two non-empty options are required")
        self.assertContains(review, url)
        blocked = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {
            "duration_minutes": 20, "acknowledge_review": "on", "acknowledge_unassigned": "on",
        })
        self.assertEqual(blocked.status_code, 200)
        paper.refresh_from_db(); self.assertIsNone(paper.practice_exam_id)
        edit = self.client.post(reverse("questions:practice_question_edit", args=[item.pk]), {
            "question_text": item.question_text, "option_A": "First graph", "option_B": "Second graph",
            "positive_marks": "4", "negative_marks": "1", "correct_option": "", "reviewed": "on",
        })
        self.assertEqual(edit.status_code, 302, getattr(edit, "context", None))
        ready = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {
            "duration_minutes": 20, "acknowledge_review": "on", "acknowledge_unassigned": "on",
        })
        self.assertEqual(ready.status_code, 302, getattr(ready, "context", None))
        paper.refresh_from_db(); item.refresh_from_db()
        self.assertFalse(item.approved_question.key_verified)
        self.assertTrue(item.approved_question.figures.filter(source="stem-source").exists())
        self.client.post(reverse("exams:exam_start", args=[paper.practice_exam.slug]))
        attempt = ExamAttempt.objects.get(exam=paper.practice_exam)
        session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        payload = session.context["questions_data"][0]
        self.assertTrue(any(image["kind"] == "stem-source" for image in payload["source_images"]))
        selected = next(option for option in payload["options"] if option["key"] == "B")
        saved = self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
            data=json.dumps({"response_id": payload["response_id"], "selected_option_id": selected["id"],
                             "action": "save_next", "time_spent_delta": 0}), content_type="application/json")
        self.assertEqual(saved.status_code, 200)
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]))
        attempt.refresh_from_db()
        self.assertIsNone(attempt.responses.get(id=payload["response_id"]).is_correct)
        self.assertEqual(attempt.score_obtained, 0)

    def test_parallel_sources_and_shuffled_options_keep_confirmed_key_mapping(self):
        doc = parallel_pdf(); data = doc.write(); doc.close()
        paper = QuestionPaperUpload.objects.create(title="Parallel paper", category=self.category,
            uploaded_by=self.owner, is_student_private=True, subject="Physics",
            private_pdf_file=SimpleUploadedFile("parallel.pdf", data, content_type="application/pdf"))
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 3)
        first = paper.extracted_questions.get(question_number=1)
        source = first.figures.get(source="parallel-source")
        self.assertTrue(source.private_image and not source.image)
        self.client.force_login(self.other)
        self.assertIn(self.client.get(reverse("questions:practice_figure_file", args=[source.pk])).status_code, (403, 404))
        self.client.force_login(self.owner)
        review = self.client.get(reverse("questions:practice_settings", args=[paper.pk]))
        self.assertContains(review, "Other original-language region")
        response = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {
            "duration_minutes": 20, "acknowledge_review": "on", "answer_key_text": "1: 2",
        })
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        paper.refresh_from_db(); first.refresh_from_db()
        self.assertTrue(first.approved_question.key_verified)
        bank_source = first.approved_question.figures.get(source="parallel-source")
        self.assertTrue(bank_source.private_image and not bank_source.image)
        exam = paper.practice_exam
        exam.shuffle_options = True
        exam.save(update_fields=["shuffle_options"])
        self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        attempt = ExamAttempt.objects.get(exam=exam)
        expiry = attempt.expires_at
        with patch("apps.exams.views_candidate.random.shuffle", side_effect=lambda values: values.reverse()):
            session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        payload = session.context["questions_data"][0]
        self.assertEqual([option["key"] for option in payload["options"]], ["D", "C", "B", "A"])
        selected = next(option for option in payload["options"] if option["key"] == "B")
        self.assertEqual(selected["text"], "English 1-2")
        source_image = next(img for img in payload["source_images"] if img["kind"] == "parallel-source")
        self.assertNotIn(source_image["url"], payload["image_urls"])
        self.assertContains(session, "Other original-language region")
        file_response = self.client.get(source_image["url"])
        self.assertEqual(file_response.status_code, 200); self.assertTrue(response_body(file_response))
        saved = self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
            data=json.dumps({"response_id": payload["response_id"], "selected_option_id": selected["id"],
                             "action": "save_next", "time_spent_delta": 0}), content_type="application/json")
        self.assertEqual(saved.status_code, 200)
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]))
        attempt.refresh_from_db()
        response = attempt.responses.get(id=payload["response_id"])
        self.assertTrue(response.is_correct)
        self.assertEqual(attempt.score_obtained, response.exam_question.positive_marks)
        self.assertEqual(attempt.expires_at, expiry)
        self.client.force_login(self.other)
        self.assertIn(self.client.get(source_image["url"]).status_code, (403, 404))

    def test_private_source_images_survive_into_cbt_and_stay_owner_only(self):
        doc = math_pdf(); data = doc.write(); doc.close()
        paper = QuestionPaperUpload.objects.create(title="Visual source", category=self.category,
            uploaded_by=self.owner, is_student_private=True, subject="Physics",
            private_pdf_file=SimpleUploadedFile("visual.pdf", data, content_type="application/pdf"))
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 2)
        item = paper.extracted_questions.get(question_number=1)
        self.assertTrue(item.is_flagged)
        source = item.figures.get(source="option-source")
        self.assertTrue(source.private_image and not source.image)
        self.client.force_login(self.other)
        self.assertIn(self.client.get(reverse("questions:practice_figure_file", args=[source.pk])).status_code, (403, 404))
        self.client.force_login(self.owner)
        response = self.client.get(reverse("questions:practice_settings", args=[paper.pk]))
        self.assertContains(response, "Original choices in printed order")
        self.assertEqual(self.client.post(reverse("questions:practice_settings", args=[paper.pk]),
                         {"duration_minutes": 20, "acknowledge_review": "on"}).status_code, 302)
        paper.refresh_from_db(); item.refresh_from_db()
        bank_source = item.approved_question.figures.get(source="option-source")
        self.assertTrue(bank_source.private_image and not bank_source.image)
        self.assertFalse(item.approved_question.key_verified)
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[paper.practice_exam.slug])).status_code, 302)
        attempt = ExamAttempt.objects.get(exam=paper.practice_exam)
        expiry = attempt.expires_at
        session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(session.status_code, 200)
        payload = session.context["questions_data"][0]
        option_source = next(img for img in payload["source_images"] if img["kind"] == "option-source")
        self.assertNotIn(option_source["url"], payload["image_urls"])
        self.assertContains(session, "Original answer choices")
        shown = self.client.get(option_source["url"])
        self.assertEqual(shown.status_code, 200); self.assertTrue(response_body(shown))
        attempt.refresh_from_db(); self.assertEqual(attempt.expires_at, expiry)
        self.client.force_login(self.other)
        self.assertIn(self.client.get(option_source["url"]).status_code, (403, 404))
