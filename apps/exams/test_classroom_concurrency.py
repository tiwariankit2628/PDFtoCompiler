"""Real concurrent classroom requests against a file-backed test database."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from unittest.mock import patch

from django.db import close_old_connections
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.classroom import ClassroomJoinView
from apps.exams.models import (Category, ClassroomParticipant, ClassroomSession, Exam, ExamAttempt,
                              ExamQuestion, ExamSecurityLog)
from apps.questions.models import AnswerStatus, Question, QuestionOption


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ClassroomConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.teacher = User.objects.create_user("race_teacher", email="race-t@example.test", password="x", role=User.Roles.TEACHER)
        self.student = User.objects.create_user("race_student", email="race-s@example.test", password="x", role=User.Roles.STUDENT, exam_track="jee")
        category = Category.objects.create(name="Race classroom", slug="race-classroom")
        self.exam = Exam.objects.create(title="Race classroom", slug="race-classroom-exam", creator=self.teacher,
            category=category, status=Exam.ExamStatus.PUBLISHED, is_classroom_only=True, duration_minutes=30,
            total_marks=4, shuffle_questions=False, shuffle_options=False, target_type="jee")
        q = Question.objects.create(category=category, creator=self.teacher, is_classroom_snapshot=True,
            answer_status=AnswerStatus.MANUALLY_VERIFIED, subject="Science", question_text="Test?", target_type="jee")
        self.correct = QuestionOption.objects.create(question=q, option_key="A", option_text="Yes", is_correct=True)
        QuestionOption.objects.create(question=q, option_key="B", option_text="No")
        ExamQuestion.objects.create(exam=self.exam, question=q, order=1, positive_marks=4, negative_marks=1)
        self.session = ClassroomSession.create_for(self.exam, self.teacher)
        ClassroomParticipant.objects.create(session=self.session, student=self.student)

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def concurrent(self, calls):
        barrier = Barrier(len(calls))
        def invoke(call):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = call()
                return response.status_code, getattr(response, "url", ""), response.content
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            return list(pool.map(invoke, calls))

    def test_parallel_starts_resume_one_attempt_and_original_timer(self):
        clients = [self.client_for(self.student) for _ in range(5)]
        url = reverse("exams:classroom_take", args=[self.session.pk])
        results = self.concurrent([lambda c=c: c.post(url) for c in clients])
        self.assertTrue(all(status == 302 for status, _, _ in results), results)
        attempt = ExamAttempt.objects.get(classroom_session=self.session, student=self.student)
        self.assertEqual(attempt.responses.count(), 1)
        self.assertEqual(len({url for _, url, _ in results}), 1)
        self.assertEqual(attempt.expires_at - attempt.started_at, timedelta(minutes=self.exam.duration_minutes))

    def test_save_and_final_submit_serialize_without_losing_the_final_choice(self):
        client = self.client_for(self.student)
        client.post(reverse("exams:classroom_take", args=[self.session.pk]))
        attempt = self.session.attempts.get(student=self.student)
        response_id = attempt.responses.get().pk
        payload = {"response_id": response_id, "selected_option_id": self.correct.pk, "action": "navigate"}
        last = {"response_id": response_id, "selected_option_id": self.correct.pk}
        c1, c2 = self.client_for(self.student), self.client_for(self.student)
        results = self.concurrent([
            lambda: c1.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
                            data=json.dumps(payload), content_type="application/json"),
            lambda: c2.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]),
                            data=json.dumps({"last_response": last}), content_type="application/json"),
        ])
        self.assertEqual([result[0] for result in results], [200, 200], results)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.SUBMITTED)
        self.assertEqual(attempt.responses.get().selected_option_id, self.correct.pk)
        self.assertEqual(float(attempt.score_obtained), 4.0)

    def test_start_racing_teacher_end_cannot_leave_a_live_attempt(self):
        student_client, teacher_client = self.client_for(self.student), self.client_for(self.teacher)
        results = self.concurrent([
            lambda: student_client.post(reverse("exams:classroom_take", args=[self.session.pk])),
            lambda: teacher_client.post(reverse("exams:classroom_control", args=[self.session.pk]),
                                        {"action": "end", "confirm": "yes"}),
        ])
        self.assertTrue(all(status == 302 for status, _, _ in results), results)
        self.session.refresh_from_db()
        self.assertTrue(self.session.is_ended)
        self.assertFalse(self.session.attempts.filter(status=ExamAttempt.AttemptStatus.IN_PROGRESS).exists())
        self.assertLessEqual(self.session.attempts.count(), 1)

    def test_duplicate_security_reports_racing_are_counted_once(self):
        self.exam.strict_exam_mode = True
        self.exam.save(update_fields=["strict_exam_mode"])
        self.client_for(self.student).post(reverse("exams:classroom_take", args=[self.session.pk]))
        attempt = self.session.attempts.get(student=self.student)
        clients = [self.client_for(self.student) for _ in range(4)]
        url = reverse("exams:exam_log_event", args=[attempt.attempt_uuid])
        body = json.dumps({"event_type": "TAB_SWITCH", "client_event_id": "same-episode"})
        results = self.concurrent([lambda c=c: c.post(url, data=body, content_type="application/json") for c in clients])
        self.assertTrue(all(status == 200 for status, _, _ in results), results)
        self.assertEqual(sorted(json.loads(content)["status"] for _, _, content in results),
                         ["duplicate", "duplicate", "duplicate", "warning"])
        attempt.refresh_from_db()
        self.assertEqual(attempt.tab_switch_warnings, 1)
        self.assertEqual(ExamSecurityLog.objects.filter(attempt=attempt, client_event_id="same-episode").count(), 1)

    def test_security_termination_racing_submit_preserves_saved_answers(self):
        self.exam.strict_exam_mode = True
        self.exam.save(update_fields=["strict_exam_mode"])
        self.client_for(self.student).post(reverse("exams:classroom_take", args=[self.session.pk]))
        attempt = self.session.attempts.get(student=self.student)
        response = attempt.responses.get()
        response.selected_option = self.correct
        response.save(update_fields=["selected_option"])
        attempt.tab_switch_warnings = 3
        attempt.save(update_fields=["tab_switch_warnings"])
        event_client, submit_client = self.client_for(self.student), self.client_for(self.student)
        results = self.concurrent([
            lambda: event_client.post(reverse("exams:exam_log_event", args=[attempt.attempt_uuid]),
                data=json.dumps({"event_type": "TAB_SWITCH", "client_event_id": "fourth"}), content_type="application/json"),
            lambda: submit_client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]),
                data=json.dumps({"last_response": {"response_id": response.pk,
                                                  "selected_option_id": self.correct.pk}}), content_type="application/json"),
        ])
        self.assertEqual([result[0] for result in results], [200, 200], results)
        attempt.refresh_from_db()
        self.assertIn(attempt.status, (ExamAttempt.AttemptStatus.SUBMITTED, ExamAttempt.AttemptStatus.TERMINATED))
        self.assertEqual(attempt.is_security_locked, attempt.status == ExamAttempt.AttemptStatus.TERMINATED)
        self.assertEqual(attempt.responses.get().selected_option_id, self.correct.pk)
        self.assertEqual(float(attempt.score_obtained), 4.0)
        event_status = json.loads(results[0][2])["status"]
        self.assertEqual(event_status, "terminated" if attempt.is_security_locked else "ignored")
        late = submit_client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
            data=json.dumps({"response_id": response.pk, "action": "clear"}), content_type="application/json")
        self.assertEqual(late.json()["status"], "expired")
        self.assertEqual(attempt.responses.get().selected_option_id, self.correct.pk)

    def test_save_racing_teacher_end_is_saved_before_end_or_rejected_after_it(self):
        self.client_for(self.student).post(reverse("exams:classroom_take", args=[self.session.pk]))
        attempt = self.session.attempts.get(student=self.student)
        response = attempt.responses.get()
        student_client, teacher_client = self.client_for(self.student), self.client_for(self.teacher)
        results = self.concurrent([
            lambda: student_client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
                data=json.dumps({"response_id": response.pk, "selected_option_id": self.correct.pk,
                                 "action": "navigate"}), content_type="application/json"),
            lambda: teacher_client.post(reverse("exams:classroom_control", args=[self.session.pk]),
                {"action": "end", "confirm": "yes"}),
        ])
        self.assertEqual([result[0] for result in results], [200, 302], results)
        saved = json.loads(results[0][2])["status"]
        self.assertIn(saved, ("success", "expired"))
        attempt.refresh_from_db()
        response.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TIMED_OUT)
        self.assertEqual(response.selected_option_id, self.correct.pk if saved == "success" else None)
        self.assertEqual(float(attempt.score_obtained), 4.0 if saved == "success" else 0.0)
        late = student_client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
            data=json.dumps({"response_id": response.pk, "action": "clear"}), content_type="application/json")
        self.assertEqual(late.json()["status"], "expired")
        response.refresh_from_db()
        self.assertEqual(response.selected_option_id, self.correct.pk if saved == "success" else None)

    def test_security_termination_racing_teacher_end_cannot_leave_live_attempt(self):
        self.exam.strict_exam_mode = True
        self.exam.save(update_fields=["strict_exam_mode"])
        self.client_for(self.student).post(reverse("exams:classroom_take", args=[self.session.pk]))
        attempt = self.session.attempts.get(student=self.student)
        attempt.responses.update(selected_option=self.correct)
        attempt.tab_switch_warnings = 3
        attempt.save(update_fields=["tab_switch_warnings"])
        student_client, teacher_client = self.client_for(self.student), self.client_for(self.teacher)
        results = self.concurrent([
            lambda: student_client.post(reverse("exams:exam_log_event", args=[attempt.attempt_uuid]),
                data=json.dumps({"event_type": "TAB_SWITCH", "client_event_id": "fourth"}), content_type="application/json"),
            lambda: teacher_client.post(reverse("exams:classroom_control", args=[self.session.pk]),
                {"action": "end", "confirm": "yes"}),
        ])
        self.assertEqual([result[0] for result in results], [200, 302], results)
        self.session.refresh_from_db()
        attempt.refresh_from_db()
        self.assertTrue(self.session.is_ended)
        self.assertIn(attempt.status, (ExamAttempt.AttemptStatus.TIMED_OUT, ExamAttempt.AttemptStatus.TERMINATED))
        self.assertEqual(attempt.is_security_locked, attempt.status == ExamAttempt.AttemptStatus.TERMINATED)
        self.assertEqual(attempt.responses.get().selected_option_id, self.correct.pk)
        self.assertEqual(float(attempt.score_obtained), 4.0)

    def test_join_rechecks_code_after_concurrent_regeneration(self):
        newcomer = User.objects.create_user("race_newcomer", email="race-new@example.test", role=User.Roles.STUDENT, exam_track="jee")
        student_client, teacher_client = self.client_for(newcomer), self.client_for(self.teacher)
        old_code = self.session.code
        looked_up, resume = Event(), Event()
        original = ClassroomJoinView._join_once

        def delayed_join(view, request, session_id, code):
            looked_up.set()  # the initial code lookup already completed
            if not resume.wait(timeout=10):
                raise AssertionError("Join was not released")
            return original(view, request, session_id, code)

        def join():
            close_old_connections()
            try:
                return student_client.post(reverse("exams:classroom_join"), {"code": old_code})
            finally:
                close_old_connections()

        with patch.object(ClassroomJoinView, "_join_once", delayed_join):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(join)
                try:
                    self.assertTrue(looked_up.wait(timeout=10))
                    changed = teacher_client.post(reverse("exams:classroom_control", args=[self.session.pk]),
                                                  {"action": "regenerate"})
                    self.assertEqual(changed.status_code, 302)
                finally:
                    resume.set()
                response = pending.result(timeout=15)
        self.session.refresh_from_db()
        self.assertNotEqual(self.session.code, old_code)
        self.assertRedirects(response, reverse("exams:classroom_join"), fetch_redirect_response=False)
        self.assertFalse(self.session.participants.filter(student=newcomer).exists())
        joined = student_client.post(reverse("exams:classroom_join"), {"code": self.session.code})
        self.assertRedirects(joined, reverse("exams:classroom_take", args=[self.session.pk]), fetch_redirect_response=False)
        self.assertTrue(self.session.participants.filter(student=newcomer).exists())

    def test_many_students_start_the_same_session_without_a_hardcoded_cap(self):
        students = [User.objects.create_user(f"race-{i}", email=f"race-{i}@example.test", role=User.Roles.STUDENT, exam_track="jee")
                    for i in range(40)]
        ClassroomParticipant.objects.bulk_create([ClassroomParticipant(session=self.session, student=user) for user in students])
        clients = [self.client_for(student) for student in students]
        url = reverse("exams:classroom_take", args=[self.session.pk])
        results = self.concurrent([lambda c=c: c.post(url) for c in clients])
        self.assertTrue(all(status == 302 for status, _, _ in results), results)
        self.assertEqual(self.session.attempts.filter(status=ExamAttempt.AttemptStatus.IN_PROGRESS).count(), len(students))
