"""Consent-gated Mathpix -> Gemini extraction with original-PDF figure crops.

Provider output is untrusted and never overwrites an existing review workspace.
Every AI-derived question is flagged for a human comparison with the source.
"""
import io
import math
import re
from collections import Counter, defaultdict

import pymupdf as fitz
from PIL import Image
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from .cloud_providers import CloudExtractionError, GeminiClient, MathpixClient, cloud_is_configured
from .figure_utils import page_image, validate_box
from .models import ExtractedFigure, ExtractedQuestion, QuestionPaperUpload
from .pdf_layout import DigitalQuestionLayout, FIGURE_REFERENCE

FIGURE_TYPES = {"diagram", "chart", "table", "figure", "image"}
KEYS = {"1": "A", "2": "B", "3": "C", "4": "D", "5": "E", "क": "A", "ख": "B", "ग": "C", "घ": "D"}
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_FIGURES = 400
MAX_TOTAL_CROP_PIXELS = 100_000_000


def _normalize_region(region, width, height):
    """Normalize a Mathpix pixel rectangle; reject, don't silently clip, bad data."""
    if not isinstance(region, dict):
        return None
    try:
        x = float(region["top_left_x"]); y = float(region["top_left_y"])
        w = float(region["width"]); h = float(region["height"])
        return validate_box([x / width, y / height, (x + w) / width, (y + h) / height])
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def _iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union else 0


def _duplicate_box(a, b):
    """Recognize the same raster inside a slightly padded OCR region."""
    if _iou(a, b) > .85:
        return True
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    smaller, larger = min(area_a, area_b), max(area_a, area_b)
    return bool(smaller and intersection / smaller > .95 and smaller / larger > .60)


def build_pages(doc, result, log):
    """Validate Mathpix page output and collect bounded original figure candidates.

    OCR bounding boxes are only used for cropping when page aspect ratios match;
    an auto-rotated/cropped provider frame cannot safely be mapped back to the PDF.
    Embedded PDF images supplement (not replace) Mathpix diagram detection.
    """
    raw_pages = result.get("pages")
    if not isinstance(raw_pages, list) or len(raw_pages) != len(doc):
        raise CloudExtractionError("Mathpix did not return every PDF page; no partial extraction was saved.")
    by_number = {}
    for raw in raw_pages:
        if not isinstance(raw, dict) or type(raw.get("page")) is not int or raw["page"] in by_number:
            raise CloudExtractionError("Mathpix returned invalid or duplicate page numbers.")
        by_number[raw["page"]] = raw
    if set(by_number) != set(range(1, len(doc) + 1)):
        raise CloudExtractionError("Mathpix page numbers do not match the uploaded PDF.")

    pages, candidates = [], {}
    for page_number in range(1, len(doc) + 1):
        page = doc[page_number - 1]
        raw = by_number[page_number]
        try:
            width, height = float(raw["page_width"]), float(raw["page_height"])
            if not (math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise CloudExtractionError(f"Mathpix returned invalid dimensions for page {page_number}.") from None
        rotation = raw.get("auto_rotate_degrees", 0)
        aspect_ok = (rotation in (0, None) and abs((width / height) / (page.rect.width / page.rect.height) - 1) < 0.04)
        if not aspect_ok:
            log(f"Warning: Page {page_number} OCR coordinates do not align with the PDF; automatic OCR crops were withheld. Use manual cropping.")
        lines = raw.get("lines")
        if not isinstance(lines, list) or len(lines) > 5000:
            raise CloudExtractionError(f"Mathpix returned invalid or excessive OCR data for page {page_number}.")
        clean_lines, figures = [], []
        total_chars = 0
        for line in lines:
            if not isinstance(line, dict):
                raise CloudExtractionError(f"Mathpix returned malformed OCR data on page {page_number}.")
            text = line.get("text_display") or line.get("text") or ""
            if not isinstance(text, str):
                raise CloudExtractionError(f"Mathpix returned malformed OCR text on page {page_number}.")
            total_chars += len(text)
            if total_chars > 200000:
                raise CloudExtractionError(f"Mathpix OCR data is too large on page {page_number}.")
            kind = str(line.get("type", "text"))[:40].lower()
            box = _normalize_region(line.get("region"), width, height)
            confidence = line.get("confidence_rate")
            if not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
                confidence = None
            clean_lines.append({"type": kind, "text": text, "box": box,
                                "confidence": confidence})
            if kind in FIGURE_TYPES and box and aspect_ok:
                figures.append({"page": page_number, "box": box, "type": kind, "source": "mathpix"})
        for img in page.get_image_info():
            try:
                rect = fitz.Rect(img["bbox"]) & page.rect
                box = validate_box([(rect.x0 - page.rect.x0) / page.rect.width,
                                    (rect.y0 - page.rect.y0) / page.rect.height,
                                    (rect.x1 - page.rect.x0) / page.rect.width,
                                    (rect.y1 - page.rect.y0) / page.rect.height])
            except (KeyError, ValueError, TypeError):
                continue
            # A full-page scan isn't a diagram. Keep smaller figures in the
            # pool even when they may be branding; the teacher can discard them.
            area = (box[2] - box[0]) * (box[3] - box[1])
            if not 0.0005 <= area <= 0.65 or any(_duplicate_box(box, f["box"]) for f in figures):
                continue
            figures.append({"page": page_number, "box": box, "type": "embedded_image", "source": "embedded"})
        figures.sort(key=lambda item: (item["box"][1], item["box"][0]))
        public_figures = []
        for index, figure in enumerate(figures, 1):
            if len(candidates) >= MAX_FIGURES:
                raise CloudExtractionError(f"More than {MAX_FIGURES} figure regions were detected; split the PDF and review manually.")
            candidate_id = f"p{page_number}-r{index}"
            figure["id"] = candidate_id
            candidates[candidate_id] = figure
            public_figures.append({"id": candidate_id, "type": figure["type"], "box": figure["box"]})
        low_confidence = any(line["text"] and line["confidence"] is not None and line["confidence"] < .8 for line in clean_lines)
        pages.append({"page": page_number, "lines": clean_lines, "candidates": public_figures,
                      "coordinates_align": aspect_ok, "low_confidence": low_confidence})
    return pages, candidates


def _as_strings(value, label):
    if not isinstance(value, list) or len(value) > 100 or any(not isinstance(x, str) or len(x) > 1000 for x in value):
        raise CloudExtractionError(f"Gemini returned an invalid {label} list.")
    return value


def validate_questions(raw_items, first_page, last_page, log, context_end=None):
    if not isinstance(raw_items, list) or len(raw_items) > 200:
        raise CloudExtractionError("Gemini returned too many questions in a page batch.")
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict) or type(raw.get("number")) is not int or not 1 <= raw["number"] <= 100000:
            raise CloudExtractionError("Gemini returned an invalid question number.")
        page = raw.get("page_number")
        if type(page) is not int or page < first_page or page > last_page:
            # A context-page question will be processed in its own batch.
            log("Warning: Gemini returned a question outside its requested page range; it was not saved from that batch.")
            continue
        end_page = raw.get("end_page", page)
        if type(end_page) is not int or end_page < page or end_page > (context_end if context_end is not None else last_page + 1):
            raise CloudExtractionError("Gemini returned an invalid question continuation page.")
        text = raw.get("text")
        if not isinstance(text, str) or len(text) > 30000:
            raise CloudExtractionError("Gemini returned invalid question text.")
        raw_options = raw.get("options")
        if not isinstance(raw_options, list) or len(raw_options) > 10:
            raise CloudExtractionError("Gemini returned invalid options.")
        options = []
        for opt in raw_options:
            if not isinstance(opt, dict) or not isinstance(opt.get("key"), str) or not isinstance(opt.get("text"), str) or len(opt["text"]) > 15000:
                raise CloudExtractionError("Gemini returned invalid option data.")
            key = KEYS.get(opt["key"].strip(), opt["key"].strip().upper())
            if key not in {"A", "B", "C", "D", "E"}:
                raise CloudExtractionError("Gemini returned an unsupported option label.")
            options.append({"key": key, "text": opt["text"].strip(),
                            "image_ids": _as_strings(opt.get("image_ids", []), "option image")})
        issues = _as_strings(raw.get("issues", []), "issue")
        item = {"number": raw["number"], "page_number": page, "end_page": end_page, "text": text.strip(), "options": options,
                "image_ids": _as_strings(raw.get("image_ids", []), "question image"), "issues": list(issues)}
        if not item["text"]:
            item["issues"].append("Missing question statement.")
        if len(options) < 2:
            item["issues"].append("Fewer than two options detected.")
        if len({opt["key"] for opt in options}) != len(options):
            item["issues"].append("Duplicate option labels.")
        for opt in options:
            if not opt["text"] and not opt["image_ids"]:
                item["issues"].append(f"Option {opt['key']} has no text or image.")
        items.append(item)
    return items


def match_figures(items, candidates):
    """Accept only unambiguous candidate IDs on the start/continuation page."""
    claims = defaultdict(list)
    for item_index, item in enumerate(items):
        for figure_id in item["image_ids"]:
            claims[figure_id].append((item_index, ""))
        for opt in item["options"]:
            for figure_id in opt["image_ids"]:
                claims[figure_id].append((item_index, opt["key"]))
    assignments = {}
    for figure_id, owners in claims.items():
        if figure_id not in candidates or len(owners) != 1:
            for index, _ in owners:
                items[index]["issues"].append("An image reference was invalid or shared; assign it manually.")
            continue
        index, option_key = owners[0]
        figure = candidates[figure_id]
        if figure["page"] not in (items[index]["page_number"], items[index]["page_number"] + 1):
            items[index]["issues"].append("A figure is outside the question's source/continuation pages; assign manually.")
            continue
        assignments[figure_id] = (index, option_key)
    for index, item in enumerate(items):
        for opt in item["options"]:
            if not opt["text"] and not any(owner == (index, opt["key"]) for owner in assignments.values()):
                item["issues"].append(f"Option {opt['key']} needs an image or text.")
        if FIGURE_REFERENCE.search(item["text"]) and not any(owner[0] == index for owner in assignments.values()):
            item["issues"].append("The question references a figure, but none was confidently matched.")
    return assignments


def _check_crop_budget(doc, candidates):
    """Avoid unbounded rasterization/storage from pathological region output."""
    total = 0
    for figure in candidates.values():
        page = doc[figure["page"] - 1]
        x0, y0, x1, y1 = figure["box"]
        width, height = (x1 - x0) * page.rect.width, (y1 - y0) * page.rect.height
        factor = min(2, 2200 / max(width, height))
        total += math.ceil(width * factor) * math.ceil(height * factor)
        if total > MAX_TOTAL_CROP_PIXELS:
            raise CloudExtractionError("Detected figure crops exceed the processing budget; split the PDF or crop them manually.")


def _gemini_page_image(page):
    """Bound inline request size; stored original-PDF figure crops stay PNG."""
    source = page_image(page, max_side=1800, scale=1.8)
    with Image.open(io.BytesIO(source)) as image:
        image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


class CloudPDFQuestionExtractor:
    def __init__(self, paper, mathpix=None, gemini=None):
        self.paper = paper
        self.mathpix = mathpix or MathpixClient(on_submitted=self._record_provider_id)
        self.gemini = gemini or GeminiClient()
        self.logs = []
        self._stored_files = []

    def log(self, message):
        self.logs.append(message)

    def _record_provider_id(self, pdf_id):
        # Persist promptly so an operator can contact Mathpix if a worker dies
        # before the cleanup request. This is a document reference, not a key.
        QuestionPaperUpload.objects.filter(pk=self.paper.pk).update(mathpix_document_id=pdf_id)
        self.paper.mathpix_document_id = pdf_id

    def extract(self):
        paper = self.paper
        with transaction.atomic():
            locked = QuestionPaperUpload.objects.select_for_update().get(pk=paper.pk)
            if locked.extracted_questions.exists() or locked.figures.exists():
                self.log("Warning: Existing review work was preserved. Upload a new copy to extract again; no data was sent.")
                locked.extraction_log = "\n".join(filter(None, [locked.extraction_log, *self.logs]))
                locked.save(update_fields=["extraction_log"])
                return 0
            if locked.status != QuestionPaperUpload.ProcessingStatus.PENDING:
                # Duplicate deliveries must not create another paid provider request.
                return 0
            if (locked.extraction_mode != QuestionPaperUpload.ExtractionMode.CLOUD
                    or not locked.cloud_consent_at or not cloud_is_configured()):
                locked.status = QuestionPaperUpload.ProcessingStatus.FAILED
                locked.extraction_log = "Cloud extraction is disabled, unconfigured, or missing per-upload consent; no data was sent."
                locked.save(update_fields=["status", "extraction_log"])
                return 0
            claimed = QuestionPaperUpload.objects.filter(
                pk=locked.pk, status=QuestionPaperUpload.ProcessingStatus.PENDING
            ).update(status=QuestionPaperUpload.ProcessingStatus.PROCESSING,
                     extraction_log="Starting consented cloud extraction.")
            if not claimed:
                return 0
        paper.status = QuestionPaperUpload.ProcessingStatus.PROCESSING
        succeeded = False
        try:
            source_field = paper.private_pdf_file if paper.is_student_private else paper.pdf_file
            with source_field.open("rb") as source:
                pdf_bytes = source.read(MAX_PDF_BYTES + 1)
            if len(pdf_bytes) > MAX_PDF_BYTES:
                raise CloudExtractionError("PDF exceeds the 25 MB cloud upload limit; no provider request was made.")
            with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
                if not len(doc) or doc.needs_pass or len(doc) > settings.PDF_CLOUD_MAX_PAGES:
                    raise CloudExtractionError("PDF is empty, password-protected, or exceeds the configured cloud page limit.")
                paper.total_pages = len(doc)
                self.log(f"Cloud extraction started for {len(doc)} page(s).")
                try:
                    ocr = self.mathpix.read_pdf(pdf_bytes)
                finally:
                    # Request deletion after retrieving OCR or on a processing error.
                    # CDN caches and billing metadata can remain under provider policy.
                    if self.mathpix.pdf_id:
                        if self.mathpix.delete():
                            self.log("Mathpix accepted the document deletion request; provider cache/metadata policies still apply.")
                        else:
                            self.log(f"Warning: Mathpix deletion request failed for document {self.mathpix.pdf_id}; contact Mathpix to delete it.")
                pages, candidates = build_pages(doc, ocr, self.log)
                _check_crop_budget(doc, candidates)
                items = []
                # Two primary pages, plus one page before and after for
                # cross-page continuation and column-order context.
                for start in range(0, len(doc), 2):
                    last = min(start + 2, len(doc))
                    context_start = max(0, start - 1)
                    context_end = min(last + 1, len(doc))
                    image_pages = [(i + 1, _gemini_page_image(doc[i]))
                                   for i in range(context_start, context_end)]
                    raw_items = self.gemini.interpret(start + 1, last, pages[context_start:context_end], image_pages)
                    items.extend(validate_questions(raw_items, start + 1, last, self.log, context_end))
                if not items:
                    raise CloudExtractionError("No questions were identified; no partial extraction was saved.")
                for item in items:
                    for source_page in pages[item["page_number"] - 1:item["end_page"]]:
                        if source_page["low_confidence"]:
                            item["issues"].append(f"Low OCR confidence on source page {source_page['page']}.")
                        if not source_page["coordinates_align"]:
                            item["issues"].append(f"OCR figure coordinates on page {source_page['page']} need manual review.")
                found_numbers = {item["number"] for item in items}
                explicit_ocr_numbers = set()
                label = re.compile(r"^\s*(?:Q(?:uestion)?\.?|प्रश्न|प्र\.)\s*(\d+)\b", re.IGNORECASE)
                for page in pages:
                    for line in page["lines"]:
                        match = label.match(line["text"])
                        if match:
                            explicit_ocr_numbers.add(int(match.group(1)))
                missing_ocr = sorted(explicit_ocr_numbers - found_numbers)
                if missing_ocr:
                    self.log("Warning: Explicit OCR labels missing from cloud output: "
                             + ", ".join(map(str, missing_ocr[:40])) + (" ..." if len(missing_ocr) > 40 else "")
                             + ". Compare the source and add any missing questions manually.")
                for previous, current in zip(items, items[1:]):
                    if 1 < current["number"] - previous["number"] <= 100:
                        self.log(f"Warning: Question numbers jump from {previous['number']} to {current['number']}; check for missing questions.")
                        current["issues"].append("Question numbering has a gap before this item; compare the source.")
                duplicate_numbers = {n for n, count in Counter(item["number"] for item in items).items() if count > 1}
                if duplicate_numbers:
                    self.log("Warning: Duplicate question numbers were detected; check section boundaries and numbering.")
                    for item in items:
                        if item["number"] in duplicate_numbers:
                            item["issues"].append("Duplicate question number in this paper.")
                # Digital labels offer an independent coverage check, not a
                # replacement for the cloud OCR. Scanned pages may have none.
                try:
                    local_items = DigitalQuestionLayout(doc, lambda _: None).extract() or []
                    missing = sorted({item.number for item in local_items} - {item["number"] for item in items})
                    if missing:
                        self.log("Warning: Digital layout detected question numbers missing from cloud output: "
                                 + ", ".join(map(str, missing[:40])) + (" ..." if len(missing) > 40 else "")
                                 + ". Compare the source and add missing questions manually.")
                except Exception:
                    self.log("Warning: Independent digital-layout coverage check could not be completed.")
                assignments = match_figures(items, candidates)
                self._save(doc, items, candidates, assignments)
                succeeded = True
                paper.questions_found = len(items)
                paper.ocr_used = True
                paper.status = QuestionPaperUpload.ProcessingStatus.EXTRACTED
                self.log(f"Extracted {len(items)} questions and {len(candidates)} original-PDF figure candidates.")
                self.log("All AI questions are flagged for human verification; answer keys were not guessed.")
                unassigned = len(candidates) - len(assignments)
                if unassigned:
                    self.log(f"Warning: {unassigned} figure candidate(s) remain unassigned and may include logos; review the image pool.")
        except CloudExtractionError as exc:
            self.log(f"Cloud extraction failed: {exc}")
            paper.status = QuestionPaperUpload.ProcessingStatus.FAILED
        except Exception:
            # Don't expose unexpected provider bodies, credentials, or document
            # text via the teacher-facing diagnostic log.
            self.log("Cloud extraction failed unexpectedly. No partial results were saved; check server configuration.")
            paper.status = QuestionPaperUpload.ProcessingStatus.FAILED
        finally:
            if not succeeded:
                for storage, name in self._stored_files:
                    try:
                        storage.delete(name)
                    except Exception:
                        self.log("Warning: A temporary image could not be removed from storage.")
            paper.extraction_log = "\n".join(self.logs)
            paper.save(update_fields=["status", "extraction_log", "total_pages", "questions_found", "ocr_used"])
        return len(items) if succeeded else 0

    def _save(self, doc, items, candidates, assignments):
        with transaction.atomic():
            locked = QuestionPaperUpload.objects.select_for_update().get(pk=self.paper.pk)
            if locked.extracted_questions.exists() or locked.figures.exists():
                raise CloudExtractionError("Review work was added during extraction; it was preserved. Upload a new copy to retry.")
            saved = []
            for item in items:
                issues = ["AI extraction: review text, formulas, options and diagrams against the source.", *item["issues"]]
                saved.append(ExtractedQuestion.objects.create(
                    paper=locked, question_number=item["number"], page_number=item["page_number"],
                    question_text=item["text"], source_pages=list(range(item["page_number"], item["end_page"] + 1)),
                    extraction_issues=issues,
                    options=[{"key": o["key"], "text": o["text"]} for o in item["options"]],
                    correct_option="", status=ExtractedQuestion.Status.EXTRACTED, is_flagged=True,
                    flag_reason=" ".join(dict.fromkeys(issues))[:255],
                ))
            for order, (candidate_id, figure) in enumerate(candidates.items()):
                assignment = assignments.get(candidate_id)
                image = ExtractedFigure(
                    paper=locked, question=saved[assignment[0]] if assignment else None,
                    option_key=assignment[1] if assignment else "", page_number=figure["page"],
                    bounding_box=figure["box"], source=figure["source"], order=order,
                )
                png = page_image(doc[figure["page"] - 1], figure["box"])
                target = image.private_image if locked.is_student_private else image.image
                target.save(f"paper_{locked.pk}_{candidate_id}.png", ContentFile(png), save=False)
                self._stored_files.append((target.storage, target.name))
                image.save()
