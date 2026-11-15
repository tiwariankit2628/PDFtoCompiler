"""Image-first support: render high-resolution crops of the ORIGINAL PDF so a
student can take a test on the printed question itself (statement, options,
formulas, tables and diagrams) instead of retyping or trusting imperfect OCR.

Nothing here contacts an external service. Crops are rendered locally with
PyMuPDF and stored through the caller's private/public storage. OCR text stays
optional: an inaccurate or empty transcription never blocks an image question.
"""
import re

import pymupdf as fitz

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
    box = validate_box(box)
    if box is None:
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
            regions.append({"page": int(region["page"]), "box": region["box"]})
    if not regions:
        primary = question.primary_image_region or {}
        if isinstance(primary, dict) and primary.get("box") and primary.get("page"):
            regions.append({"page": int(primary["page"]), "box": primary["box"]})
    return regions


def render_question_crops(doc, regions, *, scale=CROP_SCALE, max_side=CROP_MAX_SIDE):
    """Render each region to PNG bytes. Returns [(page_number, box, png_bytes)].

    Individual failures are skipped (not fatal): a missing crop leaves the
    question on its text, it never aborts extraction.
    """
    crops = []
    for region in regions:
        page_number = region.get("page")
        box = region.get("box")
        if not page_number or not box or not (1 <= page_number <= len(doc)):
            continue
        page = doc[page_number - 1]
        rect = _rect_from_box(page, box)
        if rect is None:
            continue
        try:
            data = page_image(page, box, max_side=max_side, scale=scale)
        except Exception:
            continue
        if data:
            crops.append((page_number, box, data))
    return crops


def detect_crop_answer_warning(doc, regions):
    """Best-effort check whether a crop region's text looks like it contains a
    printed answer or solution. Returns a short warning string, or "".

    This is advisory only and deliberately conservative; it is never presented
    as a guarantee that every leaked answer is caught.
    """
    for region in regions:
        page_number = region.get("page")
        box = region.get("box")
        if not page_number or not box or not (1 <= page_number <= len(doc)):
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
