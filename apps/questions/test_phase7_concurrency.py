"""Race regressions for no-retakes and active-test answer isolation."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch

from django.db import close_old_connections
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion, ExamResponse
from apps.payments.models import PracticePlan, PracticeUsage
from apps.payments.services import lock_practice_user
from .models import Question, QuestionOption


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PracticePhase7ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user("racephase7", email="racephase7@example.test", password="x", role=User.Roles.STUDENT, exam_track="jee")
        self.plan = PracticePlan.objects.get(is_default=True)
        self.plan.monthly_attempts = 1
        self.plan.attempts_per_test = None
        self.plan.save()
        category = Category.objects.create(name="Race private", slug="race-private")
        self.question = Question.objects.create(category=category, creator=self.user, is_private_practice=True,
                          key_verified=True, subject="Science", question_text="Race question", target_type="jee")
        self.correct = QuestionOption.objects.create(question=self.question, option_key="A", option_text="One", is_correct=True)
        QuestionOption.objects.create(question=self.question, option_key="B", option_text="Two")
        self.exams = []
        for i in range(2):
            exam = Exam.objects.create(title=f"Race {i}", slug=f"race-7-{i}", category=category,
                creator=self.user, is_private_practice=True, status=Exam.ExamStatus.PUBLISHED,
                duration_minutes=30, max_attempts=0, total_marks=4, target_type="jee")
            ExamQuestion.objects.create(exam=exam, question=self.question, order=1)
            self.exams.append(exam)

    def client_for_user(self):
        client = Client()
        client.force_login(self.user)
        return client

    def test_simultaneous_distinct_tests_do_not_consume_old_monthly_attempt_quota(self):
        clients = [self.client_for_user(), self.client_for_user()]
        barrier = Barrier(2)
        def start(index):
            close_old_connections()
            barrier.wait(timeout=10)
            response = clients[index].post(reverse("exams:exam_start", args=[self.exams[index].slug]))
            close_old_connections()
            return response.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(start, (0, 1)))
        self.assertTrue(all(status in (302, 503) for status in results), results)
        self.assertIn(302, results)
        for index, status in enumerate(results):
            if status == 503:  # retry a safe SQLite contention response
                clients[index].post(reverse("exams:exam_start", args=[self.exams[index].slug]))
        self.assertEqual(ExamAttempt.objects.filter(student=self.user).count(), 2)
        self.assertFalse(PracticeUsage.objects.filter(user=self.user).exists())

    def test_simultaneous_same_test_starts_resume_one_attempt(self):
        clients = [self.client_for_user(), self.client_for_user()]
        barrier = Barrier(2)
        url = reverse("exams:exam_start", args=[self.exams[0].slug])
        def start(client):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return client.post(url)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(start, clients))
        self.assertTrue(all(result.status_code in (302, 503) for result in results))
        attempt = self.exams[0].attempts.get(student=self.user)
        deadline = attempt.expires_at
        self.assertEqual(self.exams[0].attempts.count(), 1)
        resumed = clients[0].post(url)
        self.assertRedirects(resumed, reverse("exams:exam_session", args=[attempt.attempt_uuid]), fetch_redirect_response=False)
        attempt.refresh_from_db(); self.assertEqual(attempt.expires_at, deadline)
        attempt.submit_attempt()
        denied = clients[1].post(url)
        self.assertRedirects(denied, reverse("exams:exam_completed", args=[attempt.attempt_uuid]), fetch_redirect_response=False)
        self.assertEqual(self.exams[0].attempts.count(), 1)

    def test_private_single_choice_save_cannot_overwrite_final_submission(self):
        self.client_for_user().post(reverse("exams:exam_start", args=[self.exams[0].slug]))
        attempt = self.exams[0].attempts.get(student=self.user)
        deadline = attempt.expires_at
        answer = attempt.responses.get()
        wrong = self.question.options.get(option_key="B")
        save_client, submit_client = self.client_for_user(), self.client_for_user()
        barrier = Barrier(2)
        save_url = reverse("exams:exam_save_answer", args=[attempt.attempt_uuid])
        submit_url = reverse("exams:exam_submit", args=[attempt.attempt_uuid])

        def call(client, url, payload):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = client.post(url, json.dumps(payload), content_type="application/json")
                return response.status_code, response.json()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            old_save = pool.submit(call, save_client, save_url,
                                   {"response_id": answer.pk, "selected_option_id": wrong.pk, "action": "navigate"})
            final = pool.submit(call, submit_client, submit_url,
                                {"last_response": {"response_id": answer.pk, "selected_option_id": self.correct.pk}})
            saved, submitted = old_save.result(timeout=30), final.result(timeout=30)
        self.assertEqual(submitted[0], 200, submitted)
        self.assertEqual(submitted[1]["status"], "submitted")
        self.assertEqual(saved[0], 200, saved)
        self.assertIn(saved[1]["status"], ("success", "expired"))
        attempt.refresh_from_db(); answer.refresh_from_db()
        self.assertEqual((attempt.status, attempt.expires_at, attempt.score_obtained), ("submitted", deadline, 4))
        self.assertEqual(answer.selected_option_id, self.correct.pk)
        self.assertTrue(answer.is_correct)

    def test_result_read_does_not_overtake_a_start_holding_the_user_lock(self):
        old_exam, exam = self.exams
        old = ExamAttempt.objects.create(student=self.user, exam=old_exam, is_private_practice=True, total_marks=4)
        ExamResponse.objects.create(attempt=old, exam_question=old_exam.exam_questions.get(), selected_option=self.correct, order=1)
        old.submit_attempt()
        start_client = self.client_for_user(); read_client = self.client_for_user()
        acquired, release = Event(), Event()
        def held_lock(user):
            locked = lock_practice_user(user)
            acquired.set()
            if not release.wait(timeout=10):
                raise AssertionError("Test did not release the lock")
            return locked
        def do_start():
            close_old_connections()
            result = start_client.post(reverse("exams:exam_start", args=[exam.slug]))
            close_old_connections()
            return result.status_code
        def do_read():
            close_old_connections()
            response = read_client.get(reverse("questions:practice_result", args=[old.attempt_uuid]))
            close_old_connections()
            return response.status_code
        with patch("apps.payments.services.lock_practice_user", side_effect=held_lock):
            with ThreadPoolExecutor(max_workers=2) as pool:
                started = pool.submit(do_start)
                self.assertTrue(acquired.wait(timeout=10))
                read = pool.submit(do_read)
                # A held SQLite writer lock should produce a safe 503; a
                # row-locking DB may wait and then redirect to the active test.
                Event().wait(0.15)
                release.set()
                self.assertEqual(started.result(timeout=15), 302)
                self.assertIn(read.result(timeout=15), (302, 503))
        after = read_client.get(reverse("questions:practice_result", args=[old.attempt_uuid]))
        self.assertEqual(after.status_code, 302)
        active = ExamAttempt.objects.get(exam=exam, status="in_progress")
        self.assertEqual(after.url, reverse("exams:exam_session", args=[active.attempt_uuid]))
