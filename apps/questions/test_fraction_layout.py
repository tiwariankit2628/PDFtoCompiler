"""Regression tests for inlining vertically-stacked fractions.

Real question papers render fractions as a numerator above a horizontal rule and
a denominator below it. PyMuPDF's flat reading order splits these across lines,
which scrambled option text (e.g. "R/8 inside the cone" became "inside the cone
8 R") and leaked the numerator into the preceding statement. These synthetic
fixtures reproduce that geometry; no user PDF is stored.
"""
import re
import tempfile

import pymupdf as fitz
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from apps.accounts.models import User
from apps.exams.models import Category
from .extractor import PDFQuestionExtractor
from .models import QuestionPaperUpload
from .pdf_math import inline_stacked_fractions


def stacked_fraction_pdf(rows, stem="Q1 The centre of mass of the composite body is located at:"):
    """Build a one-question page whose options contain stacked fractions.

    Each row is ((numerator, denominator) or None, option_text). The numerator is
    printed above a drawn rule and the denominator below it, as in a real paper.
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 80), stem, fontsize=11)
    y = 160
    for index, (frac, text) in enumerate(rows):
        page.insert_text((45, y + 3.5), f"({index + 1})", fontsize=11)
        if frac:
            numerator, denominator = frac
            page.insert_text((70, y - 5.5), numerator, fontsize=11)
            page.draw_line((69, y), (69 + 8 + 4 * len(numerator), y))
            page.insert_text((70, y + 12.5), denominator, fontsize=11)
        page.insert_text((95, y + 3.5), text, fontsize=11)
        y += 45
    data = doc.write()
    doc.close()
    return data


DEFAULT_ROWS = [
    (("R", "8"), "inside the cone"),
    (("R", "4"), "inside the cone"),
    (("5R", "8"), "inside the shell"),
    (None, "at the common base"),
]


class InlineStackedFractionUnitTests(SimpleTestCase):
    def test_reconstructs_option_fractions_in_reading_order(self):
        with fitz.open(stream=stacked_fraction_pdf(DEFAULT_ROWS), filetype="pdf") as doc:
            page = doc[0]
            text = inline_stacked_fractions(page, page.rect, page.get_drawings())
        self.assertIsNotNone(text)
        # Collapse the layout spacing the builder preserves for the option parser.
        compact = re.sub(r"[ \t]+", " ", text)
        self.assertIn("(1) R/8 inside the cone", compact)
        self.assertIn("(2) R/4 inside the cone", compact)
        self.assertIn("(3) 5R/8 inside the shell", compact)
        self.assertIn("(4) at the common base", compact)
        # The numerator must not float above / before the option label.
        self.assertNotRegex(text, r"\n\s*R\s*\n")

    def test_plain_mcq_text_is_unchanged(self):
        # A plain MCQ (no 2-D maths) reads exactly as its native text.
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 80), "Q1 Which gas is lightest?", fontsize=11)
        for i, opt in enumerate(["(1) Hydrogen", "(2) Helium", "(3) Oxygen", "(4) Neon"]):
            page.insert_text((60, 120 + 20 * i), opt, fontsize=11)
        doc_bytes = doc.write()
        doc.close()
        with fitz.open(stream=doc_bytes, filetype="pdf") as d:
            text = inline_stacked_fractions(d[0], d[0].rect, d[0].get_drawings())
            native = d[0].get_text("text", clip=d[0].rect, sort=True)
        # No "/" fraction token invented; same words in the same order.
        self.assertNotIn("/", text)
        self.assertEqual(text.split(), native.split())

    def test_wide_rule_is_not_treated_as_a_fraction(self):
        # A full-width rule with text above and below (a table/section divider)
        # must not be merged into a fraction token.
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 100), "Header row above the divider line here", fontsize=11)
        page.draw_line((40, 112), (520, 112))  # 480pt wide >> a fraction bar
        page.insert_text((60, 128), "Body row below the divider line here", fontsize=11)
        doc_bytes = doc.write()
        doc.close()
        with fitz.open(stream=doc_bytes, filetype="pdf") as d:
            text = inline_stacked_fractions(d[0], d[0].rect, d[0].get_drawings())
        self.assertNotIn("/", text)
        self.assertIn("Header row above", text)
        self.assertIn("Body row below", text)


class StackedFractionExtractionTests(TestCase):
    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.category = Category.objects.create(name="Phys", slug="phys", is_active=True)
        self.teacher = User.objects.create_user(
            username="frac_teacher", password="Password123!", role=User.Roles.TEACHER, is_staff=True
        )

    def paper(self, content):
        return QuestionPaperUpload.objects.create(
            title="Fractions", category=self.category, subject="Physics", uploaded_by=self.teacher,
            pdf_file=SimpleUploadedFile("paper.pdf", content, content_type="application/pdf"),
            target_type="jee",
        )

    def test_option_fractions_extracted_and_not_leaked_into_stem(self):
        paper = self.paper(stacked_fraction_pdf(DEFAULT_ROWS))
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 1)
        q1 = paper.extracted_questions.get(question_number=1)
        by_key = {o["key"]: o["text"] for o in q1.options}
        self.assertEqual(by_key["A"], "R/8 inside the cone")
        self.assertEqual(by_key["B"], "R/4 inside the cone")
        self.assertEqual(by_key["C"], "5R/8 inside the shell")
        self.assertEqual(by_key["D"], "at the common base")
        # The numerator ("R"/"5R") must not leak into the statement.
        self.assertNotIn("R/8", q1.question_text)
        self.assertNotRegex(q1.question_text, r"(?:^|\s)R\s*$")

    def test_negative_numerator_fraction_options(self):
        # Mirrors a work/energy answer like -800/3 J and -1600/3 J.
        rows = [
            (("800", "3"), "J"),
            (("1600", "3"), "J"),
            (None, "-400 J"),
            (None, "-800 J"),
        ]
        paper = self.paper(stacked_fraction_pdf(rows, stem="Q1 The work done by gravity is:"))
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 1)
        q1 = paper.extracted_questions.get(question_number=1)
        by_key = {o["key"]: o["text"] for o in q1.options}
        self.assertEqual(by_key["A"], "800/3 J")
        self.assertEqual(by_key["B"], "1600/3 J")
        # No denominator digit left stranded in the statement.
        self.assertNotIn("800/3", q1.question_text)
