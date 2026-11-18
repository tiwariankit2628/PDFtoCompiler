"""Validation and grading for explicitly confirmed multi/numerical answers.

Never reinterpret an old single-option FK as a multiple or numerical answer.
Submission snapshots preserve the actual response, checked key and rules used;
no OCR suggestion or historical option flag automatically becomes a key.
"""
from decimal import Decimal
from types import SimpleNamespace

from django.core.exceptions import ValidationError

from apps.questions.answer_key_validation import format_numeric_value, parse_numeric_value
from apps.questions.models import Question


def apply_typed_response(response, data, *, clear=False):
    """Apply a validated, question-scoped typed response from a client payload.

    Missing fields do not erase a previously saved answer. An explicit empty
    list/string clears it. Call this inside the existing answer/attempt lock.
    """
    question = response.exam_question.question
    if question.question_type == Question.QuestionType.MCQ_MULTIPLE:
        if clear:
            ids = []
        elif "selected_option_ids" not in data:
            return
        else:
            ids = data["selected_option_ids"]
            if not isinstance(ids, list) or len(ids) > 20:
                raise ValidationError("Invalid multiple-choice selection.")
            # JSON booleans are Python ints; explicitly reject them.
            if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in ids):
                raise ValidationError("Invalid multiple-choice selection.")
            if len(set(ids)) != len(ids):
                raise ValidationError("A selected choice is repeated.")
            valid = set(question.options.filter(pk__in=ids).values_list("pk", flat=True))
            if valid != set(ids):
                raise ValidationError("A selected choice does not belong to this question.")
            if not ids and response.selected_option_id is not None:
                # Navigation alone must not erase a legacy single-choice answer.
                return
        response.selected_option_ids = sorted(ids)
        response.numerical_response = None
        response.selected_option = None
    elif question.question_type == Question.QuestionType.NUMERICAL:
        if clear:
            value = None
        elif "numerical_response" not in data:
            return
        else:
            raw = data["numerical_response"]
            if raw is None or raw == "":
                value = None
            elif isinstance(raw, (str, int)) and not isinstance(raw, bool) and len(str(raw)) <= 22:
                value = parse_numeric_value(raw)
            else:
                raise ValidationError("Enter a valid numerical answer.")
            if value is None and response.selected_option_id is not None:
                return
        response.numerical_response = value
        response.selected_option_ids = []
        response.selected_option = None


def grade_typed_response(response, question, options, key_confirmed):
    """Return a fail-closed, JSON-serializable decision for a typed question.

    Exact set gets full marks. In per-correct mode a proper subset gets the
    configured amount for each selected correct option, capped at full marks;
    any wrong option gets the configured penalty. Numerical answers compare
    using Decimal with the configured non-negative tolerance.
    """
    kind = question.question_type
    positive = response.exam_question.positive_marks
    negative = response.exam_question.negative_marks
    option_by_id = {option.pk: option for option in options}
    labels = [option.option_key for option in options]
    correct = [option.option_key for option in options if option.is_correct]
    rules = {"positive": str(positive), "negative": str(negative)}
    valid = bool(key_confirmed and question.typed_key_verified and positive >= 0 and negative >= 0)
    invalid_answer = False
    if kind == Question.QuestionType.MCQ_MULTIPLE:
        ids = response.selected_option_ids
        valid_ids = (isinstance(ids, list) and len(ids) <= 20
                     and all(isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in ids))
        valid_ids = valid_ids and len(ids) == len(set(ids)) and all(i in option_by_id for i in ids)
        invalid_answer = not valid_ids or response.selected_option_id is not None
        answer = sorted(option_by_id[i].option_key for i in ids) if valid_ids else []
        if invalid_answer and response.selected_option_id is not None and not answer:
            # It was saved before the type was corrected. Do not promote it to a
            # typed selection or pretend the response was unanswered.
            answer = None
        mode = question.multi_scoring_mode
        partial = question.partial_credit_per_choice
        valid = (valid and bool(correct) and len(labels) >= 2 and len(labels) == len(set(labels))
                 and all(isinstance(k, str) and len(k) == 1 and k in "ABCDE" for k in labels)
                 and mode in ("exact", "per_correct")
                 and (mode != "per_correct" or Decimal("0") < partial <= positive))
        key = sorted(correct) if valid else None
        rules.update(mode=mode, partial_per_choice=str(partial))
        attempted = bool(ids) if valid_ids else True
        attempted = attempted or invalid_answer
        if invalid_answer:
            valid = False
            key = None
        if not attempted:
            outcome, marks = "unattempted", Decimal("0")
        elif not valid:
            outcome, marks = "ungraded", Decimal("0")
        elif set(answer) == set(key):
            outcome, marks = "correct", positive
        elif mode == "per_correct" and set(answer).issubset(set(key)):
            outcome, marks = "partial", min(positive, partial * len(answer))
        else:
            outcome, marks = "incorrect", -negative
    elif kind == Question.QuestionType.NUMERICAL:
        value = response.numerical_response
        attempted = value is not None or response.selected_option_id is not None or bool(response.selected_option_ids)
        invalid_answer = response.selected_option_id is not None or bool(response.selected_option_ids)
        answer = format_numeric_value(value) if value is not None else None
        tolerance = question.numeric_tolerance
        valid = (valid and question.numeric_key is not None and tolerance is not None
                 and tolerance.is_finite() and tolerance >= 0 and question.numeric_key.is_finite())
        key = format_numeric_value(question.numeric_key) if valid else None
        rules.update(tolerance=str(tolerance))
        if invalid_answer:
            valid = False
            key = None
        if not attempted:
            outcome, marks = "unattempted", Decimal("0")
        elif not valid:
            outcome, marks = "ungraded", Decimal("0")
        elif abs(value - question.numeric_key) <= tolerance:
            outcome, marks = "correct", positive
        else:
            outcome, marks = "incorrect", -negative
    else:
        raise ValueError("Not a supported typed question.")
    return {
        "version": 1, "type": kind, "answer": answer, "key": key,
        "outcome": outcome, "marks": str(marks), "rules": rules,
        "key_confirmed": bool(valid),
        # Preserve the solution paired with the checked key, not a later edit.
        # Never snapshot an explanation as a confirmed solution for an
        # ungraded or ambiguous legacy response.
        "explanation": question.explanation if valid else "",
    }


def typed_key_is_gradeable(question, options, *, positive, negative, key_confirmed):
    """Use the grading rules themselves for owner-facing readiness indicators.

    Exam-specific mark overrides can make an otherwise confirmed partial-credit
    rule invalid, so the builder must check against the assigned marks.
    """
    probe = SimpleNamespace(exam_question=SimpleNamespace(positive_marks=positive, negative_marks=negative),
                            selected_option_id=None, selected_option_ids=[], numerical_response=None)
    return grade_typed_response(probe, question, options, key_confirmed)["key_confirmed"]


def typed_snapshot_review(response):
    """Presentation of a recorded decision, independent of today's bank key."""
    data = response.typed_grading_snapshot
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    answer = data.get("answer")
    key = data.get("key") if response.key_confirmed_at_grading is True else None
    answer_labels = answer if isinstance(answer, list) else []
    key_labels = key if isinstance(key, list) else []

    def display(value):
        if isinstance(value, list):
            return ", ".join(str(label) for label in value) if value else None
        return str(value) if value is not None else None

    return {"answer": display(answer), "key": display(key),
            "answer_labels": answer_labels, "key_labels": key_labels,
            "rules": data.get("rules") or {}, "type": data.get("type"),
            "explanation": data.get("explanation", "") if key is not None else ""}
