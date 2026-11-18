"""Shared display policy for the original question image and safe diagnostics."""
import re

from .models import AnswerStatus, ExtractedQuestion


TECHNICAL_NAMES = re.compile(r"pytesseract|tesseract|pymupdf|\bfitz\b", re.I)


def user_diagnostic(value):
    """Keep useful diagnostic content without exposing implementation names in the UI.

    Raw database logs remain unchanged for operators; this is not an attempt to
    hide project dependencies from people with access to the source code.
    """
    return TECHNICAL_NAMES.sub("text recognition", value or "")


def use_original_image(question, *, image_first, private):
    """Require the original for crop-only content without changing exam settings."""
    if not question.is_image_first:
        return False
    if image_first or not (question.question_text or "").strip():
        return True
    for option in question.options.all():
        if (option.option_text or "").strip():
            continue
        has_figure = any((figure.private_image if private else figure.image) for figure in option.figures.all())
        if not has_figure and not (not private and option.image):
            return True
    return False


def primary_question_crops(question, *, image_first, private):
    """Return all ordered full-question parts, never the supplementary crops."""
    if not image_first or not question.is_image_first:
        return []
    crops = []
    for figure in question.figures.all():
        # Staged figures use option_key; bank figures use option_id.
        if getattr(figure, "option_key", "") or getattr(figure, "option_id", None):
            continue
        field = figure.private_image if private else figure.image
        if figure.source == "question_crop" and field:
            crops.append(figure)
    return sorted(crops, key=lambda figure: (figure.page_number, figure.order, figure.pk))


def unavailable_crop_pages(crops, *, private):
    """Identify saved image references whose files can no longer be opened."""
    unavailable = set()
    for figure in crops:
        field = figure.private_image if private else figure.image
        try:
            available = bool(field and field.name and field.storage.exists(field.name))
        except (OSError, ValueError):
            available = False
        if not available:
            unavailable.add(figure.page_number)
    return sorted(unavailable)


def missing_crop_pages(question, crops):
    """Detect known missing continuation pages; not a full crop completeness test."""
    expected = {
        region.get("page") for region in (question.source_regions or [])
        if isinstance(region, dict) and isinstance(region.get("page"), int) and region["page"] > 0
    }
    expected.update(page for page in (getattr(question, "source_pages", None) or [])
                    if isinstance(page, int) and page > 0)
    found = {figure.page_number for figure in crops}
    return sorted(expected - found)


def answer_key_preview_questions(questions, *, student=False, exclude_approved=False):
    """Minimal data for an advisory typed-key preview; the server validates again."""
    confirmed = AnswerStatus.STUDENT_CONFIRMED if student else AnswerStatus.MANUALLY_VERIFIED
    rows = []
    for question in questions:
        options = question.options if isinstance(question.options, list) else []
        keys = [str(option.get("key", "")).strip().upper()
                for option in options if isinstance(option, dict)]
        rows.append({
            "number": question.question_number,
            "options": keys,
            "single": question.detected_type == ExtractedQuestion.DetectedType.SINGLE,
            "approved": bool(exclude_approved and (question.status == ExtractedQuestion.Status.APPROVED
                                                    or question.approved_question_id)),
            "saved_confirmed": bool(question.correct_option and question.answer_status == confirmed
                and question.detected_type == ExtractedQuestion.DetectedType.SINGLE
                and keys.count(question.correct_option.strip().upper()) == 1),
        })
    return rows


def decorate_staged_question(question, *, image_first, private):
    """Attach presentation-only fields without modifying stored paper content."""
    question.primary_crops = primary_question_crops(question, image_first=image_first, private=private)
    question.image_notice = ""
    if image_first and question.is_image_first:
        if not question.primary_crops:
            question.image_notice = "Original question image is missing. Review the available text and images, or add a crop before creating the test."
        else:
            unavailable = unavailable_crop_pages(question.primary_crops, private=private)
            pages = missing_crop_pages(question, question.primary_crops)
            if unavailable:
                question.image_notice = (
                    "An original image file is unavailable on page " + ", ".join(map(str, unavailable))
                    + ". Replace it or exclude this question."
                )
            if pages:
                question.image_notice += (
                    " A possible continuation has no original question image on page "
                    + ", ".join(map(str, pages)) + ". Add the missing part or exclude this question."
                )
            question.image_notice = question.image_notice.strip()
    return question
