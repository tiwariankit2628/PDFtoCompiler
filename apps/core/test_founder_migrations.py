"""The founder migration adds tracking only; it never changes old records."""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.models import Category, Exam, ExamAttempt
from apps.payments.models import CreditGrant, CreditUse, PackCheckout
from apps.questions.models import QuestionPaperUpload


class FounderMigrationTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([("core", "0001_initial")])
        self.addCleanup(self.restore_latest)

    @staticmethod
    def restore_latest():
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_upgrade_preserves_roles_private_files_purchases_and_original_timers_without_backfill(self):
        founder_candidate = User.objects.create_superuser("legacy-root", "root-legacy@example.test", "OldStrongPassword987!")
        second_root = User.objects.create_superuser("legacy-root-2", "root2-legacy@example.test", "OtherStrongPassword987!")
        student = User.objects.create_user("legacy-student", "student-legacy@example.test", "StudentPassword987!",
                                           role="STUDENT", exam_track="jee")
        old_login = timezone.now() - timedelta(days=90)
        User.objects.filter(pk=student.pk).update(last_login=old_login)
        category = Category.objects.create(name="Founder migration", slug="founder-migration")
        exam = Exam.objects.create(title="Preserved private exam", slug="founder-migration-exam", creator=student,
                                   category=category, is_private_practice=True, duration_minutes=49)
        paper = QuestionPaperUpload.objects.create(title="Preserved PDF", category=category, uploaded_by=student,
                    is_student_private=True, practice_exam=exam, private_pdf_file="papers/legacy-secret.pdf", status="extracted")
        deadline = timezone.now() + timedelta(minutes=31)
        attempt = ExamAttempt.objects.create(student=student, exam=exam, expires_at=deadline, status="in_progress",
                                              score_obtained=Decimal("17.25"))
        checkout = PackCheckout.objects.create(user=student, product="pdf", units=20, amount_paise=29900, status="paid")
        grant = CreditGrant.objects.create(user=student, checkout=checkout, product="pdf", units=20, remaining=13, is_test=True)
        use = CreditUse.objects.create(user=student, kind="pdf", grant=grant, resource_key="migration-use")
        role_snapshot = list(User.objects.order_by("pk").values_list("pk", "role", "is_staff", "is_superuser", "password"))
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        executor.migrate(leaves)
        state = executor.loader.project_state(leaves).apps
        FounderAccess = state.get_model("core", "FounderAccess")
        LoginEvent = state.get_model("core", "LoginEvent")
        Tracking = state.get_model("core", "ActivityTrackingState")
        CurrentUser = state.get_model("accounts", "User")
        CurrentPaper = state.get_model("questions", "QuestionPaperUpload")
        CurrentAttempt = state.get_model("exams", "ExamAttempt")
        CurrentGrant = state.get_model("payments", "CreditGrant")
        CurrentUse = state.get_model("payments", "CreditUse")
        self.assertFalse(FounderAccess.objects.exists())
        self.assertFalse(LoginEvent.objects.exists())
        self.assertIsNotNone(Tracking.objects.get(pk="sign_in").started_at)
        self.assertEqual(list(CurrentUser.objects.order_by("pk").values_list("pk", "role", "is_staff", "is_superuser", "password")), role_snapshot)
        self.assertEqual(CurrentUser.objects.get(pk=student.pk).last_login, old_login)
        self.assertEqual(CurrentPaper.objects.get(pk=paper.pk).private_pdf_file.name, "papers/legacy-secret.pdf")
        updated = CurrentAttempt.objects.get(pk=attempt.pk)
        self.assertEqual((updated.expires_at, updated.status, updated.score_obtained), (deadline, "in_progress", Decimal("17.25")))
        self.assertEqual(CurrentGrant.objects.get(pk=grant.pk).remaining, 13)
        self.assertEqual(CurrentUse.objects.get(pk=use.pk).grant_id, grant.pk)
        self.assertEqual((founder_candidate.role, second_root.role), ("SUPER_ADMIN", "SUPER_ADMIN"))
