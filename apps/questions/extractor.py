import io
import os
import re
import shutil
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
import pymupdf as fitz
from PIL import Image

try:
    import pytesseract
except ImportError:
    pytesseract = None

from .models import ExtractedQuestion, QuestionPaperUpload
from .pdf_layout import DigitalQuestionLayout, FIGURE_REFERENCE


class ReviewWorkPreserved(Exception):
    """A repeat extraction was refused to protect staged teacher edits."""


class PDFQuestionExtractor:
    """Extract questions for teacher review, without guessing answer keys.

    Digital papers with explicit question labels use page geometry to read
    columns in order and associate embedded figures with their question. A
    simpler text/Tesseract fallback remains available for other layouts. Both
    approaches can misread formulae, figures and OCR; review is essential.
    """

    # Question start patterns: Q.1, Q1., Question 1:, 1., 1), प्रश्न 1:, प्र. 1.
    QUESTION_PATTERN = re.compile(
        r"^(?:(?:Q(?:uestion)?\.?\s*(\d+)[:.]?)|(?:(\d+)[\.\)](?!\d))|(?:प्रश्न\s*(\d+)[:.]?)|(?:प्र\.\s*(\d+)[:.]?))\s*(.*)",
        re.IGNORECASE,
    )
    OPTION_INLINE_PATTERN = re.compile(
        r"(?:\(([A-Da-d1-4कखगघ])\)|(?:^|\s+)([A-Da-d1-4])\.)\s*(.*?)(?=(?:\([A-Da-d1-4कखगघ]\)|(?:^|\s+)[A-Da-d1-4]\.|$))",
        re.DOTALL,
    )
    OPTION_LINE_PATTERN = re.compile(r"^\s*(?:\(([A-Da-d1-4कखगघ])\)|([A-Da-d1-4])\.)\s*(.*)")

    def __init__(self, paper_upload: QuestionPaperUpload):
        self.paper = paper_upload
        self.logs = []
        self._private_files_written = []
        self.ocr_available = self._check_ocr_availability()

    def _log(self, message: str):
        self.logs.append(message)

    def _check_ocr_availability(self) -> bool:
        tess_cmd = getattr(settings, "TESSERACT_CMD", "")
        if tess_cmd and os.path.exists(tess_cmd):
            if pytesseract:
                pytesseract.pytesseract.tesseract_cmd = tess_cmd
            return True
        return bool(shutil.which("tesseract"))

    @staticmethod
    def _has_scanned_page(doc):
        """Treat an almost textless page with a large raster as a scan.

        Empty end pages are not sufficient reason to switch the entire paper
        away from the digital layout parser.
        """
        for page in doc:
            if len(page.get_text("text").strip()) >= 40:
                continue
            page_area = page.rect.get_area()
            for image in page.get_image_info():
                if fitz.Rect(image["bbox"]).get_area() > page_area * 0.35:
                    return True
        return False

    def extract(self) -> int:
        """Process the PDF into staging items and return the number saved."""
        previous_status = self.paper.status
        self.paper.status = QuestionPaperUpload.ProcessingStatus.PROCESSING
        self.paper.save(update_fields=["status"])
        self.paper.ocr_used = False
        self._log(f"Starting extraction for paper: '{self.paper.title}'")
        saved_count = 0
        try:
            source = self.paper.private_pdf_file if self.paper.is_student_private else self.paper.pdf_file
            source.open("rb")
            try:
                pdf_bytes = source.read(25 * 1024 * 1024 + 1) if self.paper.is_student_private else source.read()
            finally:
                source.close()
            with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
                if self.paper.is_student_private and (len(pdf_bytes) > 25 * 1024 * 1024 or len(doc) > settings.PRACTICE_MAX_PDF_PAGES):
                    raise ValueError("The private PDF exceeds the configured size or page limit.")
                self.paper.total_pages = len(doc)
                self._log(f"PDF verified: {len(doc)} total pages.")

                items = None
                if not self._has_scanned_page(doc):
                    items = DigitalQuestionLayout(doc, self._log).extract()
                else:
                    self._log(
                        "Warning: Scanned pages detected: using the basic OCR/text fallback. "
                        "Column order and figure matching must be checked manually."
                    )
                if items is None:
                    self._log(
                        "Warning: Using basic text/OCR extraction; diagrams cannot be reliably assigned automatically in this mode."
                    )
                    items = self._extract_text_fallback(doc)

                if not items:
                    # In particular, do not replace earlier review work with
                    # an empty result after an OCR or parsing failure.
                    self.paper.status = QuestionPaperUpload.ProcessingStatus.FAILED
                    self._log("No questions could be extracted. Check the PDF and OCR diagnostics.")
                else:
                    # Never replace teacher review work on a repeat extraction.
                    # Upload a new paper to start a fresh extraction instead.
                    with transaction.atomic():
                        locked = QuestionPaperUpload.objects.select_for_update().get(pk=self.paper.pk)
                        if locked.extracted_questions.exists() or locked.figures.exists():
                            raise ReviewWorkPreserved("Existing review work was preserved; upload a new copy to re-extract.")
                        for item in items:
                            self._save_item(item)
                            saved_count += 1
                    self.paper.questions_found = saved_count
                    self.paper.status = QuestionPaperUpload.ProcessingStatus.EXTRACTED
                    self._log(f"Extraction completed: {saved_count} questions saved for review.")
                    self._log("Check question text, formulas, options and diagrams against the original PDF before approval.")
        except ReviewWorkPreserved as exc:
            saved_count = 0
            self._log(f"Warning: {exc}")
            self.paper.status = previous_status
        except Exception as exc:
            saved_count = 0
            if self.paper.is_student_private:
                from .private_storage import private_practice_storage
                for name in self._private_files_written:
                    private_practice_storage.delete(name)
            self._log(f"Extraction error: {str(exc)}")
            self.paper.status = QuestionPaperUpload.ProcessingStatus.FAILED

        self.paper.extraction_log = "\n".join(self.logs)
        self.paper.save()
        return saved_count

    def _save_item(self, item):
        text = item.text.strip()
        options = item.options
        issues = list(item.issues)
        if not text:
            issues.insert(0, "Missing question statement.")
        if len(options) < 2:
            issues.insert(0, f"Only {len(options)} option(s) detected. Minimum 2 required for MCQ.")
        elif any(not option.get("text", "").strip() for option in options):
            issues.insert(0, "One or more options are empty; check the source.")
        if len({option.get("key") for option in options}) != len(options):
            issues.append("Duplicate option labels detected; compare with the source.")

        extracted = ExtractedQuestion(
            paper=self.paper,
            question_number=item.number,
            question_text=text,
            options=options,
            page_number=item.page_number,
            source_pages=sorted({item.page_number}
                | ({item.image[0]} if item.image else set())
                | {segment.page_index + 1 for segment in item.segments if segment.text.strip() or segment.images}),
            extraction_issues=issues,
            status=ExtractedQuestion.Status.EXTRACTED,
            is_flagged=bool(issues),
            flag_reason=" ".join(dict.fromkeys(issues))[:255],
            positive_marks=Decimal("4.00"),
            negative_marks=Decimal("1.00"),
        )
        if item.image:
            page_number, data = item.image
            filename = f"paper_{self.paper.pk}_q{item.number}_p{page_number}.png"
            target = extracted.private_image if self.paper.is_student_private else extracted.image
            target.save(filename, ContentFile(data), save=False)
            if self.paper.is_student_private:
                self._private_files_written.append(target.name)
        extracted.save()

    def _extract_text_fallback(self, doc):
        """Basic parser for unlabelled/numerical and OCR-only papers.

        It deliberately does not attach the largest image on a page: that
        frequently turns a logo or an unrelated diagram into a question image.
        """
        from .pdf_layout import LayoutQuestion

        items = []
        current = None
        for page_index, page in enumerate(doc):
            page_num = page_index + 1
            page_text = page.get_text("text")
            if len(page_text.strip()) < 40:
                if self.ocr_available and pytesseract:
                    self._log(f"Page {page_num}: sparse text; executing Tesseract OCR.")
                    pix = page.get_pixmap(dpi=200)
                    image = Image.open(io.BytesIO(pix.tobytes("png")))
                    try:
                        ocr_text = pytesseract.image_to_string(image, lang="eng+hin")
                    except Exception:
                        try:
                            ocr_text = pytesseract.image_to_string(image, lang="eng")
                            self._log(f"Warning: Page {page_num}: Hindi OCR unavailable; using English only.")
                        except Exception as exc:
                            self._log(f"Warning: Page {page_num}: OCR extraction error: {exc}")
                            ocr_text = ""
                    if len(ocr_text.strip()) > len(page_text.strip()):
                        page_text = ocr_text
                        self.paper.ocr_used = True
                else:
                    self._log(
                        f"Warning: Page {page_num}: sparse text, but Tesseract OCR is not installed or configured."
                    )
            for line in (line.strip() for line in page_text.splitlines() if line.strip()):
                match = self.QUESTION_PATTERN.match(line)
                if match:
                    number = int(match.group(1) or match.group(2) or match.group(3) or match.group(4))
                    # OCR and PDF formula fragments often look like '0. 01287'
                    # or '2. 574'. Do not silently create Q0 or a decimal
                    # fragment as a new question in this low-confidence mode.
                    decimal_fragment = bool(match.group(2) and re.match(r"^\d+\.\s+\d", line))
                    if number <= 0 or decimal_fragment:
                        match = None
                if match:
                    if current:
                        items.append(current)
                    current = LayoutQuestion(number, page_num, match.group(0), text=match.group(5).strip())
                    continue
                if not current:
                    continue
                # Parse multiple inline options before a single option line.
                inline = self.OPTION_INLINE_PATTERN.findall(line)
                if len(inline) >= 2 and self.OPTION_LINE_PATTERN.match(line):
                    # Do not split references such as 'Both (2) and (3)'.
                    first = self.OPTION_LINE_PATTERN.match(line)
                    first_key = self._normalize_key((first.group(1) or first.group(2)).upper())
                    keys = [self._normalize_key((key or key2).upper()) for key, key2, _ in inline]
                    if keys == [chr(ord(first_key) + i) for i in range(len(keys))]:
                        current.options.extend({"key": key, "text": body.strip()} for key, (_, _, body) in zip(keys, inline))
                        continue
                option = self.OPTION_LINE_PATTERN.match(line)
                if option:
                    key = (option.group(1) or option.group(2)).upper()
                    current.options.append({"key": self._normalize_key(key), "text": option.group(3).strip()})
                elif current.options:
                    current.options[-1]["text"] += " " + line
                else:
                    current.text += " " + line
        if current:
            items.append(current)
        for item in items:
            item.issues.insert(0, "Basic extraction needs manual review of numbering, text and diagrams.")
            if FIGURE_REFERENCE.search(item.text):
                item.issues.append("A figure is referenced but no question diagram was detected.")
        return items

    def _normalize_key(self, raw_key: str) -> str:
        return {"1": "A", "2": "B", "3": "C", "4": "D", "क": "A", "ख": "B", "ग": "C", "घ": "D"}.get(
            raw_key, raw_key.upper()
        )
