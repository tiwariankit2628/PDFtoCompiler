"""Classroom summaries based on saved grading decisions, not today's keys.

Older attempts have no key snapshot for unanswered questions. We report that
uncertainty instead of inferring historical facts from an editable question.
"""


def classroom_response_status(response):
    """Use the saved grading decision even if the selected option was deleted."""
    if response.key_confirmed_at_grading is False:
        return ("ungraded" if response.selected_option_id is not None
                or response.selected_option_key_at_grading is not None
                or response.is_correct is not None else "unattempted")
    if response.is_correct is True:
        return "correct"
    if response.is_correct is False:
        return "incorrect"
    if response.selected_option_id is not None or response.selected_option_key_at_grading is not None:
        return "ungraded"
    return "unattempted"


def classroom_result_summary(attempt):
    # ExamResponse's default ordering is stable, and .all() honors a caller's
    # prefetch cache when exporting a large classroom rather than issuing a
    # separate query per participant.
    responses = list(attempt.responses.all())
    counts = {"correct": 0, "incorrect": 0, "unattempted": 0, "ungraded": 0}
    pending_keys = 0
    unknown_keys = 0
    has_confirmed_keys = False

    for response in responses:
        # The option may have since been deleted, in which case a new attempt's
        # saved choice label and its saved grading decision still survive.
        status = classroom_response_status(response)
        counts[status] += 1
        answered = status != "unattempted"

        if response.key_confirmed_at_grading is True:
            has_confirmed_keys = True
        elif response.key_confirmed_at_grading is False:
            pending_keys += 1
        elif response.is_correct is not None:
            # For an older classroom attempt a saved correct/incorrect result
            # proves this response was graded, but not which option was the key.
            has_confirmed_keys = True
        elif answered:
            # Confirmed-key grading deliberately leaves these responses ungraded.
            pending_keys += 1
        else:
            # No saved decision exists for an unanswered legacy response.
            unknown_keys += 1

    fully_keyed = bool(responses) and not pending_keys and not unknown_keys
    return {
        "total": len(responses),
        "counts": counts,
        "pending_keys": pending_keys,
        "unknown_keys": unknown_keys,
        "has_graded_answers": bool(counts["correct"] + counts["incorrect"]),
        "score_available": has_confirmed_keys,
        "fully_keyed": fully_keyed,
        # This persisted value was computed from confirmed keys at submission;
        # it may be a partial percentage and does not prove a complete result.
        "percentage": attempt.percentage if has_confirmed_keys else None,
    }
