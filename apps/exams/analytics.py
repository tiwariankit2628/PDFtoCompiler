"""
Comprehensive CBT Result Evaluation & Performance Analytics Service.
Computes deep diagnostic metrics from real candidate responses:
- Subject, Topic, Chapter, and Difficulty-wise accuracy and scoring
- Time management efficiency per question and pacing analysis
- Strengths and Weaknesses profiling
- Institutional cohort statistics, question discrimination, and leaderboards
"""
from collections import defaultdict
from decimal import Decimal
from django.db.models import Avg, Count, Max, Min, Q
from django.utils import timezone
from .models import ExamResponse


def compute_student_attempt_analytics(attempt):
    """
    Computes detailed diagnostic performance analytics for a single student attempt.
    All figures are derived from actual ExamResponse records.
    """
    responses = list(
        attempt.responses.select_related(
            "exam_question__question",
            "selected_option",
            "exam_question",
        ).all()
    )

    total_questions = len(responses)
    correct_count = 0
    incorrect_count = 0
    unattempted_count = 0
    total_time_spent = 0
    time_spent_correct = 0
    time_spent_incorrect = 0
    time_spent_unattempted = 0

    # Categorized buckets
    subject_data = defaultdict(lambda: {
        "total": 0, "attempted": 0, "correct": 0, "incorrect": 0,
        "marks_obtained": Decimal("0.00"), "max_marks": Decimal("0.00"),
        "time_spent": 0,
    })
    topic_data = defaultdict(lambda: {
        "subject": "", "total": 0, "attempted": 0, "correct": 0, "incorrect": 0,
        "marks_obtained": Decimal("0.00"), "time_spent": 0,
    })
    chapter_data = defaultdict(lambda: {
        "subject": "", "total": 0, "attempted": 0, "correct": 0, "incorrect": 0,
        "marks_obtained": Decimal("0.00"),
    })
    difficulty_data = {
        "easy": {"total": 0, "correct": 0, "incorrect": 0, "unattempted": 0, "time_spent": 0},
        "medium": {"total": 0, "correct": 0, "incorrect": 0, "unattempted": 0, "time_spent": 0},
        "hard": {"total": 0, "correct": 0, "incorrect": 0, "unattempted": 0, "time_spent": 0},
    }

    fastest_time = None
    slowest_time = 0

    for r in responses:
        q = r.exam_question.question
        eq = r.exam_question
        time_spent = r.time_spent_seconds
        total_time_spent += time_spent

        diff_key = q.difficulty.lower() if q.difficulty else "medium"
        if diff_key not in difficulty_data:
            diff_key = "medium"

        difficulty_data[diff_key]["total"] += 1
        difficulty_data[diff_key]["time_spent"] += time_spent

        subj = q.subject or "General"
        subject_data[subj]["total"] += 1
        subject_data[subj]["max_marks"] += eq.positive_marks
        subject_data[subj]["time_spent"] += time_spent

        top = q.topic or "General"
        topic_data[top]["subject"] = subj
        topic_data[top]["total"] += 1
        topic_data[top]["time_spent"] += time_spent

        chap = q.chapter or "General"
        chapter_data[chap]["subject"] = subj
        chapter_data[chap]["total"] += 1

        if r.selected_option is not None:
            # Question was attempted
            subject_data[subj]["attempted"] += 1
            topic_data[top]["attempted"] += 1
            chapter_data[chap]["attempted"] += 1

            if fastest_time is None or time_spent < fastest_time:
                fastest_time = time_spent
            if time_spent > slowest_time:
                slowest_time = time_spent

            if r.selected_option.is_correct:
                correct_count += 1
                time_spent_correct += time_spent
                difficulty_data[diff_key]["correct"] += 1

                subject_data[subj]["correct"] += 1
                subject_data[subj]["marks_obtained"] += eq.positive_marks

                topic_data[top]["correct"] += 1
                topic_data[top]["marks_obtained"] += eq.positive_marks

                chapter_data[chap]["correct"] += 1
                chapter_data[chap]["marks_obtained"] += eq.positive_marks
            else:
                incorrect_count += 1
                time_spent_incorrect += time_spent
                difficulty_data[diff_key]["incorrect"] += 1

                subject_data[subj]["incorrect"] += 1
                subject_data[subj]["marks_obtained"] -= eq.negative_marks

                topic_data[top]["incorrect"] += 1
                topic_data[top]["marks_obtained"] -= eq.negative_marks

                chapter_data[chap]["incorrect"] += 1
                chapter_data[chap]["marks_obtained"] -= eq.negative_marks
        else:
            unattempted_count += 1
            time_spent_unattempted += time_spent
            difficulty_data[diff_key]["unattempted"] += 1

    # Finalize Subject Statistics
    subject_summary = []
    for s_name, s_info in subject_data.items():
        att_cnt = s_info["attempted"]
        acc = round((s_info["correct"] / att_cnt * 100.0), 1) if att_cnt > 0 else 0.0
        pct_score = round((float(s_info["marks_obtained"]) / float(s_info["max_marks"]) * 100.0), 1) if s_info["max_marks"] > 0 else 0.0
        subject_summary.append({
            "subject": s_name,
            "total": s_info["total"],
            "attempted": att_cnt,
            "correct": s_info["correct"],
            "incorrect": s_info["incorrect"],
            "unattempted": s_info["total"] - att_cnt,
            "marks_obtained": s_info["marks_obtained"],
            "max_marks": s_info["max_marks"],
            "percentage": pct_score,
            "accuracy": acc,
            "time_spent_minutes": round(s_info["time_spent"] / 60, 1),
        })

    # Finalize Topic Statistics & Strengths / Weaknesses
    topic_summary = []
    strong_topics = []
    weak_topics = []

    for t_name, t_info in topic_data.items():
        att_cnt = t_info["attempted"]
        acc = round((t_info["correct"] / att_cnt * 100.0), 1) if att_cnt > 0 else 0.0
        topic_entry = {
            "topic": t_name,
            "subject": t_info["subject"],
            "total": t_info["total"],
            "attempted": att_cnt,
            "correct": t_info["correct"],
            "incorrect": t_info["incorrect"],
            "marks_obtained": t_info["marks_obtained"],
            "accuracy": acc,
        }
        topic_summary.append(topic_entry)
        if att_cnt > 0:
            if acc >= 70.0:
                strong_topics.append({"topic": t_name, "accuracy": acc, "correct": t_info["correct"], "total": t_info["total"]})
            elif acc < 50.0:
                weak_topics.append({"topic": t_name, "accuracy": acc, "incorrect": t_info["incorrect"], "total": t_info["total"]})

    # Sort strengths and weaknesses
    strong_topics.sort(key=lambda x: x["accuracy"], reverse=True)
    weak_topics.sort(key=lambda x: x["accuracy"])

    # Difficulty Analysis
    difficulty_summary = []
    for d_name in ["easy", "medium", "hard"]:
        d_info = difficulty_data[d_name]
        d_tot = d_info["total"]
        d_att = d_info["correct"] + d_info["incorrect"]
        d_acc = round((d_info["correct"] / d_att * 100.0), 1) if d_att > 0 else 0.0
        avg_t = round(d_info["time_spent"] / d_tot, 1) if d_tot > 0 else 0.0
        difficulty_summary.append({
            "level": d_name.capitalize(),
            "total": d_tot,
            "attempted": d_att,
            "correct": d_info["correct"],
            "incorrect": d_info["incorrect"],
            "unattempted": d_info["unattempted"],
            "accuracy": d_acc,
            "avg_time_seconds": avg_t,
        })

    # Timing Averages
    avg_time_correct = round(time_spent_correct / correct_count, 1) if correct_count > 0 else 0.0
    avg_time_incorrect = round(time_spent_incorrect / incorrect_count, 1) if incorrect_count > 0 else 0.0
    avg_time_overall = round(total_time_spent / total_questions, 1) if total_questions > 0 else 0.0

    # Total cohort size for percentile and rank context
    exam = attempt.exam
    submitted_attempts = exam.attempts.filter(
        status__in=[
            attempt.AttemptStatus.SUBMITTED,
            attempt.AttemptStatus.EVALUATED,
            attempt.AttemptStatus.TIMED_OUT,
        ]
    )
    total_cohort = submitted_attempts.count()

    return {
        "total_questions": total_questions,
        "correct_count": correct_count,
        "incorrect_count": incorrect_count,
        "unattempted_count": unattempted_count,
        "attempted_count": correct_count + incorrect_count,
        "accuracy_percentage": attempt.accuracy_percentage,
        "score_obtained": attempt.score_obtained,
        "total_marks": attempt.total_marks,
        "percentage": attempt.percentage,
        "is_passed": attempt.is_passed,
        "total_time_seconds": attempt.time_taken_seconds,
        "total_time_minutes": round(attempt.time_taken_seconds / 60, 1),
        "rank": attempt.rank,
        "percentile": attempt.percentile,
        "total_cohort": total_cohort,
        "subject_summary": subject_summary,
        "topic_summary": topic_summary,
        "strong_topics": strong_topics[:5],
        "weak_topics": weak_topics[:5],
        "difficulty_summary": difficulty_summary,
        "time_analysis": {
            "avg_time_correct": avg_time_correct,
            "avg_time_incorrect": avg_time_incorrect,
            "avg_time_overall": avg_time_overall,
            "fastest_time": fastest_time or 0,
            "slowest_time": slowest_time,
        },
    }


def compute_exam_institutional_analytics(exam):
    """
    Computes institutional performance analytics for teachers and administrators:
    - Attempt volume and completion breakdown
    - Average, highest, lowest, and median scores
    - Score distribution histograms
    - Question-wise discrimination & difficulty index
    - Ranked leaderboard of students
    """
    attempts_qs = exam.attempts.filter(
        status__in=[
            "submitted", "evaluated", "timed_out"
        ]
    ).select_related("student")

    total_attempts = attempts_qs.count()
    if total_attempts == 0:
        return {
            "total_attempts": 0,
            "completed_attempts": 0,
            "pass_count": 0,
            "pass_percentage": 0.0,
            "avg_score": 0.0,
            "highest_score": 0.0,
            "lowest_score": 0.0,
            "score_distribution": {"<30%": 0, "30-50%": 0, "50-70%": 0, "70-90%": 0, ">90%": 0},
            "question_stats": [],
            "leaderboard": [],
        }

    agg = attempts_qs.aggregate(
        avg_score=Avg("score_obtained"),
        high_score=Max("score_obtained"),
        low_score=Min("score_obtained"),
        avg_pct=Avg("percentage"),
    )

    pass_count = attempts_qs.filter(is_passed=True).count()
    pass_pct = round((pass_count / total_attempts * 100.0), 1)

    # Score distribution histogram bins
    dist = {"<30%": 0, "30-50%": 0, "50-70%": 0, "70-90%": 0, ">90%": 0}
    percentages = exam.attempts.filter(
        status__in=["submitted", "evaluated", "timed_out"]
    ).values_list("percentage", flat=True)
    for pct_val in percentages:
        pct = float(pct_val)
        if pct < 30.0:
            dist["<30%"] += 1
        elif pct < 50.0:
            dist["30-50%"] += 1
        elif pct < 70.0:
            dist["50-70%"] += 1
        elif pct < 90.0:
            dist["70-90%"] += 1
        else:
            dist[">90%"] += 1

    # Question Difficulty & Discrimination Analysis
    exam_questions = exam.exam_questions.select_related("question").prefetch_related("question__options").order_by("order")
    question_stats = []

    for eq in exam_questions:
        q = eq.question
        # All candidate responses for this specific exam question across submitted attempts
        responses = ExamResponse.objects.filter(
            exam_question=eq,
            attempt__status__in=["submitted", "evaluated", "timed_out"],
        ).select_related("selected_option")

        q_total = responses.count()
        q_correct = responses.filter(selected_option__is_correct=True).count()
        q_attempted = responses.filter(selected_option__isnull=False).count()

        accuracy = round((q_correct / q_total * 100.0), 1) if q_total > 0 else 0.0

        # Classification
        if accuracy >= 70.0:
            rating = "Easy"
            badge_class = "success"
        elif accuracy >= 40.0:
            rating = "Moderate"
            badge_class = "warning"
        else:
            rating = "Challenging"
            badge_class = "danger"

        # Most common distractor (incorrect option chosen most often)
        distractor = None
        incorrect_responses = responses.filter(selected_option__isnull=False, selected_option__is_correct=False)
        if incorrect_responses.exists():
            top_opt = incorrect_responses.values("selected_option__option_key").annotate(
                count=Count("id")
            ).order_by("-count").first()
            if top_opt:
                distractor = f"Option {top_opt['selected_option__option_key']} ({top_opt['count']} students)"

        avg_time = responses.aggregate(Avg("time_spent_seconds"))["time_spent_seconds__avg"] or 0

        question_stats.append({
            "order": eq.order,
            "section": eq.section,
            "question_text": q.question_text[:85] + ("..." if len(q.question_text) > 85 else ""),
            "difficulty": q.difficulty.capitalize() if q.difficulty else "Medium",
            "rating": rating,
            "badge_class": badge_class,
            "accuracy": accuracy,
            "attempted_count": q_attempted,
            "correct_count": q_correct,
            "distractor": distractor or "None",
            "avg_time_seconds": round(avg_time, 1),
        })

    # Leaderboard (Top 25 candidates)
    leaderboard = list(
        attempts_qs.order_by("rank", "-score_obtained", "time_taken_seconds")[:25]
    )

    return {
        "total_attempts": total_attempts,
        "completed_attempts": total_attempts,
        "pass_count": pass_count,
        "pass_percentage": pass_pct,
        "avg_score": round(agg["avg_score"] or Decimal("0.0"), 2),
        "highest_score": agg["high_score"] or Decimal("0.0"),
        "lowest_score": agg["low_score"] or Decimal("0.0"),
        "avg_percentage": round(agg["avg_pct"] or Decimal("0.0"), 1),
        "score_distribution": dist,
        "question_stats": question_stats,
        "leaderboard": leaderboard,
    }

