"""Regression tests for classroom result truthfulness, privacy and isolation."""
import csv
import io
from decimal import Decimal

import pymupdf
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.analytics import compute_exam_institutional_analytics
from apps.exams.classroom import _session_leaderboard
from apps.exams.classroom_results import classroom_result_summary
from apps.exams.models import ExamAttempt, ExamSecurityLog
from apps.questions.models import AnswerStatus
from .test_classroom import ClassroomBase


class ClassroomReportingTests(ClassroomBase):
    def take(self, session, answers, student=None):
        student = student or self.student
        self.join(student, session.code)
        self.start(student, session)
        attempt = session.attempts.get(student=student, status=ExamAttempt.AttemptStatus.IN_PROGRESS)
        for response in attempt.responses.select_related("exam_question__question"):
            key = answers.get(response.exam_question.order)
            if key:
                response.selected_option = response.exam_question.question.options.get(option_key=key)
                response.save(update_fields=["selected_option"])
        attempt.submit_attempt()
        attempt.refresh_from_db()
        return attempt

    def test_partial_result_review_and_pdf_never_call_ungraded_wrong(self):
        session = self.session_for(exam=self.make_exam(keyed=1, questions=3))
        attempt = self.take(session, {1: "B", 2: "A"})
        summary = classroom_result_summary(attempt)
        self.assertEqual(summary["counts"], {"correct": 0, "partial": 0, "incorrect": 1, "unattempted": 1, "ungraded": 1})
        self.assertEqual(summary["pending_keys"], 2)
        self.assertEqual(summary["unknown_keys"], 0)
        self.assertEqual(summary["percentage"], Decimal("0.00"))
        self.assertFalse(summary["fully_keyed"])
        self.client.force_login(self.student)
        detail = self.client.get(reverse("exams:student_result_detail", args=[attempt.attempt_uuid]))
        self.assertContains(detail, "had no confirmed key when graded")
        self.assertContains(detail, "Answered, ungraded")
        self.assertNotContains(detail, "FAILED")
        self.assertIn("private", detail["Cache-Control"])
        self.assertIn("no-store", detail["Cache-Control"])
        review = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        self.assertEqual(review.status_code, 200)
        self.assertIn("private", review["Cache-Control"])
        self.assertIn("no-store", review["Cache-Control"])
        cards = {q["response"].exam_question.order: q for q in review.context["reviewed_questions"]}
        self.assertEqual([cards[i]["status"] for i in (1, 2, 3)], ["incorrect", "ungraded", "unattempted"])
        self.assertIsNone(cards[2]["correct_option"])
        self.assertContains(review, "Your answer · ungraded")
        pdf = self.client.get(reverse("exams:student_result_pdf", args=[attempt.attempt_uuid]))
        self.assertEqual(pdf.status_code, 200)
        self.assertIn("no-store", pdf["Cache-Control"])
        with pymupdf.open(stream=pdf.content, filetype="pdf") as document:
            text = " ".join(page.get_text() for page in document)
        self.assertIn("Classroom result summary", text)
        self.assertIn("Answered, ungraded: 1", text)
        self.assertIn("may be partial", text)

    def test_saved_key_and_decisions_survive_later_key_edits(self):
        exam = self.make_exam(keyed=2, questions=2)
        session = self.session_for(exam=exam)
        attempt = self.take(session, {1: "A"})
        response = attempt.responses.get(exam_question__order=1)
        unanswered = attempt.responses.get(exam_question__order=2)
        self.assertTrue(response.key_confirmed_at_grading)
        self.assertEqual(response.correct_option_key_at_grading, "A")
        self.assertEqual(response.selected_option_key_at_grading, "A")
        self.assertTrue(unanswered.key_confirmed_at_grading)
        question = response.exam_question.question
        question.options.filter(option_key="A").update(is_correct=False)
        question.options.filter(option_key="B").update(is_correct=True)
        second = unanswered.exam_question.question
        second.answer_status = AnswerStatus.NEEDS_REVIEW
        second.save(update_fields=["answer_status"])
        second.options.update(is_correct=False)
        summary = classroom_result_summary(attempt)
        self.assertTrue(summary["fully_keyed"])
        self.assertEqual(summary["percentage"], Decimal("50.00"))
        self.client.force_login(self.student)
        review = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        first = next(q for q in review.context["reviewed_questions"] if q["response"].exam_question.order == 1)
        self.assertEqual(first["status"], "correct")
        self.assertEqual(first["saved_key"], "A")
        self.assertEqual(first["correct_option"].option_key, "A")
        self.assertContains(review, "Key when graded: <strong>A</strong>")
        self.assertEqual(first["explanation"], "")

    def test_legacy_unanswered_key_status_is_unknown_not_guessed(self):
        session = self.session_for(exam=self.make_exam(keyed=2, questions=2))
        attempt = self.take(session, {1: "A"})
        attempt.responses.update(key_confirmed_at_grading=None, correct_option_key_at_grading=None,
                                 selected_option_key_at_grading=None)
        summary = classroom_result_summary(attempt)
        self.assertEqual(summary["unknown_keys"], 1)
        self.assertEqual(summary["pending_keys"], 0)
        self.assertFalse(summary["fully_keyed"])
        self.client.force_login(self.student)
        detail = self.client.get(reverse("exams:student_result_detail", args=[attempt.attempt_uuid]))
        self.assertContains(detail, "key status at grading was not recorded")
        review = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        self.assertIsNone(review.context["reviewed_questions"][0]["correct_option"])
        self.assertContains(review, "Original key status was not recorded")

    def test_all_unanswered_but_confirmed_keys_are_a_recorded_zero(self):
        session = self.session_for(exam=self.make_exam(keyed=2, questions=2))
        attempt = self.take(session, {})
        summary = classroom_result_summary(attempt)
        self.assertTrue(summary["fully_keyed"])
        self.assertTrue(summary["score_available"])
        self.assertEqual(summary["percentage"], 0)
        self.assertEqual(summary["counts"]["unattempted"], 2)

    def test_ungraded_attempt_is_not_ranked_or_exported_as_a_zero(self):
        session = self.session_for(exam=self.make_exam(keyed=0, questions=2))
        attempt = self.take(session, {1: "A", 2: "B"})
        self.assertEqual(_session_leaderboard(session), [])
        self.client.force_login(self.teacher)
        rows = self.client.get(reverse("exams:classroom_monitor", args=[session.pk])).json()["rows"]
        self.assertIsNone(rows[0]["score"])
        exported = self.client.get(reverse("exams:classroom_export", args=[session.pk]))
        row = list(csv.DictReader(io.StringIO(exported.content.decode())))[0]
        self.assertEqual(row["Score (confirmed keys)"], "")
        self.assertEqual(row["Percentage (confirmed keys)"], "")
        self.assertEqual(str(attempt.attempt_uuid), rows[0]["attempt_uuid"])

    def test_monitor_csv_and_leaderboard_use_best_retake(self):
        session = self.session_for(exam=self.make_exam(keyed=2, questions=2), allow_retakes=True)
        older = self.take(session, {1: "A", 2: "A"})
        newer = self.take(session, {1: "A", 2: "B"})
        self.assertNotEqual(older.score_obtained, newer.score_obtained)
        self.client.force_login(self.teacher)
        row = self.client.get(reverse("exams:classroom_monitor", args=[session.pk])).json()["rows"][0]
        # Status tracks the latest participation; score and result link use
        # the same best completed result as the leaderboard and export.
        self.assertEqual(row["attempt_uuid"], str(older.attempt_uuid))
        self.assertEqual(row["score"], float(older.score_obtained))
        exported = self.client.get(reverse("exams:classroom_export", args=[session.pk]))
        csv_row = list(csv.DictReader(io.StringIO(exported.content.decode())))[0]
        self.assertEqual(Decimal(csv_row["Score (confirmed keys)"]), older.score_obtained)
        self.assertEqual(Decimal(_session_leaderboard(session)[0]["score"]), older.score_obtained)

    def test_security_events_and_unreleased_results_respect_permissions(self):
        exam = self.make_exam(keyed=1, questions=1)
        exam.show_result_immediately = False
        exam.save(update_fields=["show_result_immediately"])
        session = self.session_for(exam=exam)
        attempt = self.take(session, {1: "A"})
        ExamSecurityLog.objects.create(attempt=attempt, event_type=ExamSecurityLog.EventType.TAB_SWITCH,
                                       details="reported signal")
        detail = reverse("exams:student_result_detail", args=[attempt.attempt_uuid])
        pdf = reverse("exams:student_result_pdf", args=[attempt.attempt_uuid])
        self.client.force_login(self.student)
        self.assertContains(self.client.get(detail), "Results are under evaluation")
        self.assertNotContains(self.client.get(detail), "reported signal")
        self.assertEqual(self.client.get(pdf).status_code, 403)
        self.client.force_login(self.other_teacher)
        self.assertEqual(self.client.get(detail).status_code, 403)
        self.assertEqual(self.client.get(pdf).status_code, 403)
        self.client.force_login(self.teacher)
        self.assertContains(self.client.get(detail), "reported signal")
        self.assertEqual(self.client.get(pdf).status_code, 200)

    def test_teacher_can_render_join_qr_code_locally(self):
        session = self.session_for()
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("exams:classroom_qr", args=[session.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertIn(b"<svg", response.content)
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.client.force_login(self.other_teacher)
        self.assertEqual(self.client.get(reverse("exams:classroom_qr", args=[session.pk])).status_code, 403)

    def test_active_results_are_not_presented_as_final(self):
        session = self.session_for()
        self.join(self.student, session.code)
        self.start(self.student, session)
        attempt = session.attempts.get(student=self.student)
        self.client.force_login(self.teacher)
        for name in ("student_result_detail", "student_result_review", "student_result_pdf"):
            self.assertEqual(self.client.get(reverse("exams:" + name, args=[attempt.attempt_uuid])).status_code, 403)
        monitor = self.client.get(reverse("exams:classroom_monitor", args=[session.pk])).json()
        self.assertIsNone(monitor["rows"][0]["result_url"])
        self.client.force_login(self.student)
        completed = self.client.get(reverse("exams:exam_completed", args=[attempt.attempt_uuid]))
        self.assertRedirects(completed, reverse("exams:exam_session", args=[attempt.attempt_uuid]), fetch_redirect_response=False)

    def test_classroom_results_do_not_pollute_institutional_cohorts_or_attempt_limit(self):
        exam = self.make_exam(keyed=1, questions=1)
        exam.max_attempts = 1
        exam.save(update_fields=["max_attempts"])
        session = self.session_for(exam=exam)
        classroom_attempt = self.take(session, {1: "A"})
        public_student = User.objects.create_user(username="public_candidate", email="public@test.com", role=User.Roles.STUDENT)
        ordinary = ExamAttempt.objects.create(exam=exam, student=public_student, status=ExamAttempt.AttemptStatus.SUBMITTED,
                                               score_obtained=2, total_marks=4, percentage=50)
        ordinary.recalculate_ranks()
        classroom_attempt.refresh_from_db()
        self.assertIsNone(classroom_attempt.rank)
        self.assertEqual(compute_exam_institutional_analytics(exam)["total_attempts"], 1)
        self.client.force_login(self.teacher)
        exported = self.client.get(reverse("exams:teacher_export_results_csv", args=[exam.pk])).content.decode()
        self.assertIn("public_candidate", exported)
        self.assertNotIn(self.student.username, exported)
        self.client.force_login(self.student)
        self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        self.assertTrue(ExamAttempt.objects.filter(exam=exam, student=self.student, classroom_session__isnull=True,
                                                   status=ExamAttempt.AttemptStatus.IN_PROGRESS).exists())
