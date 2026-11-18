"""Strict parsing of deliberately entered, single-correct answer keys.

This validates *human-entered* values. Image recognition itself never calls
this as a substitute for human confirmation.
"""
import re
from collections import Counter

from django.core.exceptions import ValidationError

from .models import AnswerStatus, ExtractedQuestion


# A delimiter is required so that a bare number like 12 cannot become 1:B.
PAIR = re.compile(
    r"(?<![\w])(?P<number>\d+)\s*(?:[:.\-=]\s*|\s+)"
    r"(?:\(\s*(?P<parenthesized>[A-E1-5])\s*\)|(?P<plain>[A-E1-5]))(?![\w])",
    re.IGNORECASE,
)
NUMERIC_KEYS = dict(zip("12345", "ABCDE"))


def edited_answer_status(previous_key, previous_status, selected_key, checked, confirmed_status, *, content_changed=False):
    """Keep a confirmation only while the answer and the question still match."""
    selected_key = (selected_key or "").strip().upper()
    previous_key = (previous_key or "").strip().upper()
    if not selected_key:
        return AnswerStatus.NEEDS_REVIEW
    if checked:
        return confirmed_status
    if selected_key == previous_key and not content_changed:
        return previous_status
    return AnswerStatus.NEEDS_REVIEW


def staged_question_signature(question):
    """Identify changes that may invalidate a checked paper/choice mapping.

    Ignore cosmetic metadata such as marks and review flags. Retain option
    order and crop-only labels: changing either can change the printed answer.
    """
    options = question.options if isinstance(question.options, list) else []
    choices = tuple(
        (str(option.get("key", "")).strip().upper(),
         str(option.get("text", "")).strip(), bool(option.get("visible_in_crop")))
        if isinstance(option, dict) else ("<invalid>", "", False)
        for option in options
    )
    return (question.question_number, question.detected_type,
            (question.question_text or "").strip(), question.tables or [], choices)


def parse_checked_answer_key(text, questions, *, exclude_approved=False):
    """Return validated {question_number: option_key}; reject the whole input.

    Duplicate/unknown question numbers, malformed or multiple answers, missing
    options and unsupported question types are not silently ignored. Previously
    approved bank questions cannot be changed through a staging-only import.
    """
    text = (text or "").strip()
    if not text:
        return {}
    matches = list(PAIR.finditer(text))
    remainder = PAIR.sub("", text)
    if not matches or remainder.strip(" ,;\n\r\t"):
        raise ValidationError("Use single-answer pairs like 1: A, 2: B, separated by commas or new lines.")
    questions = list(questions)
    counts = Counter(q.question_number for q in questions)
    by_number = {q.question_number: q for q in questions}
    keys = {}
    for match in matches:
        number = int(match.group("number"))
        raw = (match.group("plain") or match.group("parenthesized")).upper()
        key = NUMERIC_KEYS.get(raw, raw)
        if number in keys:
            raise ValidationError(f"Question {number} appears more than once in the answer key.")
        if number not in by_number or counts[number] != 1:
            raise ValidationError(f"Question {number} is missing, excluded, or ambiguous. Edit it individually instead.")
        question = by_number[number]
        if question.detected_type != ExtractedQuestion.DetectedType.SINGLE:
            raise ValidationError(f"Question {number} is not marked as a single-correct MCQ. Review its type first.")
        if exclude_approved and (question.status == ExtractedQuestion.Status.APPROVED or question.approved_question_id):
            raise ValidationError(f"Question {number} is already in the question bank. Edit its bank answer separately.")
        options = question.options if isinstance(question.options, list) else []
        valid_keys = [str(option.get("key", "")).upper().strip() for option in options if isinstance(option, dict)]
        if key not in valid_keys or valid_keys.count(key) != 1:
            raise ValidationError(f"Question {number} has no unique option {key}.")
        keys[number] = key
    return keys
