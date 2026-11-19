"""Deterministic regressions for PostgreSQL row locks and deferred FKs.

SQLite has a database-wide writer lock, so these interleavings are deliberately
PostgreSQL-only. Each actor uses an independent request/connection.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, local
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.payments.services import lock_practice_user
from apps.questions.models import AnswerStatus, Question, QuestionOption
from .models import Category, ClassroomParticipant, ClassroomSession, Exam, ExamAttempt, ExamQuestion


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row locks and deferred foreign keys required")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PostgreSQLClassroomLockingTests(TransactionTestCase):
    def setUp(self):
        self.teacher = User.objects.create_user("pg_teacher", email="pg-t@example.test", role="TEACHER")
        self.student = User.objects.create_user("pg_student", email="pg-s@example.test", role="STUDENT", exam_track="jee")
        category = Category.objects.create(name="PostgreSQL locks", slug="postgresql-locks")
        self.exam = Exam.objects.create(title="Locking test", slug="postgresql-locking-test", creator=self.teacher,
            category=category, status="published", is_classroom_only=True, duration_minutes=30,
            total_marks=4, shuffle_questions=False, shuffle_options=False, target_type="jee", strict_exam_mode=True)
        question = Question.objects.create(category=category, creator=self.teacher, is_classroom_snapshot=True,
            answer_status=AnswerStatus.MANUALLY_VERIFIED, subject="Math", question_text="Question?", target_type="jee")
        self.correct = QuestionOption.objects.create(question=question, option_key="A", option_text="A", is_correct=True)
        QuestionOption.objects.create(question=question, option_key="B", option_text="B")
        ExamQuestion.objects.create(exam=self.exam, question=question, order=1, positive_marks=4, negative_marks=1)
        self.session = ClassroomSession.create_for(self.exam, self.teacher)
        ClassroomParticipant.objects.create(session=self.session, student=self.student)
        self.student_client, self.teacher_client, self.resume_client = Client(), Client(), Client()
        self.student_client.force_login(self.student)
        self.resume_client.force_login(self.student)
        self.teacher_client.force_login(self.teacher)
        self.student_client.post(reverse("exams:classroom_take", args=[self.session.pk]))
        self.attempt = self.session.attempts.get(student=self.student)
        self.attempt.responses.update(selected_option=self.correct)

    def race_finalization(self, mode, opponent):
        """Finish while the other request owns a row referenced by the attempt.

        PostgreSQL defers Django's FK checks until COMMIT. The opponent holds
        the session (end) or the user (resume), then waits for the attempt;
        finalization must not wait for either row while it holds the attempt.
        """
        if mode == "security":
            self.attempt.tab_switch_warnings = 3
            self.attempt.save(update_fields=["tab_switch_warnings"])
            method_name = "terminate_for_security"
        else:
            method_name = "submit_attempt"
            if mode == "timeout":
                self.attempt.expires_at = timezone.now() - timedelta(seconds=1)
                self.attempt.save(update_fields=["expires_at"])
        finalized, opponent_locked, committing = Event(), Event(), Event()
        actor = local()
        original_finish = getattr(ExamAttempt, method_name)
        original_end = ClassroomSession.end_now

        def paused_finish(attempt, *args, **kwargs):
            result = original_finish(attempt, *args, **kwargs)
            if getattr(actor, "name", "") == "student":
                finalized.set()
                if not opponent_locked.wait(timeout=10):
                    raise AssertionError("Opponent did not acquire its lock")
                committing.set()
            return result

        def paused_end(session):
            # ClassroomSessionControlView has already locked the session.
            opponent_locked.set()
            if not committing.wait(timeout=10):
                raise AssertionError("Finalization did not reach commit")
            return original_end(session)

        def paused_user(user):
            locked = lock_practice_user(user)
            if getattr(actor, "name", "") == "resume":
                opponent_locked.set()
                if not committing.wait(timeout=10):
                    raise AssertionError("Finalization did not reach commit")
            return locked

        def student_request():
            close_old_connections()
            actor.name = "student"
            try:
                if mode == "security":
                    return self.student_client.post(reverse("exams:exam_log_event", args=[self.attempt.attempt_uuid]),
                        data=json.dumps({"event_type": "TAB_SWITCH", "client_event_id": "fourth"}), content_type="application/json")
                if mode == "timeout":
                    return self.student_client.get(reverse("exams:exam_sync_status", args=[self.attempt.attempt_uuid]))
                return self.student_client.post(reverse("exams:exam_submit", args=[self.attempt.attempt_uuid]),
                    data=json.dumps({}), content_type="application/json")
            finally:
                close_old_connections()

        def opponent_request():
            close_old_connections()
            actor.name = opponent
            try:
                if opponent == "end":
                    return self.teacher_client.post(reverse("exams:classroom_control", args=[self.session.pk]),
                                                    {"action": "end", "confirm": "yes"})
                return self.resume_client.post(reverse("exams:classroom_take", args=[self.session.pk]))
            finally:
                close_old_connections()

        with patch.object(ExamAttempt, method_name, paused_finish), \
             patch.object(ClassroomSession, "end_now", paused_end), \
             patch("apps.payments.services.lock_practice_user", paused_user):
            with ThreadPoolExecutor(max_workers=2) as pool:
                student_future = pool.submit(student_request)
                try:
                    self.assertTrue(finalized.wait(timeout=10), "Student did not lock and finalize the attempt")
                    opponent_future = pool.submit(opponent_request)
                    self.assertTrue(opponent_locked.wait(timeout=10), "Opponent did not acquire its lock")
                finally:
                    # Release either side if an assertion fails, so the pool can exit.
                    opponent_locked.set()
                    committing.set()
                student_response = student_future.result(timeout=30)
                opponent_response = opponent_future.result(timeout=30)
        self.assertEqual(student_response.status_code, 200)
        self.assertEqual(opponent_response.status_code, 302)
        self.attempt.refresh_from_db()
        expected = {"security": "terminated", "submit": "submitted", "timeout": "timed_out"}[mode]
        self.assertEqual(self.attempt.status, expected)
        self.assertEqual(self.attempt.is_security_locked, mode == "security")
        self.assertEqual(self.attempt.responses.get().selected_option_id, self.correct.pk)
        self.assertEqual(float(self.attempt.score_obtained), 4.0)
        self.assertEqual(self.exam.attempts.filter(student=self.student).count(), 1)
        if opponent == "end":
            self.session.refresh_from_db()
            self.assertTrue(self.session.is_ended)

    def test_end_while_security_termination_commits(self):
        self.race_finalization("security", "end")

    def test_end_while_student_submission_commits(self):
        self.race_finalization("submit", "end")

    def test_end_while_timeout_commits(self):
        self.race_finalization("timeout", "end")

    def test_resume_while_student_submission_commits(self):
        self.race_finalization("submit", "resume")
