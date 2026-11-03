"""Race regressions for quota and active-test answer isolation."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch

from django.db import close_old_connections
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion, ExamResponse
from apps.payments.models import PracticePlan, PracticeUsage
from apps.payments.services import consume
from .models import Question, QuestionOption


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PracticePhase7ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user("racephase7", email="racephase7@example.test", password="x", role=User.Roles.STUDENT)
        self.plan = PracticePlan.objects.get(is_default=True)
        self.plan.monthly_attempts = 1
        self.plan.attempts_per_test = None
        self.plan.save()
        category = Category.objects.create(name="Race private", slug="race-private")
        self.question = Question.objects.create(category=category, creator=self.user, is_private_practice=True,
                          key_verified=True, subject="Science", question_text="Race question")
        self.correct = QuestionOption.objects.create(question=self.question, option_key="A", option_text="One", is_correct=True)
        QuestionOption.objects.create(question=self.question, option_key="B", option_text="Two")
        self.exams = []
        for i in range(2):
            exam = Exam.objects.create(title=f"Race {i}", slug=f"race-7-{i}", category=category,
                creator=self.user, is_private_practice=True, status=Exam.ExamStatus.PUBLISHED,
                duration_minutes=30, max_attempts=0, total_marks=4)
            ExamQuestion.objects.create(exam=exam, question=self.question, order=1)
            self.exams.append(exam)

    def client_for_user(self):
        client = Client()
        client.force_login(self.user)
        return client

    def test_simultaneous_distinct_tests_cannot_overrun_monthly_attempt_quota(self):
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
        self.assertEqual(ExamAttempt.objects.filter(student=self.user).count(), 1)
        self.assertEqual(PracticeUsage.objects.get(user=self.user).attempts, 1)

    def test_result_read_does_not_overtake_a_start_holding_the_user_lock(self):
        self.plan.monthly_attempts = 10; self.plan.save()
        exam = self.exams[0]
        old = ExamAttempt.objects.create(student=self.user, exam=exam, is_private_practice=True, total_marks=4)
        ExamResponse.objects.create(attempt=old, exam_question=exam.exam_questions.get(), selected_option=self.correct, order=1)
        old.submit_attempt()
        start_client = self.client_for_user(); read_client = self.client_for_user()
        acquired, release = Event(), Event()
        def held_consume(user, kind):
            consume(user, kind)
            if kind == "attempts":
                acquired.set()
                if not release.wait(timeout=10):
                    raise AssertionError("Test did not release the lock")
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
        with patch("apps.payments.services.consume", side_effect=held_consume):
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
