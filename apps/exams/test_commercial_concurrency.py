"""Races for one-time allowances, classroom identity and sandbox refunds."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, local
from unittest.mock import patch

from django.db import close_old_connections, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.payments.models import CreditUse, PackPayment
from apps.payments.pack_gateway import apply_refund
from apps.payments.packs import debit
from apps.payments.test_helpers import sandbox_grant
from apps.questions.models import Question, QuestionOption
from .models import ClassroomActivation, ClassroomParticipant, ClassroomSession, Exam, ExamQuestion, SeriesSlot, TestSeries


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"], ALLOW_TEST_PAYMENT_CREDITS=True)
class CommercialConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.student = User.objects.create_user("race-commercial-student", email="rcs@example.test", role="STUDENT", exam_track="jee")
        self.teacher = User.objects.create_user("race-commercial-teacher", email="rct@example.test", role="TEACHER")
        from .models import Category
        self.category = Category.objects.create(name="Commercial race", slug="commercial-race")

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def exam(self, slug):
        exam = Exam.objects.create(title=slug, slug=slug, creator=self.teacher, category=self.category,
            status="published", target_type="jee", duration_minutes=35, shuffle_questions=False, is_free=True)
        q = Question.objects.create(category=self.category, creator=self.teacher, target_type="jee",
                                    question_text="Race question", subject="Math", answer_status="manually_verified")
        QuestionOption.objects.create(question=q, option_key="A", option_text="A", is_correct=True)
        QuestionOption.objects.create(question=q, option_key="B", option_text="B")
        ExamQuestion.objects.create(exam=exam, question=q, order=1, positive_marks=4, negative_marks=1)
        exam.recalculate_marks()
        return exam

    def session(self, exam):
        session = ClassroomSession.create_for(exam, self.teacher, commercial_rules=True)
        ClassroomParticipant.objects.create(student=self.student, session=session)
        return session

    def concurrently(self, jobs):
        barrier = Barrier(len(jobs))
        def run(client, url, data):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return client.post(url, data)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(run, *job) for job in jobs]
            return [f.result(timeout=30) for f in futures]

    def test_two_codes_same_test_create_one_attempt_and_one_free_debit(self):
        exam = self.exam("same-test-two-codes")
        first, second = self.session(exam), self.session(exam)
        clients = [self.client_for(self.student), self.client_for(self.student)]
        results = self.concurrently([(clients[0], reverse("exams:classroom_take", args=[first.pk]), {}),
                                     (clients[1], reverse("exams:classroom_take", args=[second.pk]), {})])
        attempt = exam.attempts.get(student=self.student)
        self.assertEqual([r.status_code for r in results], [302, 302])
        self.assertEqual([r.url for r in results], [reverse("exams:exam_session", args=[attempt.attempt_uuid])] * 2)
        self.assertEqual(attempt.expires_at - attempt.started_at, timedelta(minutes=35))
        self.assertEqual(CreditUse.objects.filter(user=self.student, kind="classroom", is_free=True).count(), 1)
        self.assertEqual(exam.attempts.filter(student=self.student).count(), 1)

    def test_one_free_start_cannot_be_spent_on_two_distinct_tests(self):
        sessions = [self.session(self.exam("race-free-one")), self.session(self.exam("race-free-two"))]
        clients = [self.client_for(self.student), self.client_for(self.student)]
        results = self.concurrently([(clients[i], reverse("exams:classroom_take", args=[sessions[i].pk]), {}) for i in range(2)])
        self.assertTrue(all(r.status_code == 302 for r in results))
        self.assertEqual(sum(r.url == reverse("payments:plans") for r in results), 1)
        self.assertEqual(sum(s.attempts.count() for s in sessions), 1)
        self.assertEqual(CreditUse.objects.filter(user=self.student, kind="classroom", is_free=True).count(), 1)

    def test_last_paid_credit_cannot_be_overspent(self):
        sessions = [self.session(self.exam("race-paid-one")), self.session(self.exam("race-paid-two"))]
        with transaction.atomic():
            debit(self.student, "classroom", "prior-free-use")
        grant = sandbox_grant(self.student, "classroom", remaining=1)
        clients = [self.client_for(self.student), self.client_for(self.student)]
        results = self.concurrently([(clients[i], reverse("exams:classroom_take", args=[sessions[i].pk]), {}) for i in range(2)])
        self.assertTrue(all(r.status_code == 302 for r in results))
        self.assertEqual(sum(r.url == reverse("payments:plans") for r in results), 1)
        self.assertEqual(sum(s.attempts.count() for s in sessions), 1)
        grant.refresh_from_db()
        self.assertEqual(grant.remaining, 0)
        self.assertEqual(CreditUse.objects.filter(user=self.student, kind="classroom", grant=grant).count(), 1)

    def test_two_teacher_codes_share_the_same_single_activation(self):
        exam = self.exam("race-teacher-codes")
        clients = [self.client_for(self.teacher), self.client_for(self.teacher)]
        url = reverse("exams:classroom_session_create")
        results = self.concurrently([(clients[i], url, {"exam": exam.pk, "label": f"group {i}"}) for i in range(2)])
        self.assertTrue(all(r.status_code == 302 and r.url.startswith("/exams/classroom/") for r in results))
        sessions = list(ClassroomSession.objects.filter(exam=exam))
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0].activation_id, sessions[1].activation_id)
        self.assertEqual(ClassroomActivation.objects.filter(exam=exam).count(), 1)
        self.assertEqual(CreditUse.objects.filter(user=self.teacher, kind="activation", is_free=True).count(), 1)

    def refund_race(self, first):
        from apps.payments.services import lock_practice_user as original_lock
        exam = self.exam("series-refund-race")
        exam.questions.update(is_classroom_snapshot=True)  # exclusive, private series fixture
        series, _ = TestSeries.objects.get_or_create(track="jee", defaults={"title": "JEE series"})
        slot, _ = SeriesSlot.objects.get_or_create(series=series, position=2)
        slot.exam = exam
        slot.full_clean()
        slot.save()
        grant = sandbox_grant(self.student, "series_jee")
        payment = PackPayment.objects.create(checkout=grant.checkout, provider_payment_id="pay_RaceRefund", amount_paise=29900, currency="INR")
        start_client = self.client_for(self.student)
        acquired, attempted, release = Event(), Event(), Event()
        actor = local()

        def controlled_lock(user):
            name = getattr(actor, "name", "")
            if name == first and not getattr(actor, "held", False):
                locked = original_lock(user)
                actor.held = True
                acquired.set()
                if not release.wait(timeout=12):
                    raise AssertionError("competing request was not released")
                return locked
            if name and name != first:
                attempted.set()
            return original_lock(user)

        def run(name):
            close_old_connections()
            actor.name = name
            try:
                if name == "start":
                    return start_client.post(reverse("exams:exam_start", args=[exam.slug]))
                return apply_refund(payment.provider_payment_id,
                                    {"id": "rfnd_RaceRefund", "payment_id": payment.provider_payment_id, "amount": 29900})
            finally:
                close_old_connections()

        second = "refund" if first == "start" else "start"
        with patch("apps.payments.services.lock_practice_user", controlled_lock), \
             patch("apps.payments.pack_gateway.lock_practice_user", controlled_lock):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(run, first)
                try:
                    self.assertTrue(acquired.wait(timeout=10), "first request never acquired its lock")
                    second_future = pool.submit(run, second)
                    self.assertTrue(attempted.wait(timeout=10), "second request never reached the same lock")
                finally:
                    release.set()
                first_future.result(timeout=30)
                second_future.result(timeout=30)
        grant.refresh_from_db()
        self.assertTrue(grant.is_revoked)
        if first == "start":
            attempt = exam.attempts.get(student=self.student)
            self.assertEqual(attempt.expires_at - attempt.started_at, timedelta(minutes=35))
            self.assertEqual(start_client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).status_code, 200)
        else:
            self.assertFalse(exam.attempts.exists())

    def test_start_before_refund_preserves_the_running_series_attempt(self):
        self.refund_race("start")

    def test_refund_before_start_blocks_new_series_attempt(self):
        self.refund_race("refund")
