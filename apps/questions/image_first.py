"""Image-first support: render high-resolution crops of the ORIGINAL PDF so a
student can take a test on the printed question itself (statement, options,
formulas, tables and diagrams) instead of retyping or trusting imperfect OCR.

Nothing here contacts an external service. Crops are rendered locally with
PyMuPDF and stored through the caller's private/public storage. OCR text stays
optional: an inaccurate or empty transcription never blocks an image question.
"""
import io
import math
import re

import pymupdf as fitz
from PIL import Image

from .figure_utils import page_image, validate_box

# High-resolution defaults for crops a student will read and zoom on a phone.
# Higher than the 2x/2200px figure default so small print stays legible.
CROP_SCALE = 3.0
CROP_MAX_SIDE = 3200

# Conservative, best-effort detection of a printed answer/solution that may have
# bled into a crop. This never claims to catch every case; it only warns the
# reviewer so they can recrop or remove the region. Patterns require an answer
# token tied to an option letter/number or an explicit solution heading, to
# avoid flagging ordinary question wording that merely contains "answer".
_ANSWER_PATTERNS = [
    re.compile(r"\bans(?:wer)?\s*[:.\-=)]?\s*\(?\s*[A-Da-d1-4]\s*\)?", re.I),
    re.compile(r"\bcorrect\s+(?:option|answer|choice|response)\b", re.I),
    re.compile(r"\b(?:sol|soln|solution|explanation)\b\s*[:.\-]", re.I),
    re.compile(r"\bhence\b[^.\n]{0,40}\bcorrect\b", re.I),
    re.compile(r"\bthe\s+answer\s+is\b", re.I),
]


def _rect_from_box(page, box):
    """Normalized [x0,y0,x1,y1] -> a clipped fitz.Rect in PDF points."""
    try:
        box = validate_box(box)
    except (TypeError, ValueError):
        return None
    rect = fitz.Rect(
        page.rect.x0 + box[0] * page.rect.width,
        page.rect.y0 + box[1] * page.rect.height,
        page.rect.x0 + box[2] * page.rect.width,
        page.rect.y0 + box[3] * page.rect.height,
    ) & page.rect
    if rect.is_empty or rect.width < 2 or rect.height < 2:
        return None
    return rect


def question_crop_regions(question):
    """Regions to render for a question, newest-first preference.

    Prefers the per-column/page source_regions (they bound the whole question);
    falls back to the primary image region. Returns a list of {"page", "box"}.
    """
    regions = []
    for region in (question.source_regions or []):
        if isinstance(region, dict) and region.get("box") and region.get("page"):
            try:
                page = int(region["page"])
                if page > 0:
                    regions.append({"page": page, "box": region["box"]})
            except (TypeError, ValueError, OverflowError):
                continue
    if not regions:
        primary = question.primary_image_region or {}
        if isinstance(primary, dict) and primary.get("box") and primary.get("page"):
            try:
                page = int(primary["page"])
                if page > 0:
                    regions.append({"page": page, "box": primary["box"]})
            except (TypeError, ValueError, OverflowError):
                pass
    return regions


def render_question_crops(doc, regions, *, scale=CROP_SCALE, max_side=CROP_MAX_SIDE):
    """Render one image per source page, preserving the reading order of parts.

    If extraction found separate regions on the same page (e.g. across two
    columns), join their original pixels vertically rather than showing them as
    separate questions or including the intervening unrelated page content.
    Their exact source boxes remain in question.source_regions. A failed part
    invalidates that page's composite so the UI can reveal its fallback.
    """
    pages = {}
    for region in regions:
        if not isinstance(region, dict):
            continue
        page_number, box = region.get("page"), region.get("box")
        if not isinstance(page_number, int) or not 1 <= page_number <= len(doc):
            continue
        pages.setdefault(page_number, []).append(box)

    crops = []
    for page_number in sorted(pages):
        boxes = pages[page_number]
        page = doc[page_number - 1]
        if any(not box or _rect_from_box(page, box) is None for box in boxes):
            continue
        # A full region can contain a supplementary region; rendering that
        # inner rectangle again would duplicate content in the primary image.
        rects = [_rect_from_box(page, box) for box in boxes]
        unique = []
        for index, rect in enumerate(rects):
            if any(other.contains(rect) and (other != rect or earlier < index)
                   for earlier, other in enumerate(rects) if earlier != index):
                continue
            unique.append(boxes[index])
        try:
            rendered = [page_image(page, box, max_side=max_side, scale=scale) for box in unique]
            if not rendered or any(not data for data in rendered):
                continue
            if len(rendered) == 1:
                crops.append((page_number, unique[0], rendered[0]))
                continue
            images = []
            for data in rendered:
                with Image.open(io.BytesIO(data)) as image:
                    images.append(image.convert("RGB"))
            width = max(image.width for image in images)
            gap = 16
            height = sum(image.height for image in images) + gap * (len(images) - 1)
            # Bound memory without clipping. A zoomable, uncropped original is
            # preferable to an unbounded composite on unusually large pages.
            factor = min(1.0, math.sqrt(24_000_000 / (width * height)), 9600 / height)
            if factor < 1:
                images = [image.resize((max(1, round(image.width * factor)),
                                        max(1, round(image.height * factor))), Image.Resampling.LANCZOS)
                          for image in images]
                gap = max(1, round(gap * factor))
                width = max(image.width for image in images)
                height = sum(image.height for image in images) + gap * (len(images) - 1)
            composite = Image.new("RGB", (width, height), "white")
            y = 0
            for image in images:
                composite.paste(image, (0, y))
                y += image.height + gap
            output = io.BytesIO()
            composite.save(output, format="PNG")
            # No single PDF rectangle describes a composite. The original
            # rectangles are retained separately in source_regions.
            crops.append((page_number, [], output.getvalue()))
        except Exception:
            # A failed part is not presented as a complete page.
            continue
    return crops


def detect_crop_answer_warning(doc, regions):
    """Best-effort check whether a crop region's text looks like it contains a
    printed answer or solution. Returns a short warning string, or "".

    This is advisory only and deliberately conservative; it is never presented
    as a guarantee that every leaked answer is caught.
    """
    for region in regions:
        if not isinstance(region, dict):
            continue
        page_number = region.get("page")
        box = region.get("box")
        if not isinstance(page_number, int) or not box or not (1 <= page_number <= len(doc)):
            continue
        page = doc[page_number - 1]
        rect = _rect_from_box(page, box)
        if rect is None:
            continue
        try:
            text = page.get_text("text", clip=rect) or ""
        except Exception:
            continue
        for pattern in _ANSWER_PATTERNS:
            if pattern.search(text):
                return ("This crop may contain a printed answer or solution. "
                        "Review and recrop or remove it if so (automatic detection is not exhaustive).")
    return ""
