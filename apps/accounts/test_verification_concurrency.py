"""Fresh verification status at commercial mutation boundaries (SQLite races)."""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, local
from unittest.mock import patch

import pymupdf as fitz
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from apps.exams.models import (Category, ClassroomActivation, ClassroomParticipant,
                               ClassroomSession, Exam, ExamAttempt, ExamQuestion, SeriesSlot, TestSeries)
from apps.payments.models import CreditUse, PackCheckout
from apps.payments.services import lock_practice_user
from apps.questions.models import ExtractedQuestion, Question, QuestionOption, QuestionPaperUpload
from .models import User


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   PUBLIC_BASE_URL="https://example.test", ALLOW_TEST_PAYMENT_CREDITS=True,
                   RAZORPAY_KEY_ID="rzp_test_Unit123", RAZORPAY_KEY_SECRET="unit_secret")
class VerificationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="verification-race-")
        self.addCleanup(self.root.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=os.path.join(self.root.name, "public"),
            STATIC_ROOT=os.path.join(self.root.name, "static"), PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private")))
        self.category = Category.objects.create(name="Verification races", slug="verification-races")
        self.student = User.objects.create_user("verification-race-student", email="student-race@example.test",
            role="STUDENT", exam_track="jee", is_verified=True, verification_required=True)
        self.teacher = User.objects.create_user("verification-race-teacher", email="teacher-race@example.test",
            role="TEACHER", is_verified=True, verification_required=True)
        self.admin = User.objects.create_user("verification-race-admin", email="admin-race@example.test", role="ADMIN")

    def make_exam(self, kind):
        private = kind == "private"
        owner = self.student if private else self.teacher
        exam = Exam.objects.create(title=f"Verification {kind}", slug=f"verification-{kind}",
            category=self.category, creator=owner, target_type="jee_main", status="published", is_free=True,
            is_private_practice=private, duration_minutes=20, shuffle_questions=False)
        question = Question.objects.create(category=self.category, creator=owner, target_type="jee_main",
            subject="Math", question_text="Choose one", is_private_practice=private,
            is_classroom_snapshot=(kind == "series"), key_verified=private, answer_status="manually_verified")
        QuestionOption.objects.create(question=question, option_key="A", option_text="A", is_correct=True)
        QuestionOption.objects.create(question=question, option_key="B", option_text="B")
        ExamQuestion.objects.create(exam=exam, question=question, order=1, positive_marks=4, negative_marks=1)
        exam.recalculate_marks()
        if kind == "series":
            series, _ = TestSeries.objects.get_or_create(track="jee", defaults={"title": "JEE series"})
            slot, _ = SeriesSlot.objects.get_or_create(series=series, position=1)
            slot.exam = exam
            slot.full_clean()
            slot.save(update_fields=["exam"])
        return exam

    @staticmethod
    def pdf():
        document = fitz.open()
        document.new_page()
        data = document.tobytes()
        document.close()
        return SimpleUploadedFile("race.pdf", data, content_type="application/pdf")

    def race(self, kind, first="profile"):
        is_teacher = kind in ("activation", "teacher_upload", "public_upload", "public_exam", "public_question",
                              "public_duplicate", "public_publish", "public_edit", "public_approve", "public_bulk")
        user = self.admin if kind == "series_publish" else self.teacher if is_teacher else self.student
        exam = self.make_exam(kind) if kind in ("series", "private", "classroom", "activation", "public_duplicate",
                                                "public_publish", "public_edit") else None
        work_data = {}
        slot = None
        if kind in ("series", "private"):
            work_url = reverse("exams:exam_start", args=[exam.slug])
        elif kind == "classroom":
            session = ClassroomSession.create_for(exam, self.teacher, commercial_rules=True)
            ClassroomParticipant.objects.create(session=session, student=user)
            work_url = reverse("exams:classroom_take", args=[session.pk])
        elif kind == "activation":
            work_url = reverse("exams:classroom_session_create")
            work_data = {"exam": exam.pk, "label": "Race class"}
        elif kind in ("student_upload", "teacher_upload", "public_upload"):
            name = {"student_upload": "practice_upload", "teacher_upload": "classroom_paper_upload",
                    "public_upload": "paper_upload"}[kind]
            work_url = reverse(f"questions:{name}")
            work_data = {"title": "Race PDF", "subject": "Math", "target_type": "jee_main", "pdf_file": self.pdf()}
            if is_teacher:
                work_data["category"] = self.category.pk
        elif kind in ("public_exam", "public_edit"):
            work_url = reverse("exams:exam_create" if kind == "public_exam" else "exams:exam_edit",
                               args=[] if kind == "public_exam" else [exam.pk])
            work_data = {"title": "Racing public exam", "category": self.category.pk, "target_type": "jee_main",
                         "duration_minutes": 20, "total_marks": 4, "passing_percentage": 40,
                         "default_positive_marks": 4, "default_negative_marks": 1, "max_attempts": 1,
                         "status": "published", "is_free": "on"}
        elif kind == "public_question":
            work_url = reverse("questions:question_create")
            work_data = {"category": self.category.pk, "target_type": "jee_main", "subject": "Math",
                         "difficulty": "medium", "language": "en", "question_type": "mcq_single",
                         "question_text": "Racing question", "option_text_A": "A", "option_text_B": "B",
                         "correct_option": "A", "positive_marks": 4, "negative_marks": 1}
        elif kind in ("public_duplicate", "public_publish"):
            if kind == "public_publish":
                exam.status = "draft"
                exam.save(update_fields=["status"])
            name = "exam_duplicate" if kind == "public_duplicate" else "exam_toggle_publish"
            work_url = reverse(f"exams:{name}", args=[exam.pk])
        elif kind in ("public_approve", "public_bulk", "series_publish"):
            paper = QuestionPaperUpload.objects.create(title="Existing reviewed paper", category=self.category,
                uploaded_by=user, status="extracted", target_type="jee_main", subject="Math",
                is_classroom_private=kind == "series_publish")
            question = ExtractedQuestion.objects.create(paper=paper, question_number=1, question_text="Reviewed question",
                is_flagged=False, options=[{"key": "A", "text": "A"}, {"key": "B", "text": "B"}],
                positive_marks=4, negative_marks=1)
            if kind == "series_publish":
                series, _ = TestSeries.objects.get_or_create(track="jee", defaults={"title": "JEE series"})
                slot, _ = SeriesSlot.objects.get_or_create(series=series, position=1)
                work_url = reverse("questions:series_paper_publish", args=[paper.pk])
                work_data = {"position": 1, "duration_minutes": 20, "answer_key_text": "1:A",
                             "confirm_review": "on", "acknowledge_images": "on"}
            elif kind == "public_bulk":
                work_url = reverse("questions:paper_bulk_approve", args=[paper.pk])
            else:
                work_url = reverse("questions:extracted_approve", args=[question.pk])
        elif kind == "bank_test":
            question = Question.objects.create(category=self.category, creator=user, target_type="jee_main",
                is_private_practice=True, subject="Math", question_text="Existing bank question")
            QuestionOption.objects.create(question=question, option_key="A", option_text="A", is_correct=True)
            QuestionOption.objects.create(question=question, option_key="B", option_text="B")
            work_url = reverse("questions:practice_bank_test")
            work_data = {"title": "Racing bank test", "duration_minutes": 20, "question_ids": [question.pk]}
        else:
            work_url = reverse("payments:pack_start", args=["pdf"])
            work_data = {"accept_terms": "yes"}

        before_exams = set(Exam.objects.values_list("pk", flat=True))
        before_questions = set(Question.objects.values_list("pk", flat=True))
        before_papers = set(QuestionPaperUpload.objects.values_list("pk", flat=True))
        before_exam_state = (exam.title, exam.status, exam.target_type) if exam else None
        work_client, profile_client = Client(), Client()
        work_client.force_login(user)
        profile_client.force_login(user)
        profile_data = {"first_name": "Race", "last_name": "User", "email": f"new-{kind}@example.test",
                        "target_exam": "JEE" if user.is_student else "", "phone": "", "institute_name": "", "bio": ""}
        acquired, attempted, release = Event(), Event(), Event()
        actor = local()

        def controlled_lock(account):
            name = getattr(actor, "name", "")
            if name == first and not getattr(actor, "held", False):
                locked = lock_practice_user(account)
                actor.held = True
                acquired.set()
                if not release.wait(timeout=15):
                    raise AssertionError("The competing verification request was not released")
                return locked
            if name and name != first:
                attempted.set()
            return lock_practice_user(account)

        def run(name):
            close_old_connections()
            actor.name = name
            try:
                if name == "profile":
                    return profile_client.post(reverse("accounts:profile"), profile_data)
                return work_client.post(work_url, work_data)
            finally:
                close_old_connections()

        second = "work" if first == "profile" else "profile"
        with patch("apps.payments.services.lock_practice_user", controlled_lock), \
             patch("apps.questions.practice_views.lock_practice_user", controlled_lock), \
             patch("apps.payments.pack_views.lock_practice_user", controlled_lock), \
             patch("apps.accounts.verification.send_verification", return_value="sent"), \
             patch("apps.payments.pack_gateway.create_provider_order") as provider_order:
            with ThreadPoolExecutor(max_workers=2) as pool:
                winner = pool.submit(run, first)
                try:
                    self.assertTrue(acquired.wait(timeout=15), "first request did not acquire the account lock")
                    competitor = pool.submit(run, second)
                    self.assertTrue(attempted.wait(timeout=15), "second request did not reach the same account lock")
                finally:
                    release.set()
                results = {first: winner.result(timeout=30), second: competitor.result(timeout=30)}
            provider_order.assert_not_called()
        user.refresh_from_db()
        self.assertTrue(user.needs_email_verification)
        self.assertEqual(results["profile"].url, reverse("accounts:verification_pending"))
        if first == "profile":
            self.assertRedirects(results["work"], reverse("accounts:verification_pending"), fetch_redirect_response=False)
            if exam is not None:
                self.assertFalse(exam.attempts.filter(student=user).exists())
                exam.refresh_from_db()
                self.assertEqual((exam.title, exam.status, exam.target_type), before_exam_state)
            self.assertEqual(set(Exam.objects.values_list("pk", flat=True)), before_exams)
            self.assertEqual(set(Question.objects.values_list("pk", flat=True)), before_questions)
            self.assertEqual(set(QuestionPaperUpload.objects.values_list("pk", flat=True)), before_papers)
            self.assertFalse(CreditUse.objects.filter(user=user).exists())
            self.assertFalse(ClassroomActivation.objects.filter(teacher=user).exists())
            self.assertFalse(PackCheckout.objects.filter(user=user).exists())
            if slot:
                slot.refresh_from_db()
                self.assertIsNone(slot.exam_id)
        else:
            self.assertEqual(kind, "series")
            attempt = exam.attempts.get(student=user)
            self.assertEqual(attempt.expires_at - attempt.started_at, timedelta(minutes=20))
            self.assertEqual(work_client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).status_code, 200)
            attempt.refresh_from_db()
            self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.IN_PROGRESS)
            self.assertEqual(exam.attempts.count(), 1)

    def test_email_change_precedes_series_start(self):
        self.race("series")

    def test_running_series_attempt_survives_later_email_change(self):
        self.race("series", first="work")

    def test_email_change_precedes_private_start(self):
        self.race("private")

    def test_email_change_precedes_classroom_start(self):
        self.race("classroom")

    def test_email_change_precedes_teacher_activation(self):
        self.race("activation")

    def test_email_change_precedes_student_pdf_upload(self):
        self.race("student_upload")

    def test_email_change_precedes_teacher_pdf_upload(self):
        self.race("teacher_upload")

    def test_email_change_precedes_test_checkout(self):
        self.race("checkout")

    def test_email_change_precedes_public_pdf_upload(self):
        self.race("public_upload")

    def test_email_change_precedes_public_exam_creation(self):
        self.race("public_exam")

    def test_email_change_precedes_public_question_creation(self):
        self.race("public_question")

    def test_email_change_precedes_public_test_duplication(self):
        self.race("public_duplicate")

    def test_email_change_precedes_public_test_publication(self):
        self.race("public_publish")

    def test_email_change_precedes_public_test_edit(self):
        self.race("public_edit")

    def test_email_change_precedes_public_question_approval(self):
        self.race("public_approve")

    def test_email_change_precedes_public_bulk_approval(self):
        self.race("public_bulk")

    def test_email_change_precedes_private_bank_test_creation(self):
        self.race("bank_test")

    def test_email_change_precedes_series_publication(self):
        self.race("series_publish")
