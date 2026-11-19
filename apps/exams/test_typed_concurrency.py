"""Typed and mixed public tests serialize saves, submission and expiry."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.db import close_old_connections
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion
from apps.questions.models import AnswerStatus, Question, QuestionOption


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class TypedConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.teacher = User.objects.create_user("typed_race_t", email="typed-race-t@example.test", password="x", role="TEACHER")
        self.student = User.objects.create_user("typed_race_s", email="typed-race-s@example.test", password="x", role="STUDENT", exam_track="jee")
        category = Category.objects.create(name="Typed race", slug="typed-race")
        self.exam = Exam.objects.create(title="Mixed typed race", slug="mixed-typed-race", creator=self.teacher,
            category=category, status="published", is_free=True, duration_minutes=30,
            shuffle_questions=False, shuffle_options=False, target_type="jee")
        multi = Question.objects.create(category=category, creator=self.teacher, question_text="Select both",
            question_type="mcq_multiple", typed_key_verified=True, answer_status=AnswerStatus.MANUALLY_VERIFIED,
            subject="Math", multi_scoring_mode="exact", target_type="jee")
        self.options = [QuestionOption.objects.create(question=multi, option_key=k, option_text=k, is_correct=k in "AC") for k in "ABC"]
        single = Question.objects.create(category=category, creator=self.teacher, question_text="Select one", subject="Math", target_type="jee")
        self.single = QuestionOption.objects.create(question=single, option_key="A", option_text="A", is_correct=True)
        QuestionOption.objects.create(question=single, option_key="B", option_text="B")
        ExamQuestion.objects.create(exam=self.exam, question=multi, order=1, positive_marks=4, negative_marks=1)
        ExamQuestion.objects.create(exam=self.exam, question=single, order=2, positive_marks=4, negative_marks=1)
        self.exam.recalculate_marks()
        self.client_for(self.student).post(reverse("exams:exam_start", args=[self.exam.slug]))
        self.attempt = self.exam.attempts.get(student=self.student)
        self.multi, self.ordinary = list(self.attempt.responses.order_by("order"))

    def client_for(self, user):
        client = Client(); client.force_login(user); return client

    def concurrently(self, *calls):
        barrier = Barrier(len(calls))
        def run(call):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = call()
                return response.status_code, response.json()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            return list(pool.map(run, calls))

    def test_typed_save_and_submit_never_lose_the_final_set(self):
        save_client, submit_client = self.client_for(self.student), self.client_for(self.student)
        ids = [self.options[0].pk, self.options[2].pk]
        answer = {"response_id": self.multi.pk, "selected_option_ids": ids, "selected_option_id": None}
        results = self.concurrently(
            lambda: save_client.post(reverse("exams:exam_save_answer", args=[self.attempt.attempt_uuid]),
                json.dumps({**answer, "action": "navigate"}), content_type="application/json"),
            lambda: submit_client.post(reverse("exams:exam_submit", args=[self.attempt.attempt_uuid]),
                json.dumps({"last_response": answer}), content_type="application/json"),
        )
        self.assertEqual([status for status, _ in results], [200, 200], results)
        self.attempt.refresh_from_db(); self.multi.refresh_from_db()
        self.assertEqual(self.attempt.status, ExamAttempt.AttemptStatus.SUBMITTED)
        self.assertEqual(self.multi.selected_option_ids, ids)
        self.assertEqual(self.multi.typed_grading_snapshot["answer"], ["A", "C"])
        self.assertEqual(self.multi.typed_grading_snapshot["outcome"], "correct")
        self.assertEqual(self.attempt.score_obtained, 4)

    def test_single_save_in_mixed_exam_is_serialized_with_submit(self):
        save_client, submit_client = self.client_for(self.student), self.client_for(self.student)
        answer = {"response_id": self.ordinary.pk, "selected_option_id": self.single.pk}
        results = self.concurrently(
            lambda: save_client.post(reverse("exams:exam_save_answer", args=[self.attempt.attempt_uuid]),
                json.dumps({**answer, "action": "navigate"}), content_type="application/json"),
            lambda: submit_client.post(reverse("exams:exam_submit", args=[self.attempt.attempt_uuid]),
                json.dumps({"last_response": answer}), content_type="application/json"),
        )
        self.assertEqual([status for status, _ in results], [200, 200], results)
        self.attempt.refresh_from_db(); self.ordinary.refresh_from_db()
        self.assertEqual(self.attempt.status, ExamAttempt.AttemptStatus.SUBMITTED)
        self.assertEqual(self.ordinary.selected_option_id, self.single.pk)
        self.assertTrue(self.ordinary.is_correct)
        self.assertEqual(self.attempt.score_obtained, 4)

    def test_timeout_on_stale_deleted_typed_attempt_does_not_recreate_it(self):
        self.attempt.expires_at = timezone.now() - timedelta(seconds=1)
        self.attempt.save(update_fields=["expires_at"])
        pk = self.attempt.pk
        ExamAttempt.objects.filter(pk=pk).delete()
        self.assertFalse(self.attempt.auto_timeout_if_expired())
        self.assertFalse(ExamAttempt.objects.filter(pk=pk).exists())

    def test_timeout_sync_submit_and_late_save_do_not_regrade_or_erase_saved_set(self):
        client = self.client_for(self.student)
        ids = [self.options[0].pk, self.options[2].pk]
        save_url = reverse("exams:exam_save_answer", args=[self.attempt.attempt_uuid])
        self.assertEqual(client.post(save_url, json.dumps({"response_id": self.multi.pk, "selected_option_ids": ids}),
                                     content_type="application/json").json()["status"], "success")
        deadline = timezone.now() - timedelta(seconds=1)
        self.attempt.expires_at = deadline; self.attempt.save(update_fields=["expires_at"])
        save_client, sync_client, submit_client = [self.client_for(self.student) for _ in range(3)]
        results = self.concurrently(
            lambda: save_client.post(save_url, json.dumps({"response_id": self.multi.pk,
                "selected_option_ids": [self.options[1].pk]}), content_type="application/json"),
            lambda: sync_client.get(reverse("exams:exam_sync_status", args=[self.attempt.attempt_uuid])),
            lambda: submit_client.post(reverse("exams:exam_submit", args=[self.attempt.attempt_uuid]),
                json.dumps({"is_timeout": True}), content_type="application/json"),
        )
        self.assertEqual([status for status, _ in results], [200, 200, 200], results)
        self.assertEqual([body["status"] for _, body in results], ["expired", "expired", "submitted"])
        self.attempt.refresh_from_db(); self.multi.refresh_from_db()
        self.assertEqual((self.attempt.status, self.attempt.expires_at, self.attempt.score_obtained),
                         (ExamAttempt.AttemptStatus.TIMED_OUT, deadline, 4))
        self.assertEqual(self.multi.selected_option_ids, ids)
        self.assertEqual(self.multi.typed_grading_snapshot["answer"], ["A", "C"])
        self.assertEqual(self.multi.typed_grading_snapshot["outcome"], "correct")

    def test_typed_public_timer_is_server_authoritative(self):
        client = self.client_for(self.student)
        submit = reverse("exams:exam_submit", args=[self.attempt.attempt_uuid])
        save = reverse("exams:exam_save_answer", args=[self.attempt.attempt_uuid])
        early = client.post(submit, json.dumps({"is_timeout": True}), content_type="application/json")
        self.assertEqual(early.json()["status"], "active")
        self.attempt.refresh_from_db(); self.assertEqual(self.attempt.status, "in_progress")
        ids = [self.options[0].pk, self.options[2].pk]
        client.post(save, json.dumps({"response_id": self.multi.pk, "selected_option_ids": ids}), content_type="application/json")
        deadline = timezone.now() - timedelta(seconds=1)
        self.attempt.expires_at = deadline; self.attempt.save(update_fields=["expires_at"])
        result = client.post(submit, json.dumps({"last_response": {"response_id": self.multi.pk,
            "selected_option_id": None, "selected_option_ids": [self.options[1].pk]}}), content_type="application/json")
        self.assertEqual(result.json()["status"], "submitted")
        self.attempt.refresh_from_db(); self.multi.refresh_from_db()
        self.assertEqual(self.attempt.status, ExamAttempt.AttemptStatus.TIMED_OUT)
        self.assertEqual(self.attempt.expires_at, deadline)
        self.assertEqual(self.multi.selected_option_ids, ids)
        self.assertEqual(self.multi.typed_grading_snapshot["outcome"], "correct")
        self.assertEqual(client.post(save, json.dumps({"response_id": self.multi.pk, "action": "clear"}),
                                     content_type="application/json").json()["status"], "expired")
        self.multi.refresh_from_db(); self.assertEqual(self.multi.selected_option_ids, ids)
