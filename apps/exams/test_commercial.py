"""Classroom activations, one attempt per test, and JEE/NEET series isolation."""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.payments.models import CreditUse
from apps.payments.test_helpers import sandbox_grant
from apps.questions.models import AnswerStatus, Question, QuestionOption
from .models import ClassroomActivation, ClassroomParticipant, ClassroomSession, Exam, ExamAttempt, ExamQuestion, TestSeries
from .series import series_ready
from .test_classroom import ClassroomBase


@override_settings(ALLOW_TEST_PAYMENT_CREDITS=True)
class CommercialClassroomTests(ClassroomBase):
    def new_code(self, exam, label="class"):
        self.client.force_login(self.teacher)
        return self.client.post(reverse("exams:classroom_session_create"), {"exam": exam.pk, "label": label})

    def test_activation_rechecks_fresh_account_role_and_verification(self):
        from .activations import ensure_activation
        exam = self.make_exam()
        User.objects.filter(pk=self.teacher.pk).update(verification_required=True, is_verified=False)
        with self.assertRaises(PermissionDenied), transaction.atomic():
            ensure_activation(self.teacher, exam)
        User.objects.filter(pk=self.teacher.pk).update(is_verified=True, role="STUDENT")
        with self.assertRaises(PermissionDenied), transaction.atomic():
            ensure_activation(self.teacher, exam)
        self.assertFalse(ClassroomActivation.objects.filter(exam=exam).exists())
        self.assertFalse(CreditUse.objects.filter(user=self.teacher, kind="activation").exists())

    def test_one_activation_is_shared_across_codes_resume_and_no_retake(self):
        exam = self.make_exam()
        self.new_code(exam, "first")
        first = ClassroomSession.objects.get(exam=exam, label="first")
        self.new_code(exam, "second")
        second = ClassroomSession.objects.get(exam=exam, label="second")
        self.assertNotEqual(first.code, second.code)
        self.assertEqual(first.activation_id, second.activation_id)
        self.assertEqual(first.activation.ends_at - first.activation.starts_at, timedelta(hours=24))
        self.assertEqual(ClassroomActivation.objects.filter(exam=exam).count(), 1)
        self.assertEqual(CreditUse.objects.filter(user=self.teacher, kind="activation", is_free=True).count(), 1)
        self.assertTrue(first.commercial_rules and second.commercial_rules)
        self.join(self.student, first.code)
        self.start(self.student, first)
        attempt = ExamAttempt.objects.get(exam=exam, student=self.student)
        original_expiry = attempt.expires_at
        self.assertEqual(CreditUse.objects.filter(user=self.student, kind="classroom").count(), 1)
        resumed = self.join(self.student, second.code)
        self.assertRedirects(resumed, reverse("exams:exam_session", args=[attempt.attempt_uuid]), fetch_redirect_response=False)
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, original_expiry)
        self.assertEqual(CreditUse.objects.filter(user=self.student, kind="classroom").count(), 1)
        attempt.submit_attempt()
        again = self.join(self.student, second.code)
        self.assertRedirects(again, reverse("exams:exam_completed", args=[attempt.attempt_uuid]), fetch_redirect_response=False)
        self.assertEqual(ExamAttempt.objects.filter(exam=exam, student=self.student).count(), 1)
        self.assertEqual(CreditUse.objects.filter(user=self.student, kind="classroom").count(), 1)
        # Guessing the old public start route cannot bypass the classroom code.
        self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        self.assertEqual(ExamAttempt.objects.filter(exam=exam, student=self.student).count(), 1)

    def test_expiry_reactivation_uses_one_more_credit_and_preserves_active_timer(self):
        exam = self.make_exam()
        self.new_code(exam, "first")
        first = ClassroomSession.objects.get(exam=exam, label="first")
        self.join(self.student, first.code)
        self.start(self.student, first)
        attempt = first.attempts.get(student=self.student)
        deadline = attempt.expires_at
        activation = first.activation
        activation.starts_at = timezone.now() - timedelta(hours=25)
        activation.ends_at = timezone.now() - timedelta(seconds=1)
        activation.save(update_fields=["starts_at", "ends_at"])
        # Existing attempts remain accessible even after the activation expires.
        self.assertEqual(self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, deadline)
        other = User.objects.create_user("new-class", email="new-class@example.test", role="STUDENT", exam_track="jee")
        self.join(other, first.code)
        self.assertFalse(ClassroomParticipant.objects.filter(session=first, student=other).exists())
        denied = self.new_code(exam, "without credit")
        self.assertRedirects(denied, reverse("payments:plans"), fetch_redirect_response=False)
        self.assertEqual(ClassroomActivation.objects.filter(exam=exam).count(), 1)
        grant = sandbox_grant(self.teacher, "activation")
        self.new_code(exam, "reactivated")
        second = ClassroomSession.objects.get(exam=exam, label="reactivated")
        self.assertNotEqual(second.activation_id, first.activation_id)
        grant.refresh_from_db()
        self.assertEqual(grant.remaining, 19)
        self.new_code(exam, "third")
        self.assertEqual(ClassroomSession.objects.get(exam=exam, label="third").activation_id, second.activation_id)
        grant.refresh_from_db()
        self.assertEqual(grant.remaining, 19)
        resumed = self.join(self.student, second.code)
        self.assertRedirects(resumed, reverse("exams:exam_session", args=[attempt.attempt_uuid]), fetch_redirect_response=False)
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, deadline)

    def test_credit_deducted_only_for_new_start_not_join_or_no_questions(self):
        exam = self.make_exam()
        self.new_code(exam)
        session = ClassroomSession.objects.get(exam=exam)
        self.join(self.student, session.code)
        self.assertFalse(CreditUse.objects.filter(user=self.student, kind="classroom").exists())
        self.start(self.student, session)
        self.start(self.student, session)
        self.assertEqual(CreditUse.objects.filter(user=self.student, kind="classroom").count(), 1)
        attempt = session.attempts.get(student=self.student)
        attempt.submit_attempt()
        second_exam = self.make_exam(title="Second classroom")
        grant = sandbox_grant(self.teacher, "activation")
        self.new_code(second_exam)
        second_session = ClassroomSession.objects.get(exam=second_exam)
        self.join(self.student, second_session.code)
        self.assertRedirects(self.start(self.student, second_session), reverse("payments:plans"), fetch_redirect_response=False)
        self.assertFalse(second_exam.attempts.filter(student=self.student).exists())
        student_grant = sandbox_grant(self.student, "classroom")
        self.start(self.student, second_session)
        student_grant.refresh_from_db()
        self.assertEqual(student_grant.remaining, 19)
        self.assertEqual(CreditUse.objects.filter(user=self.student, kind="classroom").count(), 2)

    def test_refunded_teacher_activation_blocks_new_starts_but_not_live_attempts(self):
        exam = self.make_exam()
        self.new_code(exam)
        first = ClassroomSession.objects.get(exam=exam)
        first.activation.ends_at = timezone.now() - timedelta(seconds=1)
        first.activation.starts_at = timezone.now() - timedelta(hours=25)
        first.activation.save(update_fields=["starts_at", "ends_at"])
        grant = sandbox_grant(self.teacher, "activation")
        self.new_code(exam, "paid")
        paid = ClassroomSession.objects.get(exam=exam, label="paid")
        self.join(self.student, paid.code)
        self.start(self.student, paid)
        attempt = paid.attempts.get(student=self.student)
        deadline = attempt.expires_at
        grant.is_revoked = True
        grant.save(update_fields=["is_revoked"])
        self.assertEqual(self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).status_code, 200)
        other = User.objects.create_user("refund-student", email="refund@example.test", role="STUDENT", exam_track="jee")
        self.join(other, paid.code)
        self.assertFalse(paid.participants.filter(student=other).exists())
        self.assertFalse(CreditUse.objects.filter(user=other, kind="classroom").exists())
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, deadline)


@override_settings(ALLOW_TEST_PAYMENT_CREDITS=True)
class CommercialSeriesTests(ClassroomBase):
    def setUp(self):
        super().setUp()
        self.jee = TestSeries.objects.get(track="jee")
        self.neet = TestSeries.objects.get(track="neet")
        self.other = User.objects.create_user("series-neet", email="series-neet@example.test", role="STUDENT", exam_track="neet")

    def add_series_test(self, series, position, target="jee", *, keyed=True):
        exam = Exam.objects.create(title=f"{series.track.upper()} series {position}",
            slug=f"series-{series.track}-{position}", creator=self.teacher, category=self.category,
            status=Exam.ExamStatus.PUBLISHED, target_type=target, duration_minutes=25,
            total_marks=Decimal("4"), is_free=False, shuffle_questions=False, allow_review_answers=False,
            show_result_immediately=False)
        question = Question.objects.create(category=self.category, creator=self.teacher, target_type=target,
            is_classroom_snapshot=True, subject="Science", question_text=f"Question for {series.track} series {position}",
            explanation="SECRET SERIES EXPLANATION", answer_status=(AnswerStatus.MANUALLY_VERIFIED if keyed else AnswerStatus.NEEDS_REVIEW))
        QuestionOption.objects.create(question=question, option_key="A", option_text="Correct choice", is_correct=True)
        QuestionOption.objects.create(question=question, option_key="B", option_text="Wrong choice", is_correct=False)
        ExamQuestion.objects.create(exam=exam, question=question, order=1, positive_marks=4, negative_marks=1)
        slot = series.slots.get(position=position)
        slot.exam = exam
        slot.full_clean()
        slot.save()
        return exam

    def test_series_starts_are_track_scoped_and_independent_of_classroom_credits(self):
        free = self.add_series_test(self.jee, 1, "jee_main")
        locked = self.add_series_test(self.jee, 2, "jee_advanced")
        neet = self.add_series_test(self.neet, 1, "neet")
        self.assertFalse(series_ready("jee"))
        self.client.force_login(self.student)
        page = self.client.get(reverse("exams:series"))
        self.assertContains(page, free.title)
        self.assertNotContains(page, neet.title)
        self.assertEqual(len(page.context["rows"]), 16)
        self.assertEqual(self.client.get(reverse("exams:exam_instructions", args=[neet.slug])).status_code, 404)
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[neet.slug])).status_code, 404)
        self.assertRedirects(self.client.get(reverse("exams:exam_instructions", args=[locked.slug])), reverse("exams:series"), fetch_redirect_response=False)
        self.client.post(reverse("exams:exam_start", args=[locked.slug]))
        self.assertFalse(locked.attempts.exists())
        started = self.client.post(reverse("exams:exam_start", args=[free.slug]))
        attempt = free.attempts.get(student=self.student)
        self.assertRedirects(started, reverse("exams:exam_session", args=[attempt.attempt_uuid]), fetch_redirect_response=False)
        self.assertFalse(CreditUse.objects.filter(user=self.student, kind="classroom").exists())
        self.assertEqual(attempt.expires_at - attempt.started_at, timedelta(minutes=25))
        attempt.responses.update(selected_option=free.questions.first().options.get(option_key="A"))
        attempt.submit_attempt()
        attempt.refresh_from_db()
        self.assertEqual(attempt.score_obtained, Decimal("4.00"))
        self.assertContains(self.client.get(reverse("exams:exam_completed", args=[attempt.attempt_uuid])), "Series result")
        own = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        self.assertContains(own, "Your answer review")
        self.assertContains(own, "Your choice")
        self.assertNotContains(own, "SECRET SERIES EXPLANATION")
        self.assertNotContains(own, "Correct Answer")
        free.allow_review_answers = True
        free.save(update_fields=["allow_review_answers"])
        self.assertContains(self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid])), "SECRET SERIES EXPLANATION")
        retry = self.client.post(reverse("exams:exam_start", args=[free.slug]))
        self.assertRedirects(retry, reverse("exams:exam_completed", args=[attempt.attempt_uuid]), fetch_redirect_response=False)
        self.assertEqual(free.attempts.filter(student=self.student).count(), 1)
        sandbox_grant(self.student, "series_jee")
        started = self.client.post(reverse("exams:exam_start", args=[locked.slug]))
        locked_attempt = locked.attempts.get(student=self.student)
        self.assertRedirects(started, reverse("exams:exam_session", args=[locked_attempt.attempt_uuid]), fetch_redirect_response=False)
        self.assertFalse(CreditUse.objects.filter(user=self.student, kind="classroom").exists())
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("exams:exam_instructions", args=[locked.slug])).status_code, 404)
        self.assertIn(self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid])).status_code, (403, 404))
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[neet.slug])).status_code, 302)
        self.assertTrue(neet.attempts.filter(student=self.other).exists())

    def test_series_does_not_invent_unconfirmed_keys(self):
        exam = self.add_series_test(self.jee, 1, keyed=False)
        self.client.force_login(self.student)
        self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        attempt = exam.attempts.get(student=self.student)
        attempt.responses.update(selected_option=exam.questions.first().options.get(option_key="A"))
        attempt.submit_attempt()
        response = attempt.responses.get()
        self.assertFalse(response.key_confirmed_at_grading)
        self.assertIsNone(response.is_correct)
        result = self.client.get(reverse("exams:student_result_detail", args=[attempt.attempt_uuid]))
        self.assertContains(result, "Answered, ungraded")
        self.assertContains(result, "had no confirmed key")

    def test_complete_series_readiness_and_slot_assignment_guard(self):
        for position in range(1, 17):
            self.add_series_test(self.jee, position, "jee_main")
        self.assertTrue(series_ready("jee"))
        self.assertFalse(series_ready("neet"))
        slot = self.jee.slots.get(position=1)
        different = self.make_exam(title="Cannot replace existing series test")
        slot.exam = different
        with self.assertRaises(ValidationError):
            slot.full_clean()
        # A test used for a class or with historical attempts cannot be
        # silently reclassified into a paid series.
        neet_exam = self.add_series_test(self.neet, 1, "neet")
        old = ExamAttempt.objects.create(exam=different, student=self.student, status=ExamAttempt.AttemptStatus.SUBMITTED)
        open_slot = self.neet.slots.get(position=2)
        open_slot.exam = different
        with self.assertRaises(ValidationError):
            open_slot.full_clean()
        self.assertTrue(ExamAttempt.objects.filter(pk=old.pk).exists())
