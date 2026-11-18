"""Private results use saved grading decisions, not today's editable keys."""
from collections import defaultdict
from decimal import Decimal

from apps.exams.classroom_results import classroom_response_status
from apps.exams.typed_responses import typed_snapshot_review
from .models import Question
from .presentation import missing_crop_pages, primary_question_crops


def confirmed_key(question, options):
    correct = [option for option in options if option.is_correct]
    return (correct[0] if question.question_type == Question.QuestionType.MCQ_SINGLE
            and question.key_verified and len(correct) == 1 else None)


def compute_practice_result(attempt):
    responses = list(attempt.responses.select_related("exam_question__question", "selected_option")
                     .prefetch_related("exam_question__question__options__figures", "exam_question__question__figures")
                     .order_by("order", "id"))
    rows = []
    groups = {name: defaultdict(lambda: {"total": 0, "correct": 0, "partial": 0, "incorrect": 0,
                                        "unattempted": 0, "ungraded": 0})
              for name in ("subject", "topic", "difficulty")}
    counts = {"correct": 0, "partial": 0, "incorrect": 0, "unattempted": 0, "ungraded": 0}
    graded_marks = Decimal("0.00")
    keyed = unkeyed = legacy_unknown = 0
    for response in responses:
        q = response.exam_question.question
        options = list(q.options.all())
        saved_key = None
        correct = None
        if response.key_confirmed_at_grading is True:
            keyed += 1
            graded_marks += response.exam_question.positive_marks
            saved_key = response.correct_option_key_at_grading
            matching = [o for o in options if o.option_key == saved_key] if saved_key is not None else []
            correct = matching[0] if len(matching) == 1 else None
        elif response.key_confirmed_at_grading is False:
            unkeyed += 1
        else:
            # Older practice attempts have no key snapshot. Preserve their
            # recorded decisions and score, but do not infer the original key,
            # denominator, or outcome from today's question or answer flags.
            legacy_unknown += 1

        status = classroom_response_status(response)
        label = {"correct": "Correct", "partial": "Partial credit", "incorrect": "Incorrect", "unattempted": "Unattempted",
                 "ungraded": "No answer key — ungraded"}[status]
        if status == "ungraded" and response.key_confirmed_at_grading is None:
            label = "Grading decision not recorded — ungraded"
        counts[status] += 1
        crops = primary_question_crops(q, image_first=attempt.exam.image_first_mode, private=True)
        incomplete_pages = missing_crop_pages(q, crops) if crops else []
        current_key = confirmed_key(q, options)
        explanation = q.explanation if (saved_key is not None and current_key
                                         and current_key.option_key == saved_key) else ""
        typed = typed_snapshot_review(response)
        if typed:
            saved_key = typed["key"]
            explanation = typed["explanation"]
        rows.append({"response": response, "question": q, "options": options,
                     "primary_crops": crops, "missing_crop_pages": incomplete_pages,
                     "original_image_missing": bool(attempt.exam.image_first_mode and q.is_image_first and not crops),
                     "correct": correct, "saved_key": saved_key,
                     "saved_choice": typed["answer"] if typed else response.selected_option_key_at_grading,
                     "typed": typed,
                     "legacy_key_unknown": response.key_confirmed_at_grading is None,
                     "status": status, "label": label, "explanation": explanation})
        labels = {"subject": q.subject or "Not specified", "topic": q.topic or "Not specified",
                  "difficulty": q.get_difficulty_display() or "Not specified"}
        for kind, name in labels.items():
            groups[kind][name]["total"] += 1
            groups[kind][name][status] += 1
    breakdowns = {}
    for kind, collection in groups.items():
        breakdowns[kind] = []
        for name, bucket in sorted(collection.items(), key=lambda item: item[0].casefold()):
            attempted_graded = bucket["correct"] + bucket["partial"] + bucket["incorrect"]
            accuracy = round(100 * bucket["correct"] / attempted_graded, 1) if attempted_graded else None
            breakdowns[kind].append({"name": name, **bucket, "accuracy": accuracy})
    attempted_keyed = counts["correct"] + counts["partial"] + counts["incorrect"]
    fully_keyed = bool(rows) and not unkeyed and not legacy_unknown and graded_marks > 0
    has_target = fully_keyed and attempt.exam.passing_percentage > 0
    return {
        "rows": rows, "graded_marks": graded_marks if not legacy_unknown else None,
        "keyed_count": keyed, "unkeyed_count": unkeyed, "legacy_unknown_count": legacy_unknown,
        "counts": counts, "attempted_total": len(rows) - counts["unattempted"], "breakdowns": breakdowns,
        "fully_keyed": fully_keyed,
        "accuracy": round(100 * counts["correct"] / attempted_keyed, 2) if attempted_keyed else None,
        "percentage": attempt.percentage if graded_marks > 0 and not legacy_unknown else None,
        "has_target": has_target,
        "outcome": ("Passed" if attempt.is_passed else "Below target") if has_target else "Not determined",
        "time_logged_seconds": sum(r.time_spent_seconds for r in responses),
    }
