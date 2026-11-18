"""Results for institutional attempts with missing or unsupported keys.

Grading-time snapshots are used for guarded questions; later edits do not
change a submitted attempt. Legacy scores and decisions are retained rather
than retroactively inferred from the current key.
"""
from decimal import Decimal

from .classroom_results import classroom_response_status


def partial_pdf_result_summary(attempt):
    rows = list(attempt.responses.select_related("exam_question__question", "exam_question", "selected_option"))
    counts = {"correct": 0, "partial": 0, "incorrect": 0, "unattempted": 0, "ungraded": 0}
    pending = 0
    unknown = 0
    possible = Decimal("0.00")
    graded_questions = 0
    for row in rows:
        if row.key_confirmed_at_grading is False:
            pending += 1
        elif (row.key_confirmed_at_grading is None and
              (row.exam_question.question.is_pdf_derived or
               row.exam_question.question.question_type in ("mcq_multiple", "numerical"))):
            unknown += 1  # old attempt, never guessed from today's editable key
        else:
            possible += row.exam_question.positive_marks
            graded_questions += 1
        counts[classroom_response_status(row)] += 1
    return {
        "counts": counts, "pending_keys": pending, "unknown_keys": unknown,
        "possible_marks": None if unknown else possible,
        "score_available": bool(graded_questions or unknown),
        "percentage": attempt.percentage if possible > 0 and not unknown else None,
        "fully_keyed": pending == 0 and unknown == 0,
    }
