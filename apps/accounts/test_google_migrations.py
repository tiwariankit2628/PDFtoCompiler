"""The Google-only schema addition must not rewrite existing commercial data."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class GoogleIdentityMigrationTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        self.latest = executor.loader.graph.leaf_nodes()
        previous = [node for node in self.latest if node[0] != "accounts"] + [
            ("accounts", "0004_user_verification_required_user_verification_sent_at")]
        executor.migrate(previous)
        self.addCleanup(self.restore_latest)
        self.old = executor.loader.project_state(previous).apps

    @staticmethod
    def restore_latest():
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_migration_preserves_accounts_passwords_roles_credits_attempts_and_papers(self):
        User = self.old.get_model("accounts", "User")
        Category = self.old.get_model("exams", "Category")
        Exam = self.old.get_model("exams", "Exam")
        Attempt = self.old.get_model("exams", "ExamAttempt")
        Paper = self.old.get_model("questions", "QuestionPaperUpload")
        Use = self.old.get_model("payments", "CreditUse")
        Checkout = self.old.get_model("payments", "PackCheckout")
        Grant = self.old.get_model("payments", "CreditGrant")
        saved_hash = make_password("SecretTest987!")
        student = User.objects.create(username="before-google", email="student@example.test", password=saved_hash,
            role="STUDENT", exam_track="jee", verification_required=True, is_verified=True,
            free_test_attempted=True, first_name="Original")
        teacher = User.objects.create(username="before-google-teacher", email="teacher@example.test", role="TEACHER")
        category = Category.objects.create(name="Before Google", slug="before-google")
        teacher.assigned_categories.add(category)
        exam = Exam.objects.create(title="Saved test", slug="saved-before-google", creator=teacher,
                                   category=category, status="published", target_type="jee", duration_minutes=37)
        deadline = timezone.now() + timedelta(minutes=20)
        attempt = Attempt.objects.create(exam=exam, student=student, status="in_progress", expires_at=deadline,
                                         score_obtained=Decimal("4.00"))
        paper = Paper.objects.create(title="Preserved PDF", category=category, uploaded_by=student,
            is_student_private=True, target_type="jee", private_pdf_file="papers/kept.pdf", status="extracted")
        use = Use.objects.create(user=student, kind="classroom", is_free=True, resource_key="before-google-free")
        checkout = Checkout.objects.create(user=student, product="pdf", amount_paise=29900, units=20,
                                           currency="INR", status="paid")
        grant = Grant.objects.create(user=student, checkout=checkout, product="pdf", units=20, remaining=13, is_test=True)

        executor = MigrationExecutor(connection)
        executor.migrate(self.latest)
        migrated = executor.loader.project_state(self.latest).apps
        NewUser = migrated.get_model("accounts", "User")
        NewAttempt = migrated.get_model("exams", "ExamAttempt")
        NewPaper = migrated.get_model("questions", "QuestionPaperUpload")
        NewUse = migrated.get_model("payments", "CreditUse")
        NewGrant = migrated.get_model("payments", "CreditGrant")
        Identity = migrated.get_model("accounts", "GoogleIdentity")
        State = migrated.get_model("accounts", "GoogleOAuthState")
        saved = NewUser.objects.get(pk=student.pk)
        self.assertEqual((saved.username, saved.email, saved.password, saved.role, saved.exam_track,
                          saved.verification_required, saved.is_verified, saved.free_test_attempted, saved.first_name),
                         ("before-google", "student@example.test", saved_hash, "STUDENT", "jee", True, True, True, "Original"))
        self.assertTrue(NewUser.objects.get(pk=teacher.pk).assigned_categories.filter(pk=category.pk).exists())
        saved_attempt = NewAttempt.objects.get(pk=attempt.pk)
        self.assertEqual((saved_attempt.student_id, saved_attempt.expires_at, saved_attempt.status, saved_attempt.score_obtained),
                         (student.pk, deadline, "in_progress", Decimal("4.00")))
        self.assertEqual(NewPaper.objects.get(pk=paper.pk).private_pdf_file.name, "papers/kept.pdf")
        self.assertEqual((NewUse.objects.get(pk=use.pk).user_id, NewGrant.objects.get(pk=grant.pk).remaining), (student.pk, 13))
        self.assertEqual(Identity.objects.count(), 0)
        self.assertEqual(State.objects.count(), 0)
