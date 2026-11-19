"""PDF-derived institutional questions never silently grade unconfirmed keys."""
import json
import tempfile
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pymupdf as fitz
from django.apps import apps
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.analytics import compute_exam_institutional_analytics, compute_student_attempt_analytics
from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion
from apps.questions.models import AnswerStatus, ExtractedQuestion, Question, QuestionPaperUpload


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PublicPdfConfirmedKeyTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        config = override_settings(MEDIA_ROOT=str(Path(temp.name) / "media"), PRIVATE_PRACTICE_ROOT=str(Path(temp.name) / "private"))
        config.enable(); self.addCleanup(config.disable)
        self.teacher = User.objects.create_user(username="pdf_owner", email="owner@example.test", password="x", role="TEACHER")
        self.student = User.objects.create_user(username="pdf_student", email="student@example.test", password="x", role="STUDENT", exam_track="jee")
        self.second = User.objects.create_user(username="pdf_second", email="second@example.test", password="x", role="STUDENT", exam_track="jee")
        self.category = Category.objects.create(name="Public PDF", slug="public-pdf")
        self.paper = QuestionPaperUpload.objects.create(title="Reviewed PDF", category=self.category,
            uploaded_by=self.teacher, status=QuestionPaperUpload.ProcessingStatus.EXTRACTED, image_first=False, target_type="jee")
        options = [{"key": key, "text": f"Choice {key}"} for key in "ABCD"]
        self.staged = []
        for number, key, status, kind in ((1, "B", AnswerStatus.NEEDS_REVIEW, "mcq_single"),
                                         (2, "B", AnswerStatus.MANUALLY_VERIFIED, "mcq_single"),
                                         (3, "A", AnswerStatus.MANUALLY_VERIFIED, "mcq_multiple")):
            self.staged.append(ExtractedQuestion.objects.create(paper=self.paper, question_number=number,
                question_text=f"Question {number}?", options=list(options), correct_option=key,
                answer_status=status, detected_type=kind, is_flagged=False, is_image_first=False))
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.post(reverse("questions:paper_bulk_approve", args=[self.paper.pk])).status_code, 302)
        for q in self.staged:
            q.refresh_from_db()
        self.questions = [q.approved_question for q in self.staged]
        self.assertTrue(all(q is not None and q.is_pdf_derived for q in self.questions))
        self.exam = Exam.objects.create(title="PDF key exam", slug="pdf-key-exam", category=self.category,
            creator=self.teacher, status=Exam.ExamStatus.PUBLISHED, is_free=True, duration_minutes=30,
            passing_percentage=40, shuffle_questions=False, shuffle_options=False,
            show_result_immediately=True, allow_review_answers=True, target_type="jee")
        for number, q in enumerate(self.questions, 1):
            ExamQuestion.objects.create(exam=self.exam, question=q, order=number, positive_marks=4, negative_marks=1)
        self.exam.recalculate_marks()

    def take(self, user, keys):
        self.client.force_login(user)
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[self.exam.slug])).status_code, 302)
        attempt = ExamAttempt.objects.get(exam=self.exam, student=user)
        page = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertNotContains(page, '"is_correct"')
        for item, key in zip(page.context["questions_data"], keys):
            choice = next(option for option in item["options"] if option["key"] == key)
            saved = self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
                data=json.dumps({"response_id": item["response_id"], "selected_option_id": choice["id"], "action": "navigate"}),
                content_type="application/json")
            self.assertEqual(saved.status_code, 200)
        submitted = self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), data="{}", content_type="application/json")
        self.assertIn(submitted.status_code, (200, 302))
        attempt.refresh_from_db()
        return attempt

    def test_partial_keys_are_ungraded_unranked_and_snapshot_survives_later_edits(self):
        q1, q2, q3 = self.questions
        # The unconfirmed staging value is retained but is not a bank answer.
        self.assertEqual(self.staged[0].correct_option, "B")
        self.assertFalse(q1.options.filter(is_correct=True).exists())
        self.assertEqual(q1.answer_status, AnswerStatus.NEEDS_REVIEW)
        self.assertEqual(q2.options.get(is_correct=True).option_key, "B")
        self.assertFalse(q3.options.filter(is_correct=True).exists())
        # Deleting/unlinking a paper cannot silently disable the grading guard.
        q1.source_paper = None; q1.save(update_fields=["source_paper"])
        # Even legacy/malformed multiple-correct items with one flagged option
        # must not be treated as a single-correct answer.
        q3.options.filter(option_key="A").update(is_correct=True)
        q3.answer_status = AnswerStatus.MANUALLY_VERIFIED; q3.save(update_fields=["answer_status"])
        first = self.take(self.student, ("A", "B", "A"))
        rows = list(first.responses.order_by("order"))
        self.assertEqual([r.is_correct for r in rows], [None, True, None])
        self.assertEqual([r.key_confirmed_at_grading for r in rows], [False, True, False])
        self.assertEqual(first.score_obtained, Decimal("4.00"))
        self.assertEqual(first.percentage, Decimal("100.00"))  # only 4 gradeable marks
        self.assertFalse(first.is_passed)  # a partial score is not a final outcome
        self.assertIsNone(first.rank)
        self.assertTrue(first.has_ungraded_keys)
        self.assertEqual(first.graded_possible_marks, Decimal("4.00"))
        result = self.client.get(reverse("exams:student_result_detail", args=[first.attempt_uuid]))
        self.assertContains(result, "Partial result — not ranked")
        self.assertContains(result, "Answered, ungraded: 2")
        self.assertNotContains(result, "NOT QUALIFIED")
        completed = self.client.get(reverse("exams:exam_completed", args=[first.attempt_uuid]))
        self.assertContains(completed, "Partial result — not ranked")
        review = self.client.get(reverse("exams:student_result_review", args=[first.attempt_uuid]))
        cards = review.context["reviewed_questions"]
        self.assertEqual([item["status"] for item in cards], ["ungraded", "correct", "ungraded"])
        self.assertIsNone(cards[0]["correct_option"]); self.assertIsNone(cards[2]["correct_option"])
        self.assertContains(review, "Your answer · ungraded")
        pdf = self.client.get(reverse("exams:student_result_pdf", args=[first.attempt_uuid]))
        with fitz.open(stream=pdf.content, filetype="pdf") as document:
            text = " ".join(page.get_text() for page in document)
        self.assertIn("Partial examination result", text)
        self.assertIn("Answered, ungraded: 2", text)
        self.assertNotIn("NOT QUALIFIED", text)

        # Key repairs affect only new attempts. The first result and ranking
        # remain the grading-time snapshot, while a complete second attempt ranks.
        for question in (q1, q2, q3):
            question.options.update(is_correct=False)
            question.options.filter(option_key="A").update(is_correct=True)
            question.answer_status = AnswerStatus.MANUALLY_VERIFIED
            question.question_type = Question.QuestionType.MCQ_SINGLE
            question.save(update_fields=["answer_status", "question_type"])
        second = self.take(self.second, ("A", "A", "A"))
        first.refresh_from_db(); second.refresh_from_db()
        self.assertEqual(second.rank, 1)
        self.assertIsNone(first.rank)
        self.assertEqual(second.score_obtained, Decimal("12.00"))
        self.client.force_login(self.student)
        review = self.client.get(reverse("exams:student_result_review", args=[first.attempt_uuid]))
        cards = review.context["reviewed_questions"]
        self.assertEqual([item["status"] for item in cards], ["ungraded", "correct", "ungraded"])
        self.assertIsNone(cards[0]["correct_option"])
        self.assertEqual(cards[1]["saved_key"], "B")
        self.assertEqual(cards[1]["correct_option"].option_key, "B")
        self.assertEqual(cards[1]["explanation"], "")
        leaderboard = self.client.get(reverse("exams:exam_leaderboard", args=[self.exam.slug]))
        self.assertContains(leaderboard, "remain unranked")
        self.assertContains(leaderboard, self.second.username)
        self.assertEqual([entry.student_id for entry in leaderboard.context["leaderboard"]], [self.second.pk])
        self.client.force_login(self.teacher)
        csv = self.client.get(reverse("exams:teacher_export_results_csv", args=[self.exam.pk])).content.decode()
        self.assertIn("PARTIAL / UNRANKED", csv)
        self.assertIn("(partial)", csv)

    def test_unsupported_non_pdf_types_are_ungraded_not_single_choice_scores(self):
        q1, q2, q3 = self.questions
        q1.answer_status = AnswerStatus.MANUALLY_VERIFIED
        q1.options.filter(option_key="A").update(is_correct=True)
        q1.save(update_fields=["answer_status"])
        q3.is_pdf_derived = False
        q3.answer_status = AnswerStatus.MANUALLY_VERIFIED
        q3.options.filter(option_key="A").update(is_correct=True)
        for user, kind in ((self.student, Question.QuestionType.MCQ_MULTIPLE),
                           (self.second, Question.QuestionType.NUMERICAL)):
            with self.subTest(kind=kind):
                q3.question_type = kind
                q3.save(update_fields=["is_pdf_derived", "question_type", "answer_status"])
                attempt = self.take(user, ("A", "B", "A"))
                response = attempt.responses.get(order=3)
                self.assertIsNone(response.is_correct)
                self.assertFalse(response.key_confirmed_at_grading)
                self.assertEqual(response.marks_awarded, Decimal("0.00"))
                self.assertIsNone(attempt.rank)
                review = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
                card = review.context["reviewed_questions"][2]
                self.assertEqual(card["status"], "ungraded")
                self.assertIsNone(card["correct_option"])
                self.assertNotContains(review, "NOT QUALIFIED")

    def test_missing_or_ambiguous_non_pdf_key_is_not_marked_wrong(self):
        q1, q2, q3 = self.questions
        q1.answer_status = AnswerStatus.MANUALLY_VERIFIED
        q1.options.filter(option_key="A").update(is_correct=True)
        q1.save(update_fields=["answer_status"])
        q3.is_pdf_derived = False
        q3.question_type = Question.QuestionType.MCQ_SINGLE
        q3.answer_status = AnswerStatus.MANUALLY_VERIFIED
        q3.save(update_fields=["is_pdf_derived", "question_type", "answer_status"])
        for user, keys in ((self.student, ""), (self.second, "AB")):
            with self.subTest(keys=keys):
                q3.options.update(is_correct=False)
                q3.options.filter(option_key__in=list(keys)).update(is_correct=True)
                attempt = self.take(user, ("A", "B", "A"))
                response = attempt.responses.get(order=3)
                self.assertIsNone(response.is_correct)
                self.assertFalse(response.key_confirmed_at_grading)
                self.assertEqual(response.marks_awarded, Decimal("0.00"))
                self.assertIsNone(attempt.rank)
                review = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
                self.assertEqual(review.context["reviewed_questions"][2]["status"], "ungraded")
                self.assertIsNone(review.context["reviewed_questions"][2]["correct_option"])

    def test_legacy_pdf_attempt_uses_saved_decision_not_the_current_key(self):
        q1, q2, q3 = self.questions
        # Simulate an attempt created before PDF-derived key snapshots existed.
        for q in (q1, q3):
            q.question_type = Question.QuestionType.MCQ_SINGLE
            q.answer_status = AnswerStatus.MANUALLY_VERIFIED
            q.save(update_fields=["question_type", "answer_status"])
            q.options.update(is_correct=False)
            q.options.filter(option_key="A").update(is_correct=True)
        q2.is_pdf_derived = False
        q2.save(update_fields=["is_pdf_derived"])
        attempt = self.take(self.student, ("A", "B", "A"))
        row = attempt.responses.get(order=2)
        self.assertIsNone(row.key_confirmed_at_grading)
        self.assertTrue(row.is_correct)
        old_score, old_rank = attempt.score_obtained, attempt.rank
        migration = import_module("apps.questions.migrations.0016_flag_pdf_derived_questions")
        migration.mark_public_paper_questions(apps, SimpleNamespace(connection=connection))
        q2.refresh_from_db()
        self.assertTrue(q2.is_pdf_derived)
        q2.options.update(is_correct=False)
        q2.options.filter(option_key="A").update(is_correct=True)
        q2.explanation = "CURRENT KEY EXPLANATION SHOULD NOT APPEAR"
        q2.save(update_fields=["explanation"])
        review = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        card = review.context["reviewed_questions"][1]
        self.assertEqual(card["status"], "correct")
        self.assertIsNone(card["correct_option"])
        self.assertIsNone(card["saved_key"])
        self.assertEqual(card["explanation"], "")
        self.assertContains(review, "Original key status was not recorded")
        self.assertNotContains(review, "CURRENT KEY EXPLANATION SHOULD NOT APPEAR")
        detail = self.client.get(reverse("exams:student_result_detail", args=[attempt.attempt_uuid]))
        self.assertContains(detail, "before grading-time key details were saved")
        # SET_NULL must not turn the already graded response into unattempted.
        q2.options.get(option_key="B").delete()
        attempt.refresh_from_db()
        row.refresh_from_db()
        self.assertIsNone(row.selected_option_id)
        self.assertTrue(row.is_correct)
        self.assertEqual(attempt.answered_count, 3)
        analytics = compute_student_attempt_analytics(attempt)
        self.assertEqual((analytics["correct_count"], analytics["incorrect_count"],
                          analytics["unattempted_count"], analytics["ungraded_count"]), (3, 0, 0, 0))
        self.assertEqual(analytics["subject_summary"][0]["marks_obtained"], Decimal("12.00"))
        stats = compute_exam_institutional_analytics(self.exam)["question_stats"][1]
        self.assertEqual((stats["attempted_count"], stats["correct_count"]), (1, 1))
        review = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        self.assertEqual(review.context["reviewed_questions"][1]["status"], "correct")
        attempt.refresh_from_db()
        self.assertEqual((attempt.score_obtained, attempt.rank), (old_score, old_rank))

    def test_deleted_choices_preserve_saved_partial_counts_and_labels(self):
        first = self.take(self.student, ("A", "B", "A"))
        for question in self.questions:
            question.question_type = Question.QuestionType.MCQ_SINGLE
            question.answer_status = AnswerStatus.MANUALLY_VERIFIED
            question.save(update_fields=["question_type", "answer_status"])
            question.options.update(is_correct=False)
            question.options.filter(option_key="A").update(is_correct=True)
        self.take(self.second, ("A", "A", "A"))
        self.questions[0].options.get(option_key="A").delete()
        self.questions[1].options.get(option_key="B").delete()
        first.refresh_from_db()
        self.assertEqual(first.answered_count, 3)
        analytics = compute_student_attempt_analytics(first)
        self.assertEqual((analytics["correct_count"], analytics["incorrect_count"],
                          analytics["unattempted_count"], analytics["ungraded_count"]), (1, 0, 0, 2))
        subject = analytics["subject_summary"][0]
        self.assertEqual((subject["attempted"], subject["ungraded"], subject["unattempted"]), (1, 2, 0))
        self.assertEqual(subject["max_marks"], Decimal("4.00"))
        self.client.force_login(self.student)
        review = self.client.get(reverse("exams:student_result_review", args=[first.attempt_uuid]))
        self.assertEqual([card["status"] for card in review.context["reviewed_questions"]],
                         ["ungraded", "correct", "ungraded"])
        self.assertContains(review, "Choice when graded: <strong>B</strong>")
        self.assertContains(review, "Key when graded: <strong>B</strong>")
        detail = self.client.get(reverse("exams:student_result_detail", args=[first.attempt_uuid]))
        self.assertEqual(detail.context["partial_summary"]["counts"],
                         {"correct": 1, "partial": 0, "incorrect": 0, "unattempted": 0, "ungraded": 2})
        stats = compute_exam_institutional_analytics(self.exam)["question_stats"][1]
        self.assertEqual((stats["attempted_count"], stats["correct_count"]), (2, 2))

    def test_migration_marks_old_public_paper_questions_without_changing_keys_or_attempts(self):
        q = self.questions[1]
        q.is_pdf_derived = False; q.save(update_fields=["is_pdf_derived"])
        before = list(q.options.values_list("option_key", "is_correct"))
        migration = import_module("apps.questions.migrations.0016_flag_pdf_derived_questions")
        migration.mark_public_paper_questions(apps, SimpleNamespace(connection=connection))
        q.refresh_from_db()
        self.assertTrue(q.is_pdf_derived)
        self.assertEqual(list(q.options.values_list("option_key", "is_correct")), before)
        self.assertFalse(ExamAttempt.objects.filter(exam=self.exam).exists())
