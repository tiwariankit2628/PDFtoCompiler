"""Strict Exam Mode: the disclosed three-warning page-switch rule.

All events here are client-reported and mocked. These tests verify the
SERVER-side counting, idempotency, atomicity and termination logic only; they do
NOT prove real-device tab-switch, phone-call or screen-lock behaviour.
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion, ExamSecurityLog
from apps.questions.models import Question, QuestionOption


class StrictExamModeTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Strict", slug="strict", is_active=True)
        self.teacher = User.objects.create_user(username="strict_teacher", email="st@test.com",
                                                 password="Password123!", role=User.Roles.TEACHER, is_staff=True)
        self.student = User.objects.create_user(username="strict_student", email="ss@test.com",
                                                password="Password123!", role=User.Roles.STUDENT, exam_track="jee")
        self.other = User.objects.create_user(username="strict_other", email="so@test.com",
                                              password="Password123!", role=User.Roles.STUDENT, exam_track="jee")
        self.exam = Exam.objects.create(
            title="Strict Test", slug="strict-test", category=self.category, creator=self.teacher,
            status=Exam.ExamStatus.PUBLISHED, subject="Physics", duration_minutes=60,
            total_marks=Decimal("12.00"), max_attempts=1, is_free=True, strict_exam_mode=True,
            target_type="jee",
        )
        for i in range(1, 4):
            q = Question.objects.create(category=self.category, subject="Physics",
                                        question_text=f"Q{i}", positive_marks=4, negative_marks=1, creator=self.teacher, target_type="jee")
            for key, corr in [("A", True), ("B", False), ("C", False), ("D", False)]:
                QuestionOption.objects.create(question=q, option_key=key, option_text=key, is_correct=corr)
            ExamQuestion.objects.create(exam=self.exam, question=q, order=i, positive_marks=4, negative_marks=1)

    def _start(self):
        self.client.login(username="strict_student", password="Password123!")
        self.client.post(reverse("exams:exam_start", args=[self.exam.slug]))
        return ExamAttempt.objects.get(exam=self.exam, student=self.student)

    def _event(self, attempt, *, event_type="TAB_SWITCH", client_event_id=None):
        payload = {"event_type": event_type}
        if client_event_id is not None:
            payload["client_event_id"] = client_event_id
        return self.client.post(reverse("exams:exam_log_event", args=[attempt.attempt_uuid]),
                                data=json.dumps(payload), content_type="application/json")

    def test_three_warnings_then_terminate_on_fourth_distinct_event(self):
        attempt = self._start()
        resp = attempt.responses.order_by("order").first()
        opt_a = resp.exam_question.question.options.get(option_key="A")
        resp.selected_option = opt_a; resp.is_visited = True; resp.save()

        states = []
        for n in range(1, 4):
            data = self._event(attempt, client_event_id=f"ep-{n}").json()
            states.append((data["status"], data["tab_switch_count"], data.get("final_warning")))
        self.assertEqual(states, [("warning", 1, False), ("warning", 2, False), ("warning", 3, True)])

        fourth = self._event(attempt, client_event_id="ep-4").json()
        self.assertEqual(fourth["status"], "terminated")
        self.assertEqual(fourth["tab_switch_count"], 4)
        self.assertIn("redirect_url", fourth)

        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TERMINATED)
        self.assertTrue(attempt.is_security_locked)
        self.assertEqual(attempt.tab_switch_warnings, 4)
        resp.refresh_from_db()
        self.assertEqual(resp.selected_option_id, opt_a.id)
        self.assertTrue(resp.is_correct)
        self.assertEqual(float(attempt.score_obtained), 4.0)

    def test_duplicate_and_delayed_events_count_once(self):
        attempt = self._start()
        first = self._event(attempt, client_event_id="same").json()
        self.assertEqual((first["status"], first["tab_switch_count"]), ("warning", 1))
        dup = self._event(attempt, client_event_id="same").json()
        self.assertEqual((dup["status"], dup["tab_switch_count"]), ("duplicate", 1))
        attempt.refresh_from_db()
        self.assertEqual(attempt.tab_switch_warnings, 1)
        self.assertEqual(ExamSecurityLog.objects.filter(attempt=attempt, event_type="TAB_SWITCH").count(), 1)

    def test_non_tab_events_do_not_count_toward_termination(self):
        attempt = self._start()
        for et in ("WINDOW_BLUR", "FULLSCREEN_EXIT", "COPY_ATTEMPT", "RIGHT_CLICK"):
            self.assertEqual(self._event(attempt, event_type=et).status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.tab_switch_warnings, 0)
        self.assertEqual(attempt.anti_cheat_warnings, 4)
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.IN_PROGRESS)
        self.assertEqual(ExamSecurityLog.objects.filter(attempt=attempt, event_type="TAB_SWITCH").count(), 0)
        self.assertTrue(ExamSecurityLog.objects.filter(attempt=attempt, event_type="FULLSCREEN_EXIT").exists())

    def test_refresh_does_not_reset_count_and_blocks_after_termination(self):
        attempt = self._start()
        for n in range(1, 5):
            self._event(attempt, client_event_id=f"e{n}")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TERMINATED)
        session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(session.status_code, 302)
        resp = attempt.responses.first()
        save = self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
                                data=json.dumps({"response_id": resp.id,
                                                 "selected_option_id": resp.exam_question.question.options.first().id,
                                                 "action": "save_next"}),
                                content_type="application/json")
        self.assertEqual(save.json().get("status"), "expired")
        self.assertEqual(self._event(attempt, client_event_id="late").json()["status"], "ignored")
        attempt.refresh_from_db()
        self.assertEqual(attempt.tab_switch_warnings, 4)

    def test_non_strict_exam_tab_switch_never_terminates(self):
        self.exam.strict_exam_mode = False
        self.exam.save(update_fields=["strict_exam_mode"])
        attempt = self._start()
        for n in range(6):
            data = self._event(attempt, client_event_id=f"n{n}").json()
            self.assertEqual(data["status"], "recorded")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.IN_PROGRESS)
        self.assertEqual(attempt.tab_switch_warnings, 0)

    def test_event_after_timeout_is_ignored_and_count_frozen(self):
        attempt = self._start()
        self._event(attempt, client_event_id="a")
        attempt.expires_at = timezone.now() - timedelta(seconds=1)
        attempt.save(update_fields=["expires_at"])
        data = self._event(attempt, client_event_id="b").json()
        self.assertEqual(data["status"], "ignored")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TIMED_OUT)
        self.assertEqual(attempt.tab_switch_warnings, 1)

    def test_other_student_cannot_report_events(self):
        attempt = self._start()
        self.client.login(username="strict_other", password="Password123!")
        self.assertEqual(self._event(attempt, client_event_id="x").status_code, 403)

    def test_strict_mode_is_scoped_to_the_exam(self):
        plain = Exam.objects.create(title="Plain", slug="plain-x", category=self.category,
                                    creator=self.teacher, status=Exam.ExamStatus.PUBLISHED, target_type="jee")
        self.assertFalse(plain.strict_exam_mode)
        self.assertTrue(self.exam.strict_exam_mode)
