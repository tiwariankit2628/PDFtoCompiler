import io
import math
import os
import re
import shutil
import subprocess
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
from .pdf_layout import DigitalQuestionLayout, FIGURE_REFERENCE, _normalize_option


class ReviewWorkPreserved(Exception):
    """A repeat extraction was refused to protect staged teacher edits."""


class PDFQuestionExtractor:
    """Extract questions for teacher review, without guessing answer keys.

    Page geometry is preserved for digital pages and local Tesseract OCR pages.
    A basic fallback remains for layouts with no reliable anchors. All paths can
    misread formulae, figures and OCR; human review is essential.
    """

    # Question start patterns: Q.1, Q1., Question 1:, 1., 1), प्रश्न 1:, प्र. 1.
    QUESTION_PATTERN = re.compile(
        r"^(?:(?:Q(?:uestion)?\.?\s*(\d+)[:.)]?)|(?:(\d+)[\.\)](?!\d))|(?:प्रश्न\s*(\d+)[:.]?)|(?:प्र\.\s*(\d+)[:.]?))\s*(.*)",
        re.IGNORECASE,
    )
    OPTION_INLINE_PATTERN = re.compile(
        r"(?:\(([A-Ea-e1-5कखगघङ१२३४५])\)|(?:^|\s+)([A-Ea-e1-5१२३४५])[.)])\s*(.*?)(?=(?:\([A-Ea-e1-5कखगघङ१२३४५]\)|(?:^|\s+)[A-Ea-e1-5१२३४५][.)]|$))",
        re.DOTALL,
    )
    OPTION_LINE_PATTERN = re.compile(r"^\s*(?:\(([A-Ea-e1-5कखगघङ१२३४५])\)|([A-Ea-e1-5१२३४५])[.)])\s*(.*)")

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
    def _scanned_page_indices(doc):
        """Find text-poor pages covered substantially by one or more images."""
        scanned = []
        for index, page in enumerate(doc):
            if len(page.get_text("text").strip()) >= 40:
                continue
            page_area = page.rect.get_area()
            images = [fitz.Rect(image["bbox"]) & page.rect for image in page.get_image_info()]
            if any(rect.get_area() > page_area * 0.35 for rect in images):
                scanned.append(index)
            elif len(images) > 1 and sum(rect.get_area() for rect in images) > page_area * 0.60:
                # Some scanners tile a page instead of embedding one image.
                # Repeated overlapping illustrations are not a tiled scan.
                bounds = fitz.Rect(images[0])
                for rect in images[1:]:
                    bounds |= rect
                if bounds.get_area() > page_area * 0.60:
                    scanned.append(index)
        return scanned

    @staticmethod
    def _safe_ocr_dpi(page):
        """Limit OCR rendering to 16 megapixels; skip impractically large pages."""
        area = page.rect.get_area()
        if not math.isfinite(area) or area <= 0:
            return None
        if area <= 16_000_000 / (200 / 72) ** 2:
            return 200
        dpi = int(72 * math.sqrt(16_000_000 / area))
        return dpi if dpi >= 72 else None

    def _ocr_textpages(self, doc, indices):
        """Build transient, geometrically aligned Tesseract text pages locally.

        Unlike flattening the entire document into text, this leaves the
        original PDF and every digital page intact. No document leaves the
        machine. An unreadable page is explicitly reported rather than guessed.
        """
        results, failed = {}, []
        if not indices:
            return results, failed
        tessdata = os.environ.get("TESSDATA_PREFIX", "")
        configured = getattr(settings, "TESSERACT_CMD", "")
        if not tessdata and configured:
            adjacent = os.path.join(os.path.dirname(configured), "tessdata")
            if os.path.isdir(adjacent):
                tessdata = adjacent
            elif os.path.isfile(configured):
                try:
                    result = subprocess.run([configured, "--list-langs"], capture_output=True, text=True, timeout=5, check=False)
                    match = re.search(r'List of available languages in "([^"]+)"', result.stdout)
                    if result.returncode == 0 and match:
                        tessdata = match.group(1)
                except (OSError, subprocess.SubprocessError):
                    pass
        if not tessdata:
            try:
                tessdata = fitz.get_tessdata()
            except (RuntimeError, OSError, AttributeError):
                tessdata = ""
        if not tessdata:
            self._log("Warning: Scanned pages detected but Tesseract OCR is not installed or its language data could not be found. Scanned content may be missing; digital pages will still be extracted.")
            return results, list(indices)
        has_english = os.path.isfile(os.path.join(tessdata, "eng.traineddata"))
        has_hindi = os.path.isfile(os.path.join(tessdata, "hin.traineddata"))
        if not has_english:
            self._log("Warning: English OCR data not found; English text may be missing or inaccurate.")
        languages = (["eng+hin", "eng"] if has_english and has_hindi else
                     ["eng"] if has_english else ["hin"] if has_hindi else [])
        for index in indices:
            page = doc.load_page(index)
            result = None
            dpi = self._safe_ocr_dpi(page)
            if dpi is None:
                failed.append(index)
                self._log(f"Warning: Page {index + 1}: page dimensions are too large for safe local OCR; questions may be missing.")
                continue
            if dpi < 200:
                self._log(f"Warning: Page {index + 1}: local OCR resolution was reduced for a large page; review carefully.")
            for language in languages:
                try:
                    textpage = page.get_textpage_ocr(language=language, dpi=dpi, full=True, tessdata=tessdata)
                    if page.get_text("text", textpage=textpage).strip():
                        result = (page, textpage)
                        if language == "eng":
                            self._log(f"Warning: Page {index + 1}: Hindi OCR data unavailable or failed; using English only.")
                        elif language == "hin":
                            self._log(f"Warning: Page {index + 1}: using Hindi-only OCR because English data is unavailable.")
                        break
                except Exception as exc:
                    # PyMuPDF's native Tesseract errors are not necessarily
                    # subclasses of RuntimeError. A broken language install
                    # must never discard otherwise readable digital pages.
                    self._log(f"Warning: Page {index + 1}: local OCR with {language} failed: {exc}")
            if result:
                results[index] = result
                self.paper.ocr_used = True
                self._log(f"Page {index + 1}: local Tesseract OCR completed; verify all text, formulas and figures.")
            else:
                failed.append(index)
                self._log(f"Warning: Page {index + 1}: local OCR found no usable text. Questions on this page may be missing.")
        return results, failed

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

                scanned = self._scanned_page_indices(doc)
                ocr_pages, unreadable_pages = self._ocr_textpages(doc, scanned)
                items = DigitalQuestionLayout(doc, self._log, ocr_pages, unreadable_pages).extract()
                if items is None:
                    self._log(
                        "Warning: Using basic text/OCR extraction; diagrams cannot be reliably assigned automatically in this mode."
                    )
                    items = self._extract_text_fallback(doc, ocr_pages, set(scanned))

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
            tables=getattr(item, "tables", []),
            source_regions=getattr(item, "source_regions", []),
            primary_image_region=getattr(item, "primary_image_region", {}),
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
        # Every extra is an original page crop. Uncertain ownership goes to the
        # pool, never silently to an arbitrary answer.
        from .models import ExtractedFigure
        for index, candidate in enumerate(getattr(item, "extra_figures", [])):
            target_key = candidate.get("option_key")
            figure = ExtractedFigure(
                paper=self.paper, question=extracted if target_key is not None else None,
                option_key=target_key or "", page_number=candidate["page"],
                bounding_box=candidate["box"], source=candidate.get("source", "embedded"), order=index,
            )
            target = figure.private_image if self.paper.is_student_private else figure.image
            target.save(f"paper_{self.paper.pk}_q{item.number}_extra_{index}.png", ContentFile(candidate["data"]), save=False)
            if self.paper.is_student_private:
                self._private_files_written.append(target.name)
            figure.save()

    def _extract_text_fallback(self, doc, ocr_pages=None, scanned_pages=None):
        """Basic parser for unlabelled/numerical and OCR-only papers.

        It deliberately does not attach the largest image on a page: that
        frequently turns a logo or an unrelated diagram into a question image.
        """
        from .pdf_layout import LayoutQuestion

        items = []
        current = None
        ocr_pages = ocr_pages or {}
        scanned_pages = scanned_pages or set()
        for page_index, page in enumerate(doc):
            page_num = page_index + 1
            if page_index in ocr_pages:
                ocr_page, textpage = ocr_pages[page_index]
                page_text = ocr_page.get_text("text", textpage=textpage, sort=True)
            else:
                page_text = page.get_text("text")
            if page_index not in ocr_pages and page_index in scanned_pages and len(page_text.strip()) < 40:
                dpi = self._safe_ocr_dpi(page)
                if self.ocr_available and pytesseract and dpi is not None:
                    self._log(f"Page {page_num}: sparse text; executing Tesseract OCR.")
                    try:
                        pix = page.get_pixmap(dpi=dpi)
                        image = Image.open(io.BytesIO(pix.tobytes("png")))
                        try:
                            ocr_text = pytesseract.image_to_string(image, lang="eng+hin")
                        except Exception:
                            ocr_text = pytesseract.image_to_string(image, lang="eng")
                            self._log(f"Warning: Page {page_num}: Hindi OCR unavailable; using English only.")
                    except Exception as exc:
                        # Rendering can fail before Tesseract is invoked. Keep
                        # all other pages' text and report the unreadable page.
                        self._log(f"Warning: Page {page_num}: OCR extraction error: {exc}")
                        ocr_text = ""
                    if len(ocr_text.strip()) > len(page_text.strip()):
                        page_text = ocr_text
                        self.paper.ocr_used = True
                elif dpi is None:
                    self._log(f"Warning: Page {page_num}: page dimensions are too large for safe OCR; questions may be missing.")
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
        return _normalize_option(raw_key)
