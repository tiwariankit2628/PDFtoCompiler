"""Strict, type-aware parsing of deliberately entered answer keys.

Image recognition never substitutes for a person's confirmation. A numeric
value is an option label only for an MCQ; for a numerical item it is the value.
"""
import re
from collections import Counter
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

from .models import AnswerStatus, ExtractedQuestion


# A delimiter is required so that a bare number like 12 cannot become 1:B.
# Brackets make multi-answer lists unambiguous next to comma-separated pairs.
PAIR = re.compile(
    r"(?<![\w])(?P<number>\d+)\s*(?:[:.\-=]\s*|\s+)"
    r"(?P<value>\[[A-E1-5\s,;/|]+\]|\(\s*[A-E1-5]\s*\)|[A-E]|[+-]?(?:\d+(?:\.\d+)?|\.\d+))"
    r"(?![\w.])", re.IGNORECASE,
)
NUMERIC_KEYS = dict(zip("12345", "ABCDE"))
NUMERICAL_VALUE = re.compile(r"^[+-]?(?:\d{1,12}(?:\.\d{1,6})?|\.\d{1,6})$")


def parse_numeric_value(value):
    """Bound a finite decimal to the database precision; do not round silently."""
    text = str(value).strip()
    if not NUMERICAL_VALUE.fullmatch(text):
        raise ValidationError("Enter a number with up to 12 integer digits and 6 decimal places.")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValidationError("Enter a valid finite number.") from None
    if not number.is_finite():
        raise ValidationError("Enter a valid finite number.")
    return number


def format_numeric_value(value):
    number = parse_numeric_value(value)
    if not number:
        return "0"
    return format(number.normalize(), "f")


def canonical_answer(value, question_type, options=()):
    """Validate and serialize one key according to its actual question type."""
    raw = str("" if value is None else value).strip().upper()
    if not raw:
        return ""
    if question_type == ExtractedQuestion.DetectedType.NUMERICAL:
        # Parenthesized printed numbers are still values here, not A–E.
        return format_numeric_value(raw[1:-1].strip() if raw.startswith("(") and raw.endswith(")") else raw)
    keys = [str(option.get("key", "")).strip().upper() for option in options if isinstance(option, dict)]
    if question_type == ExtractedQuestion.DetectedType.SINGLE:
        token = raw[1:-1].strip() if raw.startswith("(") and raw.endswith(")") else raw
        if len(token) != 1 or token not in "ABCDE12345":
            raise ValidationError("A single-choice key must be one option A–E or 1–5.")
        selected = [NUMERIC_KEYS.get(token, token)]
    elif question_type == ExtractedQuestion.DetectedType.MULTIPLE:
        if not (raw.startswith("[") and raw.endswith("]")):
            raise ValidationError("Use brackets for multiple choices, for example [A,C] or [1,3].")
        token = raw[1:-1].strip()
        parts = [part for part in re.split(r"[\s,;/|]+", token) if part]
        if not parts or any(len(part) != 1 or part not in "ABCDE12345" for part in parts):
            raise ValidationError("Enter multiple choices such as [A,C] or [1,3].")
        selected = [NUMERIC_KEYS.get(part, part) for part in parts]
        if len(selected) != len(set(selected)):
            raise ValidationError("A multiple-choice answer repeats an option.")
    else:
        raise ValidationError("This question type cannot use an automatically scored key.")
    for key in selected:
        if keys.count(key) != 1:
            raise ValidationError(f"The question has no unique option {key}.")
    return selected[0] if question_type == ExtractedQuestion.DetectedType.SINGLE else "[" + ",".join(sorted(selected)) + "]"


def answer_keys_set(value):
    """Return the validated canonical multiple-choice labels without guessing."""
    text = (value or "").strip()
    if not (text.startswith("[") and text.endswith("]")):
        return []
    return [key for key in text[1:-1].split(",") if key]


def confirmed_staged_key(question, required_status):
    """Never upgrade legacy non-single values or unconfirmed suggestions."""
    if question.answer_status != required_status:
        return ""
    if question.detected_type != ExtractedQuestion.DetectedType.SINGLE and not getattr(question, "typed_key_verified", False):
        return ""
    try:
        return canonical_answer(question.correct_option, question.detected_type,
                                question.options if isinstance(question.options, list) else [])
    except ValidationError:
        return ""


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
            (question.question_text or "").strip(), question.tables or [], choices,
            getattr(question, "numeric_tolerance", 0),
            getattr(question, "multi_scoring_mode", "exact"),
            getattr(question, "partial_credit_per_choice", 0))


def parse_checked_answer_key(text, questions, *, exclude_approved=False):
    """Return validated {question_number: canonical_key}; reject the whole input.

    Duplicate/unknown question numbers, malformed or ambiguous answers, missing
    options and unsupported types are not silently ignored. Approved bank items
    cannot be changed through a staging-only import.
    """
    text = (text or "").strip()
    if not text:
        return {}
    matches = list(PAIR.finditer(text))
    remainder = PAIR.sub("", text)
    if not matches or remainder.strip(" ,;\n\r\t"):
        raise ValidationError("Use pairs like 1:A, 2:[A,C], 3:-2.5, separated by commas or new lines.")
    questions = list(questions)
    counts = Counter(q.question_number for q in questions)
    by_number = {q.question_number: q for q in questions}
    keys = {}
    for match in matches:
        number = int(match.group("number"))
        raw = match.group("value")
        if number in keys:
            raise ValidationError(f"Question {number} appears more than once in the answer key.")
        if number not in by_number or counts[number] != 1:
            raise ValidationError(f"Question {number} is missing, excluded, or ambiguous. Edit it individually instead.")
        question = by_number[number]
        if exclude_approved and (question.status == ExtractedQuestion.Status.APPROVED or question.approved_question_id):
            raise ValidationError(f"Question {number} is already in the question bank. Edit its bank answer separately.")
        if question.detected_type == ExtractedQuestion.DetectedType.MULTIPLE and not raw.startswith("["):
            raise ValidationError(f"Question {number} is multiple-correct. Use brackets, for example {number}:[1,3].")
        try:
            key = canonical_answer(raw, question.detected_type,
                                   question.options if isinstance(question.options, list) else [])
        except ValidationError as exc:
            raise ValidationError(f"Question {number}: {exc.messages[0]}") from None
        keys[number] = key
    return keys
