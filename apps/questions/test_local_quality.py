"""Local-only extraction regressions. Generated fixtures contain no user data."""
import io
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

import pymupdf as fitz
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from apps.accounts.models import User
from apps.exams.models import Category
from .test_helpers import response_body
from .extractor import PDFQuestionExtractor
from .models import QuestionPaperUpload
from .pdf_layout import DigitalQuestionLayout, OPTION_MARKER, _normalize_option, _ocr_line_text, parse_options
from .pdf_graphics import scanned_graphic_candidates
from .pdf_math import reconstruct_option_fractions
from .pdf_tables import reconstruct_cell_fraction
from .test_pdf_layout import image_bytes


def numeric_columns_pdf():
    doc = fitz.open()
    cover = doc.new_page(width=600, height=800)
    cover.insert_text((30, 100), "1. Fill in your registration details.")
    cover.insert_text((30, 120), "2. Use a black pen.")
    page = doc.new_page(width=600, height=800)
    for n, x, y in [(1, 40, 90), (2, 40, 350), (3, 340, 90), (4, 340, 350)]:
        page.insert_text((x, y), f"{n}. Which answer belongs to question {n}?")
        if n == 1:
            page.insert_image(fitz.Rect(x + 25, y + 18, x + 105, y + 78), stream=image_bytes("red"))
        for i, word in enumerate(("First", "Second", "Third", "Fourth")):
            page.insert_text((x + 20, y + 105 + 20 * i), f"{chr(65+i)}) {word} {n}")
    data = doc.write(); doc.close(); return data


def mixed_pdf():
    doc = fitz.open()
    digital = doc.new_page(width=600, height=800)
    digital.insert_text((40, 100), "Q1 Which diagram is shown?")
    digital.insert_image(fitz.Rect(60, 120, 140, 180), stream=image_bytes("red"))
    digital.insert_text((60, 205), "(1) Red")
    digital.insert_text((60, 225), "(2) Blue")
    original_scan = fitz.open()
    page = original_scan.new_page(width=600, height=800)
    page.insert_text((40, 100), "Q2 What is the correct answer?", fontsize=16)
    page.insert_text((60, 140), "(1) Yes", fontsize=16)
    page.insert_text((60, 175), "(2) No", fontsize=16)
    raster = page.get_pixmap(dpi=180).tobytes("png")
    scan = doc.new_page(width=600, height=800)
    scan.insert_image(scan.rect, stream=raster)
    data = doc.write(); doc.close(); original_scan.close(); return data


class LocalOptionTests(SimpleTestCase):
    def test_ocr_word_spacing_uses_source_gaps_without_splitting_adjacent_glyphs(self):
        def span(text, x0, x1):
            return {"text": text, "bbox": (x0, 0, x1, 20)}
        spans = [span("भारत", 0, 36), span("की", 41, 61), span("राजधानी", 66, 120),
                 span("?", 121, 125)]
        self.assertEqual(_ocr_line_text(spans), "भारत की राजधानी?")
        self.assertEqual(_ocr_line_text([span("अ", 0, 10), span("क्ष", 10.5, 20)]), "अक्ष")
        self.assertEqual(_ocr_line_text([span("Hello", 0, 25), span(" ", 25, 29), span("world", 29, 55)]), "Hello world")

    def test_suffix_bracket_and_five_answer_formats(self):
        for labels in [("A)", "B)", "C)", "D)", "E)"),
                       ("[A]", "[B]", "[C]", "[D]", "[E]"),
                       ("1)", "2)", "3)", "4)", "5)"),
                       ("(१)", "(२)", "(३)", "(४)", "(५)")]:
            with self.subTest(labels=labels):
                text = "Choose one:\n" + "\n".join(f"{label} Answer {i}" for i, label in enumerate(labels))
                statement, options, _, _ = parse_options(text)
                self.assertEqual(statement, "Choose one:")
                self.assertEqual([o["key"] for o in options], list("ABCDE"))
                self.assertEqual([o["text"] for o in options], [f"Answer {i}" for i in range(5)])

    def test_fallback_render_failure_keeps_readable_digital_questions(self):
        with fitz.open(stream=mixed_pdf(), filetype="pdf") as doc:
            extractor = PDFQuestionExtractor(QuestionPaperUpload(title="fallback"))
            extractor.ocr_available = True
            with patch.object(fitz.Page, "get_pixmap", side_effect=Exception("damaged scan")):
                items = extractor._extract_text_fallback(doc, scanned_pages={1})
        self.assertEqual([item.number for item in items], [1])
        self.assertEqual(len(items[0].options), 2)
        self.assertTrue(any("OCR extraction error" in line for line in extractor.logs))

    def test_basic_fallback_understands_letter_parentheses_and_fifth_choice(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "Q1 Choose one?")
        for index, label in enumerate("ABCDE"):
            page.insert_text((60, 100 + 20 * index), f"{label}) Answer {index + 1}")
        extractor = PDFQuestionExtractor(QuestionPaperUpload(title="fallback"))
        items = extractor._extract_text_fallback(doc); doc.close()
        self.assertEqual(len(items), 1)
        self.assertEqual([o["key"] for o in items[0].options], list("ABCDE"))
        self.assertTrue(any("Basic extraction" in issue for issue in items[0].issues))

    def test_five_statement_labels_before_four_answers(self):
        _, options, _, _ = parse_options("Match:\n(A) a\n(B) b\n(C) c\n(D) d\n(E) e\n"
                                          "(1) one\n(2) two\n(3) three\n(4) four")
        self.assertEqual([o["text"] for o in options], ["one", "two", "three", "four"])


class GeometryMathTests(SimpleTestCase):
    @staticmethod
    def geometry_page(*, radical_bar=True, accents=False, unit=False, type3_font="Type3Math"):
        """A minimal positioned glyph stream, not copied from any user PDF."""
        def span(text, x, y, *, font=None):
            return {"font": font or type3_font, "size": 10, "flags": 0, "chars": [
                {"c": char, "bbox": (x + 5 * i, y, x + 5 * (i + 1), y + 8),
                 "origin": (x + 5 * i, y + 8)} for i, char in enumerate(text)
            ]}
        lines = [
            {"spans": [span("(1)", 10, 100, font="Helvetica")]},
            {"spans": [span("a", 30, 94)]},
            {"spans": [span("10", 30, 106)]},
            {"spans": [span("√74", 44, 100)]},
            {"spans": [span("(2) No", 10, 140, font="Helvetica")]},
        ]
        if accents:
            lines.append({"spans": [span("--", 49, 94)]})
        if unit:
            lines.append({"spans": [span("N", 60, 100, font="Helvetica")]})
        raw = {"blocks": [{"type": 0, "lines": lines}]}
        class Page:
            def get_text(self, kind, clip):
                assert kind == "rawdict"
                return raw
        drawings = [{"rect": fitz.Rect(29, 103, 40, 103.2)}]
        if radical_bar:
            drawings.append({"rect": fitz.Rect(49, 99, 59, 99.2)})
        return Page(), drawings

    def test_multicharacter_root_with_fraction_and_source_rule(self):
        segment = SimpleNamespace(rect=fitz.Rect(0, 0, 200, 200))
        options = [{"key": "A", "text": "raw"}, {"key": "B", "text": "No"}]
        page, drawings = self.geometry_page()
        result = reconstruct_option_fractions(page, segment, options, OPTION_MARKER, _normalize_option, drawings)
        self.assertEqual(result["A"], r"\(\frac{a}{10}\sqrt{74}\)")
        self.assertEqual(result["B"], "No")

    def test_multicharacter_root_accepts_raised_glyph_overbars(self):
        segment = SimpleNamespace(rect=fitz.Rect(0, 0, 200, 200))
        options = [{"key": "A", "text": "raw"}, {"key": "B", "text": "No"}]
        page, drawings = self.geometry_page(radical_bar=False, accents=True)
        result = reconstruct_option_fractions(page, segment, options, OPTION_MARKER, _normalize_option, drawings)
        self.assertEqual(result["A"], r"\(\frac{a}{10}\sqrt{74}\)")

    def test_legacy_type3_fraction_in_table_cell(self):
        def char(value, y):
            return {"c": value, "bbox": (30, y, 35, y + 8), "origin": (30, y + 8)}
        raw = {"blocks": [{"type": 0, "lines": [{"spans": [
            {"font": "Unnamed-T3", "size": 10, "chars": [char("a", 42), char("b", 54)]}
        ]}]}]}
        class Page:
            def get_text(self, kind, clip):
                return raw
        value = reconstruct_cell_fraction(Page(), fitz.Rect(0, 0, 100, 100),
                                          [{"rect": fitz.Rect(29, 52, 36, 52.2)}])
        self.assertEqual(value, r"\(\frac{a}{b}\)")

    def test_legacy_unnamed_type3_font_is_recognized(self):
        segment = SimpleNamespace(rect=fitz.Rect(0, 0, 200, 200))
        options = [{"key": "A", "text": "raw"}, {"key": "B", "text": "No"}]
        page, drawings = self.geometry_page(type3_font="Unnamed-T3")
        result = reconstruct_option_fractions(page, segment, options, OPTION_MARKER, _normalize_option, drawings)
        self.assertEqual(result["A"], r"\(\frac{a}{10}\sqrt{74}\)")

    def test_mixed_font_unit_stays_outside_multicharacter_root(self):
        segment = SimpleNamespace(rect=fitz.Rect(0, 0, 200, 200))
        options = [{"key": "A", "text": "raw"}, {"key": "B", "text": "No"}]
        page, drawings = self.geometry_page(unit=True)
        result = reconstruct_option_fractions(page, segment, options, OPTION_MARKER, _normalize_option, drawings)
        self.assertEqual(result["A"], r"\(\frac{a}{10}\sqrt{74}N\)")

    def test_unmarked_root_is_not_guessed(self):
        segment = SimpleNamespace(rect=fitz.Rect(0, 0, 200, 200))
        options = [{"key": "A", "text": "raw"}, {"key": "B", "text": "No"}]
        page, drawings = self.geometry_page(radical_bar=False)
        result = reconstruct_option_fractions(page, segment, options, OPTION_MARKER, _normalize_option, drawings)
        self.assertEqual(result, {})

    def test_raised_option_glyph_is_removed_from_stem_only_when_source_matches(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "Q1 Which expression is correct?")
        page.insert_text((57, 119), "7", fontsize=6)  # source glyph sorts before the option label
        page.insert_text((60, 127), "(1) root")
        page.insert_text((160, 127), "(2) plain")
        with patch("apps.questions.pdf_layout.reconstruct_option_fractions", return_value={
            "A": r"\(\sqrt{7}\)", "B": "plain"
        }):
            questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        doc.close()
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].text, "Which expression is correct?")
        self.assertEqual(questions[0].options[0]["text"], r"\(\sqrt{7}\)")
        self.assertTrue(any("Mathematical formatting" in issue for issue in questions[0].issues))

    def test_raised_source_text_is_kept_when_answer_reconstruction_is_incomplete(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "Q1 Which expression is correct?")
        page.insert_text((57, 119), "7", fontsize=6)
        page.insert_text((60, 127), "(1) root")
        page.insert_text((160, 127), "(2) plain")
        with patch("apps.questions.pdf_layout.reconstruct_option_fractions", return_value={"A": r"\(\sqrt{7}\)"}):
            questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        doc.close()
        self.assertTrue(questions[0].text.endswith("7"))


class LocalLayoutTests(SimpleTestCase):
    def test_plain_numbered_two_columns_keep_figures_and_ignore_instructions(self):
        with fitz.open(stream=numeric_columns_pdf(), filetype="pdf") as doc:
            questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([q.number for q in questions], [1, 2, 3, 4])
        self.assertEqual([q.page_number for q in questions], [2, 2, 2, 2])
        self.assertEqual([o["text"] for o in questions[0].options],
                         ["First 1", "Second 1", "Third 1", "Fourth 1"])
        self.assertIsNotNone(questions[0].image)
        self.assertFalse(any(q.image for q in questions[1:]))

    def test_ocr_dpi_is_bounded_for_unusually_large_pages(self):
        doc = fitz.open(); normal = doc.new_page(width=600, height=800)
        self.assertEqual(PDFQuestionExtractor._safe_ocr_dpi(normal), 200)
        big = doc.new_page(width=2000, height=2000)
        dpi = PDFQuestionExtractor._safe_ocr_dpi(big)
        self.assertLess(dpi, 200)
        self.assertLessEqual(2000 * 2000 * (dpi / 72) ** 2, 16_000_000)
        oversized = doc.new_page(width=10000, height=10000)
        self.assertIsNone(PDFQuestionExtractor._safe_ocr_dpi(oversized))
        doc.close()

    def test_multiple_option_labels_on_one_line_get_distinct_original_crops(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 Which of the diagrams is correct?")
        for i, x in enumerate((50, 170, 290, 410)):
            page.insert_text((x, 125), f"({i + 1})")
            page.insert_image(fitz.Rect(x + 20, 140, x + 80, 185),
                              stream=image_bytes(("red", "green", "blue", "yellow")[i]))
        q = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        doc.close()
        self.assertIsNone(q.image)
        self.assertEqual([f["option_key"] for f in q.extra_figures], list("ABCD"))
        for figure, color in zip(q.extra_figures, [(255,0,0), (0,128,0), (0,0,255), (255,255,0)]):
            with Image.open(io.BytesIO(figure["data"])) as crop:
                self.assertEqual(crop.convert("RGB").getpixel((crop.width // 2, crop.height // 2)), color)

    def test_scanned_ink_candidate_preserves_original_figure(self):
        source = fitz.open(); original = source.new_page(width=600, height=800)
        original.insert_text((40, 80), "Q1 What is this diagram?")
        original.draw_circle((130, 150), 35)
        original.draw_line((95, 150), (165, 150)); original.draw_line((130, 115), (130, 185))
        original.insert_text((60, 215), "(1) Circle")
        original.insert_text((60, 235), "(2) Square")
        lines = [("".join(span["text"] for span in line["spans"]), fitz.Rect(line["bbox"]), line["spans"])
                 for block in original.get_text("dict")["blocks"] if block["type"] == 0 for line in block["lines"]]
        raster = original.get_pixmap(dpi=180).tobytes("png")
        scan_doc = fitz.open(); scan = scan_doc.new_page(width=600, height=800)
        scan.insert_image(scan.rect, stream=raster)
        candidates = scanned_graphic_candidates(scan, fitz.Rect(0, 60, 300, 255), lines)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].contains(fitz.Point(130, 150)))
        self.assertFalse(candidates[0].contains(fitz.Point(60, 215)))
        scan_doc.close(); source.close()

    @skipUnless(os.environ.get("LOCAL_OCR_TESSDATA"), "Optional local Tesseract language data not configured")
    def test_actual_tesseract_finds_a_referenced_scanned_diagram(self):
        source = fitz.open(); original = source.new_page(width=600, height=800)
        original.insert_text((40, 90), "Q1 Which diagram is shown?", fontsize=16)
        original.draw_circle((130, 175), 35)
        original.draw_line((95, 175), (165, 175)); original.draw_line((130, 140), (130, 210))
        original.insert_text((60, 260), "(1) Circle", fontsize=16)
        original.insert_text((60, 295), "(2) Square", fontsize=16)
        raster = original.get_pixmap(dpi=180).tobytes("png")
        scan_doc = fitz.open(); scan = scan_doc.new_page(width=600, height=800)
        scan.insert_image(scan.rect, stream=raster)
        textpage = scan.get_textpage_ocr(language="eng", dpi=200, full=True,
                                         tessdata=os.environ["LOCAL_OCR_TESSDATA"])
        q = DigitalQuestionLayout(scan_doc, lambda _: None, {0: (scan, textpage)}).extract()[0]
        self.assertIn("diagram", q.text.lower())
        self.assertIsNotNone(q.image, q.issues)
        self.assertTrue(any("scanned-page" in issue for issue in q.issues))
        self.assertEqual([o["text"] for o in q.options], ["Circle", "Square"])
        scan_doc.close(); source.close()

    @skipUnless(os.environ.get("LOCAL_OCR_TESSDATA"), "Optional local Tesseract language data not configured")
    def test_actual_local_hindi_ocr_retains_real_source_options_and_flags_review(self):
        source = fitz.open(); original = source.new_page(width=600, height=800)
        css = ("@font-face {font-family: noto; src: url(NotoSansDevanagari.ttf);} "
               "body {font-family:noto; font-size:19pt; line-height:1.5;} p {margin:10px 0;}")
        original.insert_htmlbox(fitz.Rect(40, 40, 560, 600),
            "<div>प्रश्न 1. भारत की राजधानी क्या है?</div><p>(1) दिल्ली</p><p>(2) मुंबई</p><p>(3) जयपुर</p><p>(4) पटना</p>",
            css=css, archive=fitz.Archive(str(Path(__file__).resolve().parents[2] / "static" / "fonts")))
        raster = original.get_pixmap(dpi=200).tobytes("png")
        scan_doc = fitz.open(); page = scan_doc.new_page(width=600, height=800)
        page.insert_image(page.rect, stream=raster)
        tp = page.get_textpage_ocr(language="eng+hin", dpi=200, full=True,
                                   tessdata=os.environ["LOCAL_OCR_TESSDATA"])
        q = DigitalQuestionLayout(scan_doc, lambda _: None, {0: (page, tp)}).extract()[0]
        self.assertEqual(q.number, 1)
        self.assertIn("भारत की राजधानी क्या है", q.text)
        self.assertFalse(q.text.startswith("प्रश्न"))
        self.assertEqual([o["key"] for o in q.options], list("ABCD"))
        self.assertIn("मुंबई", q.options[1]["text"])
        self.assertTrue(any("OCR" in issue for issue in q.issues))
        scan_doc.close(); source.close()

    def test_preocr_full_page_raster_uses_its_text_layer_and_crops_diagram(self):
        # Simulate a scanner's full-page image with an invisible OCR text layer.
        original = fitz.open(); page = original.new_page(width=600, height=800)
        text = [(40, 90, "Q1 Which diagram is shown?"), (60, 260, "(1) Circle"), (60, 295, "(2) Square")]
        for x, y, value in text:
            page.insert_text((x, y), value, fontsize=16)
        page.draw_circle((130, 175), 35)
        page.draw_line((95, 175), (165, 175)); page.draw_line((130, 140), (130, 210))
        raster = page.get_pixmap(dpi=180).tobytes("png"); original.close()
        doc = fitz.open(); scan = doc.new_page(width=600, height=800)
        scan.insert_image(scan.rect, stream=raster)
        for x, y, value in text:
            scan.insert_text((x, y), value, fontsize=16, render_mode=3)
        self.assertEqual(PDFQuestionExtractor._scanned_page_indices(doc), [])  # existing text is usable
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        doc.close()
        self.assertEqual(len(questions), 1)
        self.assertEqual([option["text"] for option in questions[0].options], ["Circle", "Square"])
        self.assertIsNotNone(questions[0].image)
        self.assertTrue(any("Full-page raster with text layer" in issue for issue in questions[0].issues))
        self.assertTrue(any("scanned-page diagram" in issue for issue in questions[0].issues))
        box = questions[0].primary_image_region["box"]
        self.assertLess((box[2] - box[0]) * (box[3] - box[1]), .1)  # not the full page

    def test_raster_page_vector_overlay_is_not_duplicated_as_scan_ink(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_image(page.rect, stream=image_bytes("white"), keep_proportion=False)
        page.insert_text((40, 90), "Q1 Which diagram is shown?", fontsize=16)
        page.draw_circle((130, 175), 35)
        page.draw_line((95, 175), (165, 175)); page.draw_line((130, 140), (130, 210))
        page.insert_text((60, 260), "(1) Circle", fontsize=16)
        page.insert_text((60, 295), "(2) Square", fontsize=16)
        q = DigitalQuestionLayout(doc, lambda _: None).extract()[0]; doc.close()
        self.assertIsNotNone(q.image)
        self.assertEqual({f["source"] for f in q.extra_figures}, {"stem-source", "option-source"})
        self.assertFalse(any(f["source"] == "scan" for f in q.extra_figures))
        self.assertTrue(any("vector diagram" in issue for issue in q.issues))

    def test_vector_figure_is_cropped_from_original_and_flagged(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 What is shown in this diagram?")
        page.draw_circle((110, 150), 30)
        page.draw_line((80, 150), (140, 150)); page.draw_line((110, 120), (110, 180))
        page.insert_text((60, 210), "(1) Circle")
        page.insert_text((60, 230), "(2) Square")
        q = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        doc.close()
        self.assertIsNotNone(q.image)
        self.assertEqual(q.image[0], 1)
        self.assertTrue(any("vector diagram" in issue for issue in q.issues))
        self.assertEqual(q.primary_image_region["page"], 1)
        self.assertEqual([option["text"] for option in q.options], ["Circle", "Square"])

    def test_legacy_unnamed_type3_sets_complex_math_review_flag(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 Which answer?")
        page.insert_text((60, 120), "(1) One"); page.insert_text((60, 140), "(2) Two")
        layout = DigitalQuestionLayout(doc, lambda _: None)
        for _, _, spans in layout.lines[0]:
            for span in spans:
                if "One" in span["text"]:
                    span["font"] = "Unnamed-T3"
        questions = layout.extract(); doc.close()
        self.assertTrue(any("Mathematical formatting" in issue for issue in questions[0].issues))

    def test_native_math_failure_preserves_source_options(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 Which expression is correct?")
        page.insert_text((60, 120), "(1) First")
        page.insert_text((100, 115), "2", fontsize=6)  # triggers the complex-math path
        page.insert_text((60, 140), "(2) Second")
        logs = []
        with patch("apps.questions.pdf_layout.reconstruct_option_fractions", side_effect=Exception("bad glyphs")):
            questions = DigitalQuestionLayout(doc, logs.append).extract()
        doc.close()
        self.assertEqual(len(questions), 1)
        self.assertEqual(len(questions[0].options), 2)
        self.assertTrue(any("math reconstruction failed" in line for line in logs))
        self.assertTrue(any("Mathematical formatting" in issue for issue in questions[0].issues))

    def test_native_graphic_or_table_failure_preserves_other_question_content(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 Which value appears in the table?")
        page.insert_text((60, 120), "(1) One"); page.insert_text((60, 140), "(2) Two")
        logs = []
        with patch.object(fitz.Page, "find_tables", side_effect=Exception("bad table geometry")), \
             patch.object(fitz.Page, "cluster_drawings", side_effect=Exception("bad graphics")):
            questions = DigitalQuestionLayout(doc, logs.append).extract()
        doc.close()
        self.assertEqual(len(questions), 1)
        self.assertEqual([option["text"] for option in questions[0].options], ["One", "Two"])
        self.assertTrue(any("table detection failed" in line for line in logs))
        self.assertTrue(any("vector diagram detection failed" in line for line in logs))
        self.assertTrue(any("Table or matching" in issue for issue in questions[0].issues))

    def test_unreferenced_vector_candidate_is_pooled_not_silently_assigned(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 Which choice applies?")
        page.draw_circle((110, 150), 30)
        page.draw_line((80, 150), (140, 150)); page.draw_line((110, 120), (110, 180))
        page.insert_text((60, 210), "(1) Circle")
        page.insert_text((60, 230), "(2) Square")
        q = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        doc.close()
        self.assertIsNone(q.image)
        self.assertEqual(len(q.extra_figures), 1)
        self.assertIsNone(q.extra_figures[0]["option_key"])
        self.assertEqual(q.extra_figures[0]["source"], "vector")

    def test_unreferenced_ruled_table_is_structured_but_answer_grid_is_not_consumed(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 Which value is highest?")
        page.draw_rect(fitz.Rect(60, 100, 300, 180))
        page.draw_line((180, 100), (180, 180))
        page.draw_line((60, 140), (300, 140))
        for x, y, value in [(70, 125, "Item"), (190, 125, "Value"), (70, 165, "X"), (190, 165, "10")]:
            page.insert_text((x, y), value)
        page.insert_text((60, 205), "(1) 10")
        page.insert_text((60, 225), "(2) 20")
        question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        doc.close()
        self.assertIn("[[table:1]]", question.text)
        self.assertEqual(question.tables[0]["columns"], 2)
        self.assertEqual([[cell["text"] for cell in row] for row in question.tables[0]["rows"]],
                         [["Item", "Value"], ["X", "10"]])
        self.assertEqual([option["text"] for option in question.options], ["10", "20"])
        self.assertTrue(any("table structure" in issue for issue in question.issues))

    def test_grid_in_answer_area_does_not_become_question_table(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 Pick the correct entry.")
        page.draw_rect(fitz.Rect(60, 100, 300, 180))
        page.draw_line((180, 100), (180, 180)); page.draw_line((60, 140), (300, 140))
        for x, y, value in [(70, 125, "(1) Red"), (190, 125, "(2) Blue"),
                            (70, 165, "(3) Green"), (190, 165, "(4) White")]:
            page.insert_text((x, y), value)
        question = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        doc.close()
        self.assertFalse(question.tables)
        self.assertEqual([option["key"] for option in question.options], list("ABCD"))

    def test_numbered_questions_and_numeric_choices_at_the_same_margin(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        for number, y in [(1, 80), (2, 260)]:
            page.insert_text((40, y), f"{number}. Which answer belongs to question {number}?")
            for option in range(1, 5):
                page.insert_text((40, y + option * 20), f"{option}) Answer {option} for {number}")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        doc.close()
        self.assertEqual([q.number for q in questions], [1, 2])
        self.assertEqual([o["text"] for o in questions[1].options], [f"Answer {i} for 2" for i in range(1, 5)])

    def test_plain_numbered_question_continues_to_next_page(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((40, 700), "1. The choices are on the next page.")
        page = doc.new_page(width=600, height=800)
        page.insert_text((60, 80), "(A) One"); page.insert_text((60, 100), "(B) Two")
        page.insert_text((40, 160), "2. Another question?")
        page.insert_text((60, 180), "(A) Yes"); page.insert_text((60, 200), "(B) No")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        doc.close()
        self.assertEqual([q.number for q in questions], [1, 2])
        self.assertEqual([o["text"] for o in questions[0].options], ["One", "Two"])
        self.assertEqual([r["page"] for r in questions[0].source_regions], [1, 2])

    def test_single_question_after_a_numbered_cover_is_not_absorbed_by_it(self):
        doc = fitz.open(); cover = doc.new_page(width=600, height=800)
        cover.insert_text((5, 80), "1. Read these instructions carefully.")
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "1. Which color is correct?")
        page.insert_text((60, 100), "A) Red"); page.insert_text((60, 120), "B) Blue")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract(); doc.close()
        self.assertEqual([(q.number, q.page_number) for q in questions], [(1, 2)])
        self.assertEqual(questions[0].text, "Which color is correct?")

    def test_numbered_questions_can_change_margins_between_pages(self):
        doc = fitz.open()
        for number, x in ((1, 30), (2, 60), (3, 60)):
            page = doc.new_page(width=600, height=800)
            page.insert_text((x, 80), f"{number}. Which choice for item {number}?")
            page.insert_text((x + 20, 100), "A) Yes"); page.insert_text((x + 20, 120), "B) No")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract(); doc.close()
        self.assertEqual([q.number for q in questions], [1, 2, 3])
        self.assertEqual([q.page_number for q in questions], [1, 2, 3])

    def test_cover_margin_does_not_override_numbered_questions(self):
        doc = fitz.open(); cover = doc.new_page(width=600, height=800)
        cover.insert_text((5, 80), "1. Read instructions carefully.")
        page = doc.new_page(width=600, height=800)
        for n, y in [(1, 80), (2, 200)]:
            page.insert_text((40, y), f"{n}. What is answer {n}?")
            page.insert_text((60, y + 20), "A) Yes"); page.insert_text((60, y + 40), "B) No")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        doc.close()
        self.assertEqual([q.number for q in questions], [1, 2])
        self.assertEqual([q.page_number for q in questions], [2, 2])

    def test_question_prefix_with_closing_parenthesis(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "Q1) What is the answer?")
        page.insert_text((60, 100), "(A) Yes"); page.insert_text((60, 120), "(B) No")
        q = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        doc.close()
        self.assertEqual(q.number, 1); self.assertEqual(q.text, "What is the answer?")

    def test_prefixed_questions_do_not_split_at_aligned_numeric_options(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "Q1 Which answer is correct?")
        for option in range(1, 5):
            page.insert_text((40, 80 + 20 * option), f"{option}) Answer {option}")
        page.insert_text((40, 240), "Q2 Another question?")
        for option in range(1, 5):
            page.insert_text((40, 240 + 20 * option), f"{option}) Choice {option}")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract(); doc.close()
        self.assertEqual([q.number for q in questions], [1, 2])
        self.assertEqual([o["text"] for o in questions[0].options], [f"Answer {i}" for i in range(1, 5)])

    def test_incomplete_numbered_item_between_confirmed_questions_is_kept_flagged(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "1. First question?")
        page.insert_text((60, 100), "A) Yes"); page.insert_text((60, 120), "B) No")
        page.insert_text((40, 180), "2. This item has no extractable choices?")
        page.insert_text((40, 260), "3. Third question?")
        page.insert_text((60, 280), "A) Yes"); page.insert_text((60, 300), "B) No")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract(); doc.close()
        self.assertEqual([q.number for q in questions], [1, 2, 3])
        self.assertEqual(questions[1].options, [])
        self.assertTrue(any("inferred" in issue for issue in questions[1].issues))

    def test_numbered_question_label_is_not_an_option_marker(self):
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "1. Which figure is correct?")
        page.insert_image(fitz.Rect(60, 105, 140, 165), stream=image_bytes("red"))
        page.insert_text((60, 190), "A) Yes")
        page.insert_text((60, 210), "B) No")
        q = DigitalQuestionLayout(doc, lambda _: None).extract()[0]
        doc.close()
        self.assertIsNotNone(q.image)
        self.assertEqual(q.extra_figures, [])


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MixedExtractionTests(TestCase):
    def setUp(self):
        import tempfile
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        override = override_settings(MEDIA_ROOT=root.name); override.enable(); self.addCleanup(override.disable)
        teacher = User.objects.create_user(username="mixed-teacher", password="test", role=User.Roles.TEACHER)
        category = Category.objects.create(name="Mixed", slug="mixed")
        self.paper = QuestionPaperUpload.objects.create(
            title="Mixed local", category=category, uploaded_by=teacher,
            pdf_file=SimpleUploadedFile("mixed.pdf", mixed_pdf(), content_type="application/pdf"))

    def test_missing_ocr_does_not_downgrade_digital_page_or_drop_its_figure(self):
        with patch("apps.questions.extractor.fitz.get_tessdata", side_effect=RuntimeError("missing")), \
             patch.dict(os.environ, {"TESSDATA_PREFIX": ""}):
            count = PDFQuestionExtractor(self.paper).extract()
        self.paper.refresh_from_db()
        self.assertEqual(count, 1)
        question = self.paper.extracted_questions.get()
        self.assertEqual(question.question_number, 1)
        self.assertTrue(question.image)
        self.assertIn("Scanned content may be missing", self.paper.extraction_log)

    def test_bad_language_data_does_not_discard_digital_page(self):
        with patch.dict(os.environ, {"TESSDATA_PREFIX": "/nonexistent-tessdata-for-test"}):
            count = PDFQuestionExtractor(self.paper).extract()
        self.paper.refresh_from_db()
        self.assertEqual(count, 1)
        self.assertTrue(self.paper.extracted_questions.get().image)
        self.assertIn("English OCR data not found", self.paper.extraction_log)
        self.assertIn("may be missing", self.paper.extraction_log)

    def test_native_ocr_exception_does_not_discard_digital_pages(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "eng.traineddata").touch()
            with patch.dict(os.environ, {"TESSDATA_PREFIX": directory}), \
                 patch.object(fitz.Page, "get_textpage_ocr", side_effect=Exception("synthetic native OCR error")):
                count = PDFQuestionExtractor(self.paper).extract()
        self.paper.refresh_from_db()
        self.assertEqual(count, 1)
        self.assertTrue(self.paper.extracted_questions.get().image)
        self.assertIn("local OCR with eng failed", self.paper.extraction_log)
        self.assertIn("may be missing", self.paper.extraction_log)

    @skipUnless(os.environ.get("LOCAL_OCR_TESSDATA"), "Optional local Tesseract language data not configured")
    def test_actual_local_tesseract_mixed_pdf_preserves_digital_and_scanned_questions(self):
        with patch.dict(os.environ, {"TESSDATA_PREFIX": os.environ["LOCAL_OCR_TESSDATA"]}):
            count = PDFQuestionExtractor(self.paper).extract()
        self.paper.refresh_from_db()
        self.assertEqual(count, 2, self.paper.extraction_log)
        first, second = list(self.paper.extracted_questions.order_by("question_number"))
        self.assertTrue(first.image)
        self.assertEqual([first.page_number, second.page_number], [1, 2])
        self.assertIn("correct answer", second.question_text.lower())
        self.assertEqual([option["text"] for option in second.options], ["Yes", "No"])
        self.assertTrue(second.is_flagged)
        self.assertIn("OCR", " ".join(second.extraction_issues))
        self.assertTrue(self.paper.ocr_used)
        self.assertFalse(second.image)  # The page-sized scan is not a diagram.


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class InstitutionalGraphicWorkflowTests(TestCase):
    def setUp(self):
        import tempfile
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        override = override_settings(MEDIA_ROOT=os.path.join(root.name, "public"),
                                     STATIC_ROOT=os.path.join(root.name, "static"))
        override.enable(); self.addCleanup(override.disable)
        self.teacher = User.objects.create_user(username="graphic-teacher", email="graphic-teacher@example.test", password="x", role=User.Roles.TEACHER, is_staff=True)
        self.other = User.objects.create_user(username="other-teacher", email="other-teacher@example.test", password="x", role=User.Roles.TEACHER, is_staff=True)
        self.category = Category.objects.create(name="Teacher images", slug="teacher-images")

    def test_teacher_reviews_option_images_before_bank_approval(self):
        from django.urls import reverse
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 Which of the diagrams is correct?")
        for index, x in enumerate((50, 170, 290, 410)):
            page.insert_text((x, 125), f"({index + 1})")
            page.insert_image(fitz.Rect(x + 20, 140, x + 80, 185),
                              stream=image_bytes(("red", "green", "blue", "yellow")[index]))
        data = doc.write(); doc.close()
        paper = QuestionPaperUpload.objects.create(
            title="Teacher figures", uploaded_by=self.teacher, category=self.category, subject="Science",
            pdf_file=SimpleUploadedFile("teacher.pdf", data, content_type="application/pdf"))
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 1)
        item = paper.extracted_questions.get()
        figures = list(item.figures.order_by("option_key"))
        self.assertEqual([f.option_key for f in figures], list("ABCD"))
        self.assertTrue(all(f.image and not f.private_image for f in figures))
        self.assertTrue(item.is_flagged)
        url = reverse("questions:figure_file", args=[figures[0].pk])
        self.client.force_login(self.other)
        self.assertIn(self.client.get(url).status_code, (403, 404))
        self.client.force_login(self.teacher)
        response = self.client.get(url); self.assertEqual(response.status_code, 200); self.assertTrue(response_body(response))
        self.client.post(reverse("questions:extracted_approve", args=[item.pk]))
        item.refresh_from_db(); self.assertIsNone(item.approved_question)  # still flagged
        self.client.post(reverse("questions:extracted_toggle_flag", args=[item.pk]))
        self.client.post(reverse("questions:extracted_approve", args=[item.pk]))
        item.refresh_from_db(); self.assertIsNotNone(item.approved_question)
        bank = item.approved_question
        self.assertFalse(bank.key_verified)  # no answer key was inferred
        self.assertEqual([f.option.option_key for f in bank.figures.select_related("option").order_by("option__option_key")], list("ABCD"))
        self.assertTrue(all(f.image and f.bounding_box and f.source == "embedded" for f in bank.figures.all()))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PrivateGraphicWorkflowTests(TestCase):
    def setUp(self):
        import tempfile
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        override = override_settings(MEDIA_ROOT=os.path.join(root.name, "public"),
                                     STATIC_ROOT=os.path.join(root.name, "static"),
                                     PRIVATE_PRACTICE_ROOT=os.path.join(root.name, "private"))
        override.enable(); self.addCleanup(override.disable)
        self.owner = User.objects.create_user(username="image-owner", email="image-owner@example.test", password="x", role=User.Roles.STUDENT)
        self.other = User.objects.create_user(username="image-other", email="image-other@example.test", password="x", role=User.Roles.STUDENT)
        self.category = Category.objects.create(name="Image choices", slug="image-choices")

    def paper(self, doc):
        data = doc.write(); doc.close()
        return QuestionPaperUpload.objects.create(
            title="Private graphics", uploaded_by=self.owner, category=self.category, subject="Science",
            is_student_private=True, private_pdf_file=SimpleUploadedFile("private.pdf", data, content_type="application/pdf"))

    def test_option_images_remain_private_and_survive_practice_creation(self):
        from django.urls import reverse
        from .practice_forms import practice_question_error
        from apps.exams.models import ExamAttempt
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 Which of the diagrams is correct?")
        for i, x in enumerate((50, 170, 290, 410)):
            page.insert_text((x, 125), f"({i + 1})")
            page.insert_image(fitz.Rect(x + 20, 140, x + 80, 185),
                              stream=image_bytes(("red", "green", "blue", "yellow")[i]))
        paper = self.paper(doc)
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 1)
        item = paper.extracted_questions.get()
        figures = list(item.figures.order_by("option_key"))
        self.assertEqual([f.option_key for f in figures], list("ABCD"))
        self.assertTrue(all(f.private_image and not f.image and f.source == "embedded" for f in figures))
        self.assertTrue(item.is_flagged)  # assignment must still be checked
        self.assertEqual(practice_question_error(item), "")  # image-only choices are valid
        self.client.force_login(self.owner)
        response = self.client.get(reverse("questions:practice_figure_file", args=[figures[0].pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertTrue(response_body(response))
        self.client.force_login(self.other)
        self.assertIn(self.client.get(reverse("questions:practice_figure_file", args=[figures[0].pk])).status_code, (403, 404))
        self.client.force_login(self.owner)
        response = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {
            "duration_minutes": "20", "acknowledge_review": "on", "passing_percentage": "0"})
        self.assertEqual(response.status_code, 302, getattr(response, "context", ""))
        paper.refresh_from_db(); item.refresh_from_db()
        self.assertFalse(item.approved_question.key_verified)
        self.assertEqual(item.approved_question.figures.count(), 4)
        self.assertEqual([f.option.option_key for f in item.approved_question.figures.select_related("option").order_by("option__option_key")], list("ABCD"))
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[paper.practice_exam.slug])).status_code, 302)
        attempt = ExamAttempt.objects.get(exam=paper.practice_exam)
        session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(session.status_code, 200)
        payload = session.context["questions_data"][0]["options"]
        self.assertEqual([option["key"] for option in payload], list("ABCD"))
        self.assertTrue(all(option["image_url"] for option in payload))
        shown_image = self.client.get(payload[0]["image_url"])
        self.assertEqual(shown_image.status_code, 200); self.assertTrue(response_body(shown_image))

    @skipUnless(os.environ.get("LOCAL_OCR_TESSDATA"), "Optional local Tesseract language data not configured")
    def test_actual_scanned_diagram_is_saved_privately_with_source_region(self):
        from django.urls import reverse
        original = fitz.open(); page = original.new_page(width=600, height=800)
        page.insert_text((40, 90), "Q1 Which diagram is shown?", fontsize=16)
        page.draw_circle((130, 175), 35)
        page.draw_line((95, 175), (165, 175)); page.draw_line((130, 140), (130, 210))
        page.insert_text((60, 260), "(1) Circle", fontsize=16)
        page.insert_text((60, 295), "(2) Square", fontsize=16)
        raster = page.get_pixmap(dpi=180).tobytes("png"); original.close()
        doc = fitz.open(); scan = doc.new_page(width=600, height=800)
        scan.insert_image(scan.rect, stream=raster)
        paper = self.paper(doc)
        with patch.dict(os.environ, {"TESSDATA_PREFIX": os.environ["LOCAL_OCR_TESSDATA"]}):
            self.assertEqual(PDFQuestionExtractor(paper).extract(), 1)
        question = paper.extracted_questions.get(); paper.refresh_from_db()
        self.assertTrue(paper.ocr_used)
        self.assertTrue(question.private_image and not question.image)
        self.assertEqual(question.primary_image_region["page"], 1)
        self.assertTrue(question.is_flagged)
        self.client.force_login(self.other)
        url = reverse("questions:practice_extracted_image", args=[question.pk])
        self.assertIn(self.client.get(url).status_code, (403, 404))
        self.client.force_login(self.owner)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200); self.assertTrue(response_body(response))

    def test_unreferenced_vector_candidate_is_private_in_review_pool(self):
        from django.urls import reverse
        doc = fitz.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 80), "Q1 Which choice applies?")
        page.draw_circle((110, 150), 30)
        page.draw_line((80, 150), (140, 150)); page.draw_line((110, 120), (110, 180))
        page.insert_text((60, 210), "(1) Circle"); page.insert_text((60, 230), "(2) Square")
        paper = self.paper(doc)
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 1)
        figure = paper.figures.get()
        self.assertIsNone(figure.question)
        self.assertEqual(figure.source, "vector")
        self.assertTrue(figure.private_image and not figure.image)
        self.client.force_login(self.other)
        self.assertIn(self.client.get(reverse("questions:practice_figure_file", args=[figure.pk])).status_code, (403, 404))
        self.client.force_login(self.owner)
        response = self.client.get(reverse("questions:practice_figure_file", args=[figure.pk]))
        self.assertEqual(response.status_code, 200); self.assertTrue(response_body(response))
