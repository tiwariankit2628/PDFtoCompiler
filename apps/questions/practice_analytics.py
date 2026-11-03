"""Private results: only student-confirmed, unambiguous keys are gradable."""
from collections import defaultdict
from decimal import Decimal


def confirmed_key(question, options):
    correct = [option for option in options if option.is_correct]
    return correct[0] if question.key_verified and len(correct) == 1 else None


def compute_practice_result(attempt):
    responses = list(attempt.responses.select_related("exam_question__question", "selected_option")
                     .prefetch_related("exam_question__question__options__figures", "exam_question__question__figures")
                     .order_by("order", "id"))
    rows = []
    groups = {name: defaultdict(lambda: {"total": 0, "correct": 0, "incorrect": 0,
                                        "unattempted": 0, "ungraded": 0})
              for name in ("subject", "topic", "difficulty")}
    counts = {"correct": 0, "incorrect": 0, "unattempted": 0, "ungraded": 0}
    graded_marks = Decimal("0.00")
    unkeyed = 0
    for response in responses:
        q = response.exam_question.question
        options = list(q.options.all())
        correct = confirmed_key(q, options)
        if correct:
            graded_marks += response.exam_question.positive_marks
        else:
            unkeyed += 1
        if not response.selected_option_id:
            status, label = "unattempted", "Unattempted"
        elif not correct:
            status, label = "ungraded", ("Unconfirmed answer key — ungraded" if any(o.is_correct for o in options) else "No answer key — ungraded")
        elif response.is_correct is True:
            status, label = "correct", "Correct"
        else:
            status, label = "incorrect", "Incorrect"
        counts[status] += 1
        rows.append({"response": response, "question": q, "options": options,
                     "correct": correct, "status": status, "label": label,
                     # An explanation can itself reveal an unverified answer.
                     "explanation": q.explanation if correct else ""})
        labels = {"subject": q.subject or "Not specified", "topic": q.topic or "Not specified",
                  "difficulty": q.get_difficulty_display() or "Not specified"}
        for kind, name in labels.items():
            groups[kind][name]["total"] += 1
            groups[kind][name][status] += 1
    breakdowns = {}
    for kind, collection in groups.items():
        breakdowns[kind] = []
        for name, bucket in sorted(collection.items(), key=lambda item: item[0].casefold()):
            attempted_graded = bucket["correct"] + bucket["incorrect"]
            accuracy = round(100 * bucket["correct"] / attempted_graded, 1) if attempted_graded else None
            breakdowns[kind].append({"name": name, **bucket, "accuracy": accuracy})
    attempted_keyed = counts["correct"] + counts["incorrect"]
    fully_keyed = bool(rows) and not unkeyed and graded_marks > 0
    has_target = fully_keyed and attempt.exam.passing_percentage > 0
    return {
        "rows": rows, "graded_marks": graded_marks,
        "keyed_count": len(rows) - unkeyed, "unkeyed_count": unkeyed,
        "counts": counts, "attempted_total": len(rows) - counts["unattempted"], "breakdowns": breakdowns, "fully_keyed": fully_keyed,
        "accuracy": round(100 * counts["correct"] / attempted_keyed, 2) if attempted_keyed else None,
        "percentage": attempt.percentage if graded_marks > 0 else None,
        "has_target": has_target,
        "outcome": ("Passed" if attempt.is_passed else "Below target") if has_target else "Not determined",
        "time_logged_seconds": sum(r.time_spent_seconds for r in responses),
    }
