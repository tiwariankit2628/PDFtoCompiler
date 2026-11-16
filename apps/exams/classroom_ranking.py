"""One consistent best-attempt policy for classroom monitoring and reports."""
from .classroom_results import classroom_result_summary
from .models import ExamAttempt

# Security-terminated attempts are available for teacher review but are not
# ranked. They are not silently mixed with ordinary completed attempts.
RANKABLE_STATUSES = (ExamAttempt.AttemptStatus.SUBMITTED, ExamAttempt.AttemptStatus.EVALUATED,
                     ExamAttempt.AttemptStatus.TIMED_OUT)


def _performance(attempt):
    return (attempt.score_obtained, -attempt.incorrect_count, -attempt.time_taken_seconds)


def classroom_attempts_by_student(session, student_ids=None):
    """Latest participation state plus the best completed result per student.

    Prefer a fully keyed completed attempt; otherwise use the best partial one,
    then the latest entirely ungraded/terminated attempt. This prevents a
    partial score from outranking a complete result. Ties prefer fewer wrong,
    faster completion, and then the earlier attempt for deterministic retakes.
    """
    queryset = session.attempts.select_related("student").prefetch_related("responses")
    if student_ids is not None:
        queryset = queryset.filter(student_id__in=student_ids)
    rows = {}
    for attempt in queryset.order_by("student_id", "-started_at", "-id"):
        row = rows.setdefault(attempt.student_id, {"latest": attempt, "best": None, "summary": None, "quality": -1})
        if attempt.status not in ExamAttempt.FINAL_STATUSES:
            continue
        summary = classroom_result_summary(attempt)
        if attempt.status not in RANKABLE_STATUSES:
            quality = 0  # retained for review, never ranked
        elif summary["fully_keyed"] and summary["score_available"]:
            quality = 3
        elif summary["score_available"]:
            quality = 2
        else:
            quality = 1
        old = row["best"]
        if (quality > row["quality"] or
                (quality == row["quality"] and quality >= 2 and old is not None
                 and (_performance(attempt), -attempt.id) > (_performance(old), -old.id))):
            row.update(best=attempt, summary=summary, quality=quality)
    return rows


def classroom_ranked_results(rows):
    """Competition ranks over best fully keyed, eligible completed attempts."""
    eligible = [row for row in rows.values() if row["quality"] == 3]
    eligible.sort(key=lambda row: (-row["best"].score_obtained, row["best"].incorrect_count,
                                   row["best"].time_taken_seconds, row["best"].student.username.casefold(),
                                   row["best"].student_id))
    prev, rank = None, 0
    for position, row in enumerate(eligible, 1):
        attempt = row["best"]
        key = _performance(attempt)
        if key != prev:
            rank, prev = position, key
        row["rank"] = rank
    return eligible
