"""
Official CBT Candidate Scorecard PDF Generator.
Generates publication-quality, printable vector PDF scorecards using PyMuPDF (fitz).
Includes candidate identification, score summary, pass/fail result, rank, percentile,
and subject-wise performance diagnostics.
"""
import io
from decimal import Decimal
import pymupdf as fitz
from django.utils import timezone


def generate_student_scorecard_pdf(attempt, analytics):
    """
    Builds an official candidate evaluation scorecard PDF document.
    Returns bytes of the compiled PDF.
    """
    exam = attempt.exam
    student = attempt.student

    # Create A4 Document (595 x 842 points)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    # Palette Colors
    PRIMARY_COLOR = fitz.pdfcolor["blue"]  # (0, 0, 1) or custom RGB
    HEADER_BG = (0.05, 0.25, 0.55)        # Deep CBT Navy
    ACCENT_COLOR = (0.1, 0.45, 0.85)
    LIGHT_GRAY = (0.95, 0.96, 0.98)
    BORDER_COLOR = (0.80, 0.84, 0.90)
    TEXT_DARK = (0.1, 0.15, 0.2)
    TEXT_MUTED = (0.45, 0.50, 0.55)
    PASS_GREEN = (0.1, 0.6, 0.25)
    FAIL_RED = (0.85, 0.2, 0.2)

    # 1. Top Header Banner
    header_rect = fitz.Rect(0, 0, 595, 75)
    page.draw_rect(header_rect, color=None, fill=HEADER_BG)

    page.insert_text(
        fitz.Point(35, 34),
        "TESTFORGE CBT EXAMINATION SYSTEM",
        fontsize=16,
        fontname="helv",
        color=(1, 1, 1),
    )
    page.insert_text(
        fitz.Point(35, 54),
        "OFFICIAL CANDIDATE SCORECARD & PERFORMANCE DIAGNOSTIC REPORT",
        fontsize=9,
        fontname="helv",
        color=(0.85, 0.90, 0.98),
    )

    # Watermark / Tag
    page.insert_text(
        fitz.Point(440, 44),
        "VERIFIED RESULT",
        fontsize=10,
        fontname="helv",
        color=(1, 1, 1),
    )

    # 2. Candidate & Exam Meta Block (Boxed)
    meta_rect = fitz.Rect(35, 95, 560, 205)
    page.draw_rect(meta_rect, color=BORDER_COLOR, fill=LIGHT_GRAY, width=1)

    # Left Column: Candidate Info
    page.insert_text(fitz.Point(50, 118), "CANDIDATE DETAILS", fontsize=10, fontname="helv", color=HEADER_BG)
    page.insert_text(fitz.Point(50, 138), f"Name: {student.get_full_name() or student.username}", fontsize=9, fontname="helv", color=TEXT_DARK)
    page.insert_text(fitz.Point(50, 155), f"Email: {student.email}", fontsize=9, fontname="helv", color=TEXT_DARK)
    page.insert_text(fitz.Point(50, 172), f"Target Exam: {student.target_exam or exam.target_exam or 'General'}", fontsize=9, fontname="helv", color=TEXT_DARK)
    page.insert_text(fitz.Point(50, 189), f"Institute: {student.institute_name or 'N/A'}", fontsize=9, fontname="helv", color=TEXT_DARK)

    # Right Column: Examination Info
    page.insert_text(fitz.Point(310, 118), "EXAMINATION DETAILS", fontsize=10, fontname="helv", color=HEADER_BG)
    page.insert_text(fitz.Point(310, 138), f"Title: {exam.title[:38]}", fontsize=9, fontname="helv", color=TEXT_DARK)
    page.insert_text(fitz.Point(310, 155), f"Category: {exam.category.name}", fontsize=9, fontname="helv", color=TEXT_DARK)
    completed_time_str = attempt.submitted_at.strftime("%b %d, %Y %I:%M %p") if attempt.submitted_at else "N/A"
    page.insert_text(fitz.Point(310, 172), f"Completed: {completed_time_str}", fontsize=9, fontname="helv", color=TEXT_DARK)
    page.insert_text(fitz.Point(310, 189), f"Attempt ID: {str(attempt.attempt_uuid)[:18]}...", fontsize=8, fontname="helv", color=TEXT_MUTED)

    # 3. Primary Scorecard Summary Cards (4 Columns)
    y_scores = 220
    card_width = 122
    card_gap = 12

    # Card 1: Score
    c1_rect = fitz.Rect(35, y_scores, 35 + card_width, y_scores + 75)
    page.draw_rect(c1_rect, color=BORDER_COLOR, fill=(1, 1, 1), width=1)
    page.insert_text(fitz.Point(45, y_scores + 22), "SCORE OBTAINED", fontsize=8, fontname="helv", color=TEXT_MUTED)
    page.insert_text(fitz.Point(45, y_scores + 48), f"{attempt.score_obtained} / {attempt.total_marks}", fontsize=14, fontname="helv", color=HEADER_BG)
    page.insert_text(fitz.Point(45, y_scores + 64), f"Percentage: {attempt.percentage}%", fontsize=8, fontname="helv", color=TEXT_DARK)

    # Card 2: Result Outcome
    c2_rect = fitz.Rect(35 + card_width + card_gap, y_scores, 35 + card_width * 2 + card_gap, y_scores + 75)
    page.draw_rect(c2_rect, color=BORDER_COLOR, fill=(1, 1, 1), width=1)
    page.insert_text(fitz.Point(45 + card_width + card_gap, y_scores + 22), "FINAL RESULT", fontsize=8, fontname="helv", color=TEXT_MUTED)
    res_text = "PASSED" if attempt.is_passed else "NOT QUALIFIED"
    res_color = PASS_GREEN if attempt.is_passed else FAIL_RED
    page.insert_text(fitz.Point(45 + card_width + card_gap, y_scores + 48), res_text, fontsize=13, fontname="helv", color=res_color)
    page.insert_text(fitz.Point(45 + card_width + card_gap, y_scores + 64), f"Benchmark: {exam.passing_percentage}%", fontsize=8, fontname="helv", color=TEXT_MUTED)

    # Card 3: Rank
    c3_rect = fitz.Rect(35 + (card_width + card_gap) * 2, y_scores, 35 + card_width * 3 + card_gap * 2, y_scores + 75)
    page.draw_rect(c3_rect, color=BORDER_COLOR, fill=(1, 1, 1), width=1)
    page.insert_text(fitz.Point(45 + (card_width + card_gap) * 2, y_scores + 22), "CANDIDATE RANK", fontsize=8, fontname="helv", color=TEXT_MUTED)
    rank_str = f"#{attempt.rank}" if attempt.rank else "N/A"
    page.insert_text(fitz.Point(45 + (card_width + card_gap) * 2, y_scores + 48), rank_str, fontsize=14, fontname="helv", color=HEADER_BG)
    page.insert_text(fitz.Point(45 + (card_width + card_gap) * 2, y_scores + 64), f"Cohort: {analytics.get('total_cohort', 1)} Students", fontsize=8, fontname="helv", color=TEXT_MUTED)

    # Card 4: Percentile
    c4_rect = fitz.Rect(35 + (card_width + card_gap) * 3, y_scores, 560, y_scores + 75)
    page.draw_rect(c4_rect, color=BORDER_COLOR, fill=(1, 1, 1), width=1)
    page.insert_text(fitz.Point(45 + (card_width + card_gap) * 3, y_scores + 22), "PERCENTILE", fontsize=8, fontname="helv", color=TEXT_MUTED)
    pctl_str = f"{attempt.percentile}%" if attempt.percentile else "N/A"
    page.insert_text(fitz.Point(45 + (card_width + card_gap) * 3, y_scores + 48), pctl_str, fontsize=14, fontname="helv", color=HEADER_BG)
    page.insert_text(fitz.Point(45 + (card_width + card_gap) * 3, y_scores + 64), "Relative Standing", fontsize=8, fontname="helv", color=TEXT_MUTED)

    # 4. Question Breakdown Strip
    y_strip = 310
    strip_rect = fitz.Rect(35, y_strip, 560, y_strip + 36)
    page.draw_rect(strip_rect, color=BORDER_COLOR, fill=LIGHT_GRAY, width=1)
    page.insert_text(
        fitz.Point(45, y_strip + 22),
        f"Total Questions: {analytics['total_questions']}   |   Correct: {analytics['correct_count']}   |   Incorrect: {analytics['incorrect_count']}   |   Unattempted: {analytics['unattempted_count']}   |   Accuracy: {analytics['accuracy_percentage']}%   |   Time: {analytics['total_time_minutes']} Mins",
        fontsize=8.5,
        fontname="helv",
        color=TEXT_DARK,
    )

    # 5. Subject-Wise Performance Table
    y_table = 365
    page.insert_text(fitz.Point(35, y_table), "SUBJECT-WISE PERFORMANCE BREAKDOWN", fontsize=11, fontname="helv", color=HEADER_BG)

    # Table Header Row
    y_th = y_table + 12
    th_rect = fitz.Rect(35, y_th, 560, y_th + 24)
    page.draw_rect(th_rect, color=None, fill=HEADER_BG)

    headers = [("Subject", 45), ("Total", 175), ("Attempted", 225), ("Correct", 285), ("Incorrect", 345), ("Marks", 410), ("Accuracy", 480)]
    for title, x_pos in headers:
        page.insert_text(fitz.Point(x_pos, y_th + 16), title, fontsize=8.5, fontname="helv", color=(1, 1, 1))

    # Table Body Rows
    y_row = y_th + 24
    for idx, subj in enumerate(analytics.get("subject_summary", [])):
        bg_color = (0.97, 0.98, 1.0) if idx % 2 == 1 else (1, 1, 1)
        row_rect = fitz.Rect(35, y_row, 560, y_row + 22)
        page.draw_rect(row_rect, color=BORDER_COLOR, fill=bg_color, width=0.5)

        page.insert_text(fitz.Point(45, y_row + 15), str(subj["subject"])[:20], fontsize=8.5, fontname="helv", color=TEXT_DARK)
        page.insert_text(fitz.Point(175, y_row + 15), str(subj["total"]), fontsize=8.5, fontname="helv", color=TEXT_DARK)
        page.insert_text(fitz.Point(225, y_row + 15), str(subj["attempted"]), fontsize=8.5, fontname="helv", color=TEXT_DARK)
        page.insert_text(fitz.Point(285, y_row + 15), str(subj["correct"]), fontsize=8.5, fontname="helv", color=PASS_GREEN)
        page.insert_text(fitz.Point(345, y_row + 15), str(subj["incorrect"]), fontsize=8.5, fontname="helv", color=FAIL_RED)
        page.insert_text(fitz.Point(410, y_row + 15), f"{subj['marks_obtained']}/{subj['max_marks']}", fontsize=8.5, fontname="helv", color=TEXT_DARK)
        page.insert_text(fitz.Point(480, y_row + 15), f"{subj['accuracy']}%", fontsize=8.5, fontname="helv", color=HEADER_BG)
        y_row += 22

    # 6. Difficulty & Strengths Section
    y_diag = y_row + 25
    page.insert_text(fitz.Point(35, y_diag), "DIFFICULTY ACCURACY & TOPIC DIAGNOSTICS", fontsize=11, fontname="helv", color=HEADER_BG)

    # Difficulty Box
    diff_box = fitz.Rect(35, y_diag + 12, 285, y_diag + 115)
    page.draw_rect(diff_box, color=BORDER_COLOR, fill=LIGHT_GRAY, width=1)
    page.insert_text(fitz.Point(45, y_diag + 28), "Difficulty Level Breakdown", fontsize=9, fontname="helv", color=HEADER_BG)

    y_diff_line = y_diag + 46
    for d in analytics.get("difficulty_summary", []):
        page.insert_text(
            fitz.Point(45, y_diff_line),
            f"{d['level']}:  {d['correct']}/{d['total']} Correct  |  Accuracy: {d['accuracy']}%  |  Avg Time: {d['avg_time_seconds']}s",
            fontsize=8,
            fontname="helv",
            color=TEXT_DARK,
        )
        y_diff_line += 18

    # Strengths & Weaknesses Box
    sw_box = fitz.Rect(300, y_diag + 12, 560, y_diag + 115)
    page.draw_rect(sw_box, color=BORDER_COLOR, fill=LIGHT_GRAY, width=1)
    page.insert_text(fitz.Point(310, y_diag + 28), "Key Strengths & Weak Areas", fontsize=9, fontname="helv", color=HEADER_BG)

    # Top Strengths
    str_txt = ", ".join([f"{st['topic']} ({st['accuracy']}%)" for st in analytics.get("strong_topics", [])]) or "Balanced across syllabus"
    page.insert_text(fitz.Point(310, y_diag + 48), "Strong Topics (>=70%):", fontsize=8, fontname="helv", color=PASS_GREEN)
    page.insert_text(fitz.Point(310, y_diag + 62), str_txt[:55], fontsize=7.5, fontname="helv", color=TEXT_DARK)

    # Top Weaknesses
    wk_txt = ", ".join([f"{wk['topic']} ({wk['accuracy']}%)" for wk in analytics.get("weak_topics", [])]) or "No critical weaknesses"
    page.insert_text(fitz.Point(310, y_diag + 82), "Needs Improvement (<50%):", fontsize=8, fontname="helv", color=FAIL_RED)
    page.insert_text(fitz.Point(310, y_diag + 96), wk_txt[:55], fontsize=7.5, fontname="helv", color=TEXT_DARK)

    # 7. Verification & Security Footer
    y_footer = 770
    footer_line = fitz.Rect(35, y_footer, 560, y_footer + 1)
    page.draw_rect(footer_line, color=None, fill=BORDER_COLOR)

    now_str = timezone.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    page.insert_text(
        fitz.Point(35, y_footer + 18),
        f"This document is an authentic digitally generated scorecard issued by the TestForge CBT Examination Engine. Issued on {now_str}.",
        fontsize=7.5,
        fontname="helv",
        color=TEXT_MUTED,
    )
    page.insert_text(
        fitz.Point(35, y_footer + 30),
        f"Security Hash Token: {str(attempt.attempt_uuid)} | Confidential | Not for reproduction without authorization.",
        fontsize=7,
        fontname="helv",
        color=TEXT_MUTED,
    )

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes

