"""Source-backed regressions for difficult local PDF layouts.

All figures and tables are synthetic. No user documents are stored here.
"""
import io
import os
import tempfile

import pymupdf as fitz
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category, ExamAttempt
from .test_helpers import response_body
from .extractor import PDFQuestionExtractor
from .models import QuestionPaperUpload
from .practice_forms import practice_question_error
from .pdf_layout import DigitalQuestionLayout
from .pdf_options import reorder_wrapped_options


def layout(doc):
    return DigitalQuestionLayout(doc, lambda _: None).extract()


def borderless_fixture(*, question="Match the following table:", side_note=False, headers=("Column I", "Column II")):
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((40, 80), "Q1 " + question)
    for x, y, value in [(60, 110, headers[0]), (240, 110, headers[1]),
                        (60, 135, "Apple"), (240, 135, "Fruit"),
                        (60, 160, "Carrot"), (240, 160, "Vegetable"),
                        (60, 185, "Potato"), (240, 185, "Tuber")]:
        page.insert_text((x, y), value)
    if side_note:
        page.insert_text((410, 160), "Important side note")
    page.insert_text((60, 225), "(1) Apple is a fruit")
    page.insert_text((300, 225), "(2) Carrot is a tree")
    page.insert_text((80, 245), "and grows on trees")
    page.insert_text((320, 245), "and grows underground")
    page.insert_text((60, 275), "(3) Potato is a tuber")
    page.insert_text((300, 275), "(4) None of these")
    return doc


def disconnected_fixture(*, diagrams=True, stem=False):
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((40, 70), "Q1 Choose the correct figure.")
    if stem:
        for x, y in [(80, 90), (110, 90), (80, 120), (110, 120)]:
            page.draw_rect(fitz.Rect(x, y, x + 8, y + 8), width=1)
        for i in range(4):
            page.insert_text((40, 200 + i * 22), f"({i + 1}) Option {i + 1}")
    else:
        for x, y, label in [(40, 130, "(1)"), (300, 130, "(2)"),
                            (40, 250, "(3)"), (300, 250, "(4)")]:
            page.insert_text((x, y), label)
            if diagrams:
                # Individually too small and too far apart for component
                # detection; collectively form one source-backed option image.
                for dx, dy in [(0, 0), (22, 0), (0, 25), (22, 25)]:
                    page.draw_rect(fitz.Rect(x + 25 + dx, y + 8 + dy,
                                             x + 33 + dx, y + 16 + dy), width=1)
    return doc


class LocalLayoutEnhancementTests(SimpleTestCase):
    def test_wrapped_side_by_side_choices_are_not_interleaved(self):
        doc = borderless_fixture()
        self.addCleanup(doc.close)
        question = layout(doc)[0]
        self.assertEqual([item["text"] for item in question.options], [
            "Apple is a fruit and grows on trees", "Carrot is a tree and grows underground",
            "Potato is a tuber", "None of these",
        ])
        self.assertTrue(any("reordered" in issue for issue in question.issues))

    def test_option_reordering_refuses_to_drop_or_invent_a_token(self):
        doc = borderless_fixture()
        self.addCleanup(doc.close)
        parser = DigitalQuestionLayout(doc, lambda _: None)
        question = parser.extract()[0]
        # A synthetic inconsistency makes exact source accounting fail; the
        # geometry helper must leave the caller's content unchanged.
        options = [dict(option) for option in question.options]
        options[0]["text"] += " EXTRA"
        from .pdf_layout import parse_options
        text = question.segments[0].text.replace(question.label, "", 1)
        _, _, offset, _ = parse_options(text)
        markers = parser._option_label_boxes(question, text, offset)[0]
        self.assertEqual(reorder_wrapped_options(doc[0], question.segments[0], markers, options), {})

    def test_referenced_borderless_table_is_structured_without_losing_cells(self):
        doc = borderless_fixture()
        self.addCleanup(doc.close)
        question = layout(doc)[0]
        self.assertEqual(question.text, "Match the following table: [[table:1]]")
        self.assertEqual([[cell["text"] for cell in row] for row in question.tables[0]["rows"]], [
            ["Column I", "Column II"], ["Apple", "Fruit"], ["Carrot", "Vegetable"], ["Potato", "Tuber"],
        ])
        self.assertTrue(any("Borderless table" in issue for issue in question.issues))
        self.assertNotIn("[[table:", " ".join(o["text"] for o in question.options))

    def test_text_table_detector_does_not_consume_adjacent_prose(self):
        doc = borderless_fixture(side_note=True)
        self.addCleanup(doc.close)
        question = layout(doc)[0]
        all_visible = question.text + " " + " ".join(cell["text"] for table in question.tables for row in table["rows"] for cell in row)
        self.assertIn("Important side note", all_visible)
        self.assertEqual(len(question.options), 4)

    def test_unreferenced_text_columns_are_not_silently_made_a_table(self):
        doc = borderless_fixture(question="Compare these observations.", headers=("Group One", "Group Two"))
        self.addCleanup(doc.close)
        question = layout(doc)[0]
        self.assertEqual(question.tables, [])
        self.assertIn("Apple", question.text)
        self.assertIn("Vegetable", question.text)

    def test_disconnected_image_choices_keep_source_crops_and_ownership(self):
        doc = disconnected_fixture()
        self.addCleanup(doc.close)
        question = layout(doc)[0]
        self.assertEqual({figure["option_key"] for figure in question.extra_figures}, set("ABCD"))
        self.assertTrue(all(figure["source"] == "option-ink" for figure in question.extra_figures))
        self.assertTrue(any("disconnected option diagram" in issue for issue in question.issues))
        for figure in question.extra_figures:
            image = Image.open(io.BytesIO(figure["data"])).convert("L")
            self.assertLess(min(image.getdata()), 50)  # original ink, not an invented graphic
            x0, y0, x1, y1 = figure["box"]
            self.assertTrue(0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1)

    def test_disconnected_option_images_can_continue_on_the_next_page(self):
        doc = fitz.open()
        first = doc.new_page(width=600, height=800)
        first.insert_text((40, 90), "Q1 Choose the figure.")
        def choice(page, x, y, label):
            page.insert_text((x, y), label)
            for dx, dy in [(0, 0), (22, 0), (0, 25), (22, 25)]:
                page.draw_rect(fitz.Rect(x + 25 + dx, y + 8 + dy, x + 33 + dx, y + 16 + dy))
        choice(first, 40, 620, "(1)"); choice(first, 300, 620, "(2)")
        second = doc.new_page(width=600, height=800)
        choice(second, 40, 90, "(3)"); choice(second, 300, 90, "(4)")
        second.insert_text((40, 250), "Q2 Another question?")
        second.insert_text((40, 280), "(1) Yes"); second.insert_text((40, 300), "(2) No")
        self.addCleanup(doc.close)
        questions = layout(doc)
        self.assertEqual([(f["page"], f["option_key"]) for f in questions[0].extra_figures],
                         [(1, "A"), (1, "B"), (2, "C"), (2, "D")])
        self.assertFalse(questions[1].extra_figures)

    def test_empty_choices_without_ink_do_not_get_fabricated_images(self):
        doc = disconnected_fixture(diagrams=False)
        self.addCleanup(doc.close)
        question = layout(doc)[0]
        self.assertFalse(question.extra_figures)
        self.assertFalse(question.image)

    def test_disconnected_stem_candidate_goes_to_review_pool(self):
        doc = disconnected_fixture(stem=True)
        self.addCleanup(doc.close)
        question = layout(doc)[0]
        self.assertFalse(question.image)
        self.assertEqual([(f["option_key"], f["source"]) for f in question.extra_figures], [(None, "stem-ink")])
        self.assertTrue(any("unassigned pool" in issue for issue in question.issues))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DisconnectedGraphicWorkflowTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        settings = override_settings(PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private"),
                                     MEDIA_ROOT=os.path.join(self.root.name, "media"),
                                     STATIC_ROOT=os.path.join(self.root.name, "static"))
        settings.enable(); self.addCleanup(settings.disable)
        self.owner = User.objects.create_user(username="fragment-owner", email="fragment-owner@example.test", role=User.Roles.STUDENT)
        self.other = User.objects.create_user(username="fragment-other", email="fragment-other@example.test", role=User.Roles.STUDENT)
        self.teacher = User.objects.create_user(username="fragment-teacher", email="fragment-teacher@example.test", role=User.Roles.TEACHER, is_staff=True)
        self.category = Category.objects.create(name="Disconnected figures", slug="disconnected-figures")

    def paper(self, *, private):
        doc = disconnected_fixture(); data = doc.write(); doc.close()
        kwargs = {"private_pdf_file" if private else "pdf_file": SimpleUploadedFile("synthetic.pdf", data, content_type="application/pdf")}
        return QuestionPaperUpload.objects.create(title="Fragments", category=self.category,
                    uploaded_by=self.owner if private else self.teacher, is_student_private=private, subject="Physics", **kwargs)

    def test_private_option_fragments_are_owner_only_and_reach_cbt(self):
        paper = self.paper(private=True)
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 1)
        item = paper.extracted_questions.get()
        figures = list(item.figures.order_by("option_key"))
        self.assertEqual([f.option_key for f in figures], list("ABCD"))
        self.assertTrue(all(f.private_image and not f.image and f.source == "option-ink" for f in figures))
        self.assertTrue(item.is_flagged)
        self.assertEqual(practice_question_error(item), "")
        self.client.force_login(self.other)
        self.assertIn(self.client.get(reverse("questions:practice_figure_file", args=[figures[0].pk])).status_code, (403, 404))
        self.client.force_login(self.owner)
        result = self.client.get(reverse("questions:practice_figure_file", args=[figures[0].pk]))
        self.assertEqual(result.status_code, 200); self.assertTrue(response_body(result))
        result = self.client.post(reverse("questions:practice_settings", args=[paper.pk]),
                                  {"duration_minutes": 20, "acknowledge_review": "on"})
        self.assertEqual(result.status_code, 302)
        paper.refresh_from_db(); item.refresh_from_db()
        self.assertFalse(item.approved_question.key_verified)
        self.assertEqual([f.option.option_key for f in item.approved_question.figures.select_related("option").order_by("option__option_key")], list("ABCD"))
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[paper.practice_exam.slug])).status_code, 302)
        attempt = ExamAttempt.objects.get(exam=paper.practice_exam)
        session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertTrue(all(option["image_url"] for option in session.context["questions_data"][0]["options"]))

    def test_teacher_must_review_disconnected_choices_before_approval(self):
        paper = self.paper(private=False)
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 1)
        item = paper.extracted_questions.get()
        self.assertEqual(list(item.figures.order_by("option_key").values_list("option_key", flat=True)), list("ABCD"))
        self.assertTrue(all(f.image and not f.private_image for f in item.figures.all()))
        self.client.force_login(self.teacher)
        self.client.post(reverse("questions:extracted_approve", args=[item.pk]))
        item.refresh_from_db(); self.assertIsNone(item.approved_question)
        self.client.post(reverse("questions:extracted_toggle_flag", args=[item.pk]))
        self.client.post(reverse("questions:extracted_approve", args=[item.pk]))
        item.refresh_from_db()
        self.assertEqual(item.approved_question.figures.count(), 4)
        self.assertFalse(item.approved_question.key_verified)
