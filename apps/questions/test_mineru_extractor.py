"""Tests for the optional local MinerU extraction backend.

The MinerU subprocess is mocked, so these run without the heavy MinerU virtualenv.
All fixtures are synthetic; no user PDF or MinerU model is required.
"""
import io
import json
import os
import tempfile
from unittest.mock import patch

import pymupdf as fitz
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from apps.accounts.models import User
from apps.exams.models import Category
from .models import ExtractedQuestion, QuestionPaperUpload
from .mineru_extractor import (
    clean_markup, split_options, html_table_to_structured,
    parse_mineru_content, mineru_is_configured, MinerUQuestionExtractor,
)


def _png(color="red"):
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, format="PNG")
    return buf.getvalue()


def _one_page_pdf():
    doc = fitz.open()
    doc.new_page(width=400, height=600).insert_text((40, 80), "placeholder")
    data = doc.write()
    doc.close()
    return data


# A synthetic MinerU content list mirroring the real format (bold markers, $...$
# LaTeX, numeric option labels, an image between stem and options, a table).
SYNTHETIC_CONTENT = [
    {"type": "header", "text": "All India Test", "page_idx": 0},
    {"type": "text", "page_idx": 0,
     "text": "**1. A quantity has dimensions** $[\\mathbf{M}^2\\mathbf{L}^{-1}]$ **. The value is:** "
             "(1) $\\frac{R}{8}$ (2) $\\frac{R}{4}$ (3) $\\frac{R}{2}$ (4) $R$"},
    {"type": "text", "page_idx": 0,
     "text": "**2. A composite structure formed from a cone and a shell (see figure).**"},
    {"type": "image", "img_path": "images/img_0.jpg", "page_idx": 0, "bbox": [100, 200, 300, 400]},
    {"type": "text", "page_idx": 0,
     "text": "(1) inside the cone (2) inside the shell (3) at the base (4) at the apex"},
    {"type": "text", "page_idx": 0,
     "text": "<strong><u>Section B: Mechanics</u></strong>"},
    {"type": "text", "page_idx": 0,
     "text": "**3. Match the columns using the table below.** [[t]] "
             "(1) A-1 (2) A-2 (3) A-3 (4) A-4"},
    {"type": "page_number", "text": "[2]", "page_idx": 0},
]

SYNTHETIC_TABLE = {"type": "table", "page_idx": 0,
                   "table_body": "<table><tr><th>List I</th><th>List II</th></tr>"
                                 "<tr><td>Force</td><td>N</td></tr></table>"}


class CleanMarkupTests(SimpleTestCase):
    def test_strips_emphasis_keeps_latex(self):
        self.assertEqual(clean_markup("**bold** and __x__"), "bold and x")
        self.assertEqual(clean_markup("<strong><u>Head</u></strong>"), "Head")
        self.assertEqual(clean_markup("x<sup>2</sup>"), "x^{2}")
        self.assertEqual(clean_markup("keep $\\frac{R}{8}$ math"), "keep $\\frac{R}{8}$ math")


class SplitOptionsTests(SimpleTestCase):
    def test_splits_numeric_options_math_aware(self):
        stem, options, offset = split_options(
            "The value is: (1) $\\frac{R}{8}$ (2) $\\frac{R}{4}$ (3) 10 (4) 20")
        self.assertEqual(stem, "The value is:")
        self.assertEqual([o["key"] for o in options], ["A", "B", "C", "D"])
        self.assertEqual(options[0]["text"].strip(), "$\\frac{R}{8}$")

    def test_does_not_split_on_marker_inside_math(self):
        # A "(2)" inside a $...$ span must not be treated as an option label.
        stem, options, _ = split_options("Given $f(2) = 3$ find (1) a (2) b (3) c")
        self.assertEqual([o["key"] for o in options], ["A", "B", "C"])
        self.assertIn("f(2)", stem)

    def test_no_options_returns_empty(self):
        stem, options, offset = split_options("Explain the working of a motor.")
        self.assertEqual(options, [])
        self.assertIsNone(offset)


class HtmlTableTests(SimpleTestCase):
    def test_simple_table_converts_to_structured(self):
        table = html_table_to_structured(
            "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>")
        self.assertEqual(table["columns"], 2)
        self.assertEqual(len(table["rows"]), 2)
        self.assertTrue(table["rows"][0][0]["header"])
        self.assertEqual(table["rows"][1][1]["text"], "2")

    def test_ragged_table_rejected(self):
        self.assertIsNone(html_table_to_structured(
            "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>"))


class ParseContentTests(SimpleTestCase):
    def test_parses_questions_options_and_figure(self):
        qs = parse_mineru_content(SYNTHETIC_CONTENT, image_dir="/tmp/x")
        by_num = {q["number"]: q for q in qs}
        self.assertEqual(set(by_num), {1, 2, 3})
        # Q1 LaTeX option preserved
        self.assertEqual(by_num[1]["options"][0]["text"].strip(), "$\\frac{R}{8}$")
        # Q2 figure attached to the stem (between stem and options)
        self.assertEqual(len(by_num[2]["figures"]), 1)
        self.assertIsNone(by_num[2]["figures"][0]["option_key"])
        self.assertEqual([o["text"] for o in by_num[2]["options"]],
                         ["inside the cone", "inside the shell", "at the base", "at the apex"])

    def test_section_banner_not_merged_into_question(self):
        qs = parse_mineru_content(SYNTHETIC_CONTENT, image_dir="/tmp/x")
        by_num = {q["number"]: q for q in qs}
        self.assertNotIn("Section B", by_num[2]["text"])
        self.assertNotIn("Mechanics", by_num[2]["text"])

    def test_table_attached_to_stem(self):
        content = SYNTHETIC_CONTENT[:6] + [
            {"type": "text", "page_idx": 0, "text": "**3. Match the columns.**"},
            SYNTHETIC_TABLE,
            {"type": "text", "page_idx": 0, "text": "(1) A-1 (2) A-2 (3) A-3 (4) A-4"},
        ]
        qs = parse_mineru_content(content, image_dir="/tmp/x")
        by_num = {q["number"]: q for q in qs}
        self.assertEqual(len(by_num[3]["tables"]), 1)
        self.assertIn("[[table:1]]", by_num[3]["text"])


class MinerUConfiguredTests(SimpleTestCase):
    @override_settings(MINERU_PYTHON="")
    def test_unconfigured(self):
        self.assertFalse(mineru_is_configured())

    def test_configured_requires_existing_file(self):
        import sys
        with override_settings(MINERU_PYTHON=sys.executable):
            self.assertTrue(mineru_is_configured())
        with override_settings(MINERU_PYTHON=r"C:/nonexistent/python.exe"):
            self.assertFalse(mineru_is_configured())


class MinerUExtractionTests(TestCase):
    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.category = Category.objects.create(name="Phys", slug="phys", is_active=True)
        self.teacher = User.objects.create_user(
            username="mineru_teacher", password="Password123!", role=User.Roles.TEACHER, is_staff=True)

    def _paper(self):
        return QuestionPaperUpload.objects.create(
            title="MinerU Paper", category=self.category, subject="Physics", uploaded_by=self.teacher,
            extraction_mode=QuestionPaperUpload.ExtractionMode.LOCAL_MINERU,
            pdf_file=SimpleUploadedFile("p.pdf", _one_page_pdf(), content_type="application/pdf"), target_type="jee")

    def _fake_run(self, command, **kwargs):
        # Emulate the MinerU subprocess: write content_list.json + an image.
        out_dir = command[command.index("--out") + 1]
        os.makedirs(os.path.join(out_dir, "images"), exist_ok=True)
        with open(os.path.join(out_dir, "images", "img_0.jpg"), "wb") as handle:
            handle.write(_png())
        with open(os.path.join(out_dir, "content_list.json"), "w", encoding="utf-8") as handle:
            json.dump(SYNTHETIC_CONTENT, handle)

        class _Result:
            returncode = 0
            stdout = '{"ok": true}'
            stderr = ""
        return _Result()

    def test_extract_stages_questions_options_and_figure(self):
        import sys
        paper = self._paper()
        with override_settings(MINERU_PYTHON=sys.executable, MINERU_MAX_PAGES=60):
            with patch("apps.questions.mineru_extractor.subprocess.run", side_effect=self._fake_run):
                count = MinerUQuestionExtractor(paper).extract()
        self.assertEqual(count, 3)
        paper.refresh_from_db()
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.EXTRACTED)
        q2 = paper.extracted_questions.get(question_number=2)
        self.assertEqual(len(q2.options), 4)
        self.assertTrue(q2.is_flagged)  # all MinerU output needs review
        self.assertIn("MinerU", q2.flag_reason)
        self.assertTrue(q2.image.name)  # cone figure attached as primary image

    def test_unconfigured_fails_cleanly_without_overwriting(self):
        paper = self._paper()
        with override_settings(MINERU_PYTHON=""):
            count = MinerUQuestionExtractor(paper).extract()
        self.assertEqual(count, 0)
        paper.refresh_from_db()
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.FAILED)
        self.assertIn("not configured", paper.extraction_log)

    def test_subprocess_failure_is_reported(self):
        import sys
        paper = self._paper()

        class _Fail:
            returncode = 1
            stdout = ""
            stderr = "boom"
        with override_settings(MINERU_PYTHON=sys.executable):
            with patch("apps.questions.mineru_extractor.subprocess.run", return_value=_Fail()):
                count = MinerUQuestionExtractor(paper).extract()
        self.assertEqual(count, 0)
        paper.refresh_from_db()
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.FAILED)
        self.assertIn("MinerU subprocess failed", paper.extraction_log)

    def test_existing_review_work_preserved(self):
        import sys
        paper = self._paper()
        ExtractedQuestion.objects.create(paper=paper, question_number=99, question_text="prior", options=[])
        with override_settings(MINERU_PYTHON=sys.executable):
            with patch("apps.questions.mineru_extractor.subprocess.run", side_effect=self._fake_run):
                count = MinerUQuestionExtractor(paper).extract()
        self.assertEqual(count, 0)
        self.assertTrue(paper.extracted_questions.filter(question_number=99).exists())
        self.assertFalse(paper.extracted_questions.filter(question_number=1).exists())
