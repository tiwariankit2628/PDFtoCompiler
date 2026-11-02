import io
import os
import re
import shutil
from decimal import Decimal
from django.conf import settings
from django.core.files.base import ContentFile
import pymupdf as fitz
from PIL import Image

try:
    import pytesseract
except ImportError:
    pytesseract = None

from .models import ExtractedQuestion, QuestionPaperUpload


class PDFQuestionExtractor:
    """
    Commercial-Grade PDF Question Paper Parser.
    Extracts question numbers, bilingual question text (Hindi/English),
    mathematical formulas, MCQ options (A, B, C, D / 1, 2, 3, 4 / क, ख, ग, घ),
    and diagram images across multi-page examination papers.
    """

    # Question start patterns: Q.1, Q1., Question 1:, 1., 1), (1), प्रश्न 1:, प्र. 1.
    QUESTION_PATTERN = re.compile(
        r"^(?:(?:Q(?:uestion)?\.?\s*(\d+)[:.]?)|(?:(\d+)[\.\)](?!\d))|(?:प्रश्न\s*(\d+)[:.]?)|(?:प्र\.\s*(\d+)[:.]?))\s*(.*)",
        re.IGNORECASE,
    )

    # Option patterns: (A), (B), (C), (D) or A., B., C., D. or (1), (2), (3), (4) or (क), (ख), (ग), (घ)
    OPTION_INLINE_PATTERN = re.compile(
        r"(?:\(([A-Da-d1-4कखगघ])\)|(?:^|\s+)([A-Da-d1-4])\.)\s*(.*?)(?=(?:\([A-Da-d1-4कखगघ]\)|(?:^|\s+)[A-Da-d1-4]\.|$))",
        re.DOTALL,
    )

    OPTION_LINE_PATTERN = re.compile(
        r"^\s*(?:\(([A-Da-d1-4कखगघ])\)|([A-Da-d1-4])\.)\s*(.*)",
    )

    def __init__(self, paper_upload: QuestionPaperUpload):
        self.paper = paper_upload
        self.logs = []
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

    def extract(self) -> int:
        """
        Executes the complete extraction pipeline.
        Returns total number of questions extracted.
        """
        self.paper.status = QuestionPaperUpload.ProcessingStatus.PROCESSING
        self.paper.save(update_fields=["status"])
        self._log(f"Starting extraction for paper: '{self.paper.title}'")

        try:
            # Open PDF using PyMuPDF
            pdf_bytes = self.paper.pdf_file.read()
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            total_pages = len(doc)
            self.paper.total_pages = total_pages
            self._log(f"PDF verified: {total_pages} total pages.")

            # Clear any previous extracted items for this paper to prevent duplication
            self.paper.extracted_questions.all().delete()

            extracted_items = []
            current_q = None
            page_images_map = {}

            # Step 1: Extract text and images page by page
            for page_idx in range(total_pages):
                page_num = page_idx + 1
                page = doc[page_idx]

                # Extract text
                page_text = page.get_text("text")

                # Check if page is scanned (sparse digital text)
                if len(page_text.strip()) < 40:
                    if self.ocr_available and pytesseract:
                        self._log(f"Page {page_num}: Scanned page detected. Executing Tesseract OCR...")
                        pix = page.get_pixmap(dpi=200)
                        img = Image.open(io.BytesIO(pix.tobytes("png")))
                        try:
                            ocr_text = pytesseract.image_to_string(img, lang="eng+hin")
                        except Exception:
                            # Fall back to English OCR if Hindi traineddata is not installed
                            try:
                                ocr_text = pytesseract.image_to_string(img, lang="eng")
                            except Exception as ocr_err:
                                self._log(f"Page {page_num}: OCR extraction error: {str(ocr_err)}")
                                ocr_text = ""
                        if len(ocr_text.strip()) > len(page_text.strip()):
                            page_text = ocr_text
                            self.paper.ocr_used = True
                    else:
                        self._log(
                            f"Page {page_num}: Scanned page detected, but Tesseract OCR is not installed. "
                            f"To enable OCR for scanned papers, install Tesseract OCR and set TESSERACT_CMD."
                        )

                # Extract embedded diagram/image from page if present
                image_file = self._extract_page_image(doc, page, page_num)
                if image_file:
                    page_images_map[page_num] = image_file

                # Parse lines on the page
                lines = [line.strip() for line in page_text.splitlines() if line.strip()]
                for line in lines:
                    # Check if line initiates a new Question
                    q_match = self.QUESTION_PATTERN.match(line)
                    if q_match:
                        # Save previous question if exists
                        if current_q:
                            extracted_items.append(current_q)

                        q_num_str = (
                            q_match.group(1)
                            or q_match.group(2)
                            or q_match.group(3)
                            or q_match.group(4)
                        )
                        q_num = int(q_num_str) if q_num_str and q_num_str.isdigit() else (len(extracted_items) + 1)
                        q_body = q_match.group(5).strip()

                        current_q = {
                            "number": q_num,
                            "text": q_body,
                            "options": [],
                            "page": page_num,
                            "image": page_images_map.get(page_num),
                        }
                        continue

                    # If inside a question, check for Option line
                    if current_q:
                        opt_match = self.OPTION_LINE_PATTERN.match(line)
                        if opt_match:
                            opt_key = (opt_match.group(1) or opt_match.group(2)).upper()
                            opt_text = opt_match.group(3).strip()
                            current_q["options"].append({"key": self._normalize_key(opt_key), "text": opt_text})
                            continue

                        # Check for inline options on the same line (e.g. (A) 12  (B) 15  (C) 18  (D) 24)
                        inline_opts = self.OPTION_INLINE_PATTERN.findall(line)
                        if len(inline_opts) >= 2:
                            for key, key2, text in inline_opts:
                                opt_k = (key or key2).upper()
                                current_q["options"].append({
                                    "key": self._normalize_key(opt_k),
                                    "text": text.strip(),
                                })
                            continue

                        # Otherwise append to question body or last option
                        if current_q["options"]:
                            # Append to last option
                            current_q["options"][-1]["text"] += " " + line
                        else:
                            # Append to question text
                            current_q["text"] += " " + line

            if current_q:
                extracted_items.append(current_q)

            # Step 2: Persist ExtractedQuestion records
            saved_count = 0
            for item in extracted_items:
                q_text = item["text"].strip()
                options = item["options"]

                # Validate and flag if incomplete
                is_flagged = False
                flag_reason = ""
                if not q_text:
                    is_flagged = True
                    flag_reason = "Missing question statement."
                elif len(options) < 2:
                    is_flagged = True
                    flag_reason = f"Only {len(options)} option(s) detected. Minimum 2 required for MCQ."

                extracted_obj = ExtractedQuestion(
                    paper=self.paper,
                    question_number=item["number"],
                    question_text=q_text,
                    options=options,
                    page_number=item["page"],
                    status=ExtractedQuestion.Status.EXTRACTED,
                    is_flagged=is_flagged,
                    flag_reason=flag_reason,
                    positive_marks=Decimal("4.00"),
                    negative_marks=Decimal("1.00"),
                )

                if item.get("image"):
                    filename, content = item["image"]
                    extracted_obj.image.save(filename, content, save=False)

                extracted_obj.save()
                saved_count += 1

            self.paper.questions_found = saved_count
            self.paper.status = (
                QuestionPaperUpload.ProcessingStatus.EXTRACTED
                if saved_count > 0
                else QuestionPaperUpload.ProcessingStatus.FAILED
            )
            self._log(f"Extraction completed successfully. {saved_count} questions extracted.")

        except Exception as e:
            self._log(f"Extraction error: {str(e)}")
            self.paper.status = QuestionPaperUpload.ProcessingStatus.FAILED

        self.paper.extraction_log = "\n".join(self.logs)
        self.paper.save()
        return self.paper.questions_found

    def _normalize_key(self, raw_key: str) -> str:
        """Standardizes option letters to A, B, C, D."""
        mapping = {
            "1": "A", "2": "B", "3": "C", "4": "D",
            "क": "A", "ख": "B", "ग": "C", "घ": "D",
        }
        return mapping.get(raw_key, raw_key.upper())

    def _extract_page_image(self, doc, page, page_num):
        """Extracts the largest diagram image from page if present."""
        try:
            image_list = page.get_images(full=True)
            if not image_list:
                return None

            # Pick largest image by size (to avoid tiny icons/logos)
            largest_img = None
            max_size = 0

            for img_info in image_list:
                xref = img_info[0]
                base_img = doc.extract_image(xref)
                img_bytes = base_img["image"]
                img_ext = base_img["ext"]
                if len(img_bytes) > max_size and len(img_bytes) > 2048:  # Minimum 2KB
                    max_size = len(img_bytes)
                    largest_img = (f"paper_{self.paper.pk}_p{page_num}_{xref}.{img_ext}", ContentFile(img_bytes))

            return largest_img
        except Exception:
            return None
