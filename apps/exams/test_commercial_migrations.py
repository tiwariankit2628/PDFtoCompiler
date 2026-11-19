"""Exercise the commercial cutover on representative pre-upgrade records."""
from datetime import date, timedelta
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class CommercialMigrationTests(TransactionTestCase):
    migrate_from = [("accounts", "0003_user_exam_track_alter_user_target_exam"),
                    ("exams", "0014_exam_target_type"),
                    ("questions", "0018_question_target_type_questionpaperupload_target_type"),
                    ("payments", "0007_practiceplan_monthly_ai_suggestions_and_more")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.addCleanup(self.restore_latest)
        self.old = executor.loader.project_state(self.migrate_from).apps

    @staticmethod
    def restore_latest():
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_users_papers_attempts_timers_keys_and_payments_survive(self):
        User = self.old.get_model("accounts", "User")
        Category = self.old.get_model("exams", "Category")
        Exam = self.old.get_model("exams", "Exam")
        Session = self.old.get_model("exams", "ClassroomSession")
        Participant = self.old.get_model("exams", "ClassroomParticipant")
        Attempt = self.old.get_model("exams", "ExamAttempt")
        Response = self.old.get_model("exams", "ExamResponse")
        ExamQuestion = self.old.get_model("exams", "ExamQuestion")
        Question = self.old.get_model("questions", "Question")
        Option = self.old.get_model("questions", "QuestionOption")
        Paper = self.old.get_model("questions", "QuestionPaperUpload")
        ExtractedQuestion = self.old.get_model("questions", "ExtractedQuestion")
        Usage = self.old.get_model("payments", "PracticeUsage")
        Plan = self.old.get_model("payments", "PracticePlan")
        Checkout = self.old.get_model("payments", "Checkout")
        Payment = self.old.get_model("payments", "Payment")
        Entitlement = self.old.get_model("payments", "PracticeEntitlement")

        teacher = User.objects.create(username="migration-commercial-teacher", email="mct@example.test", role="TEACHER", is_verified=False)
        student = User.objects.create(username="migration-commercial-student", email="mcs@example.test", role="STUDENT", exam_track="jee", is_verified=False)
        other = User.objects.create(username="migration-commercial-other", email="mco@example.test", role="STUDENT", exam_track="jee", is_verified=True)
        usage_only = User.objects.create(username="migration-commercial-usage", email="mcu@example.test", role="STUDENT", exam_track="neet")
        new = User.objects.create(username="migration-commercial-new", email="mcn@example.test", role="STUDENT", exam_track="jee")
        category = Category.objects.create(name="Migration commercial", slug="migration-commercial")
        exam = Exam.objects.create(title="Legacy class", slug="migration-commercial-class", category=category,
            creator=teacher, status="published", target_type="jee", duration_minutes=41,
            show_result_immediately=False, allow_review_answers=False)
        question = Question.objects.create(category=category, creator=teacher, subject="Math", target_type="jee",
                                            question_text="Preserve this question", answer_status="manually_verified")
        option = Option.objects.create(question=question, option_key="A", option_text="Preserve this key", is_correct=True)
        eq = ExamQuestion.objects.create(exam=exam, question=question, order=1, positive_marks=4, negative_marks=1)
        first = Session.objects.create(exam=exam, teacher=teacher, code="LEGACY123", allow_retakes=True, results_released=False)
        second = Session.objects.create(exam=exam, teacher=teacher, code="LEGACY456", allow_retakes=True, results_released=False)
        Participant.objects.create(session=first, student=student)
        deadline = timezone.now() + timedelta(minutes=26)
        active = Attempt.objects.create(exam=exam, student=student, classroom_session=first,
                                        status="in_progress", expires_at=deadline, total_marks=4)
        old_result = Attempt.objects.create(exam=exam, student=other, classroom_session=second,
                                            status="submitted", score_obtained=4, percentage=100, total_marks=4)
        response = Response.objects.create(attempt=old_result, exam_question=eq, selected_option=option,
            order=1, is_correct=True, marks_awarded=4, correct_option_key_at_grading="A", key_confirmed_at_grading=True)
        paper = Paper.objects.create(title="Legacy private PDF", category=category, uploaded_by=student,
            is_student_private=True, target_type="jee", private_pdf_file="papers/legacy.pdf", status="extracted")
        staged = ExtractedQuestion.objects.create(paper=paper, question_number=7, question_text="Edited source work")
        Usage.objects.create(user=usage_only, period=date(2025, 12, 1), pdf_uploads=1, attempts=2)
        plan = Plan.objects.create(name="Legacy paid", slug="migration-legacy-paid", billing="one_time", price_paise=5000)
        checkout = Checkout.objects.create(user=student, plan=plan, billing="one_time", amount_paise=5000,
                                           status="paid", provider_order_id="order_LegacyMigration")
        payment = Payment.objects.create(checkout=checkout, provider_payment_id="pay_LegacyMigration", amount_paise=5000, currency="INR")
        until = timezone.now() + timedelta(days=3)
        entitlement = Entitlement.objects.create(user=student, plan=plan, checkout=checkout,
                                                 starts_at=timezone.now() - timedelta(days=1), valid_until=until)
        ids = {"teacher": teacher.pk, "student": student.pk, "other": other.pk, "usage": usage_only.pk, "new": new.pk,
               "exam": exam.pk, "first": first.pk, "second": second.pk, "active": active.pk, "old_result": old_result.pk,
               "response": response.pk, "option": option.pk, "paper": paper.pk, "staged": staged.pk,
               "checkout": checkout.pk, "payment": payment.pk, "entitlement": entitlement.pk}

        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        executor.migrate(leaves)
        migrated = executor.loader.project_state(leaves).apps
        CurrentUser = migrated.get_model("accounts", "User")
        CurrentSession = migrated.get_model("exams", "ClassroomSession")
        CurrentAttempt = migrated.get_model("exams", "ExamAttempt")
        CurrentResponse = migrated.get_model("exams", "ExamResponse")
        CurrentPaper = migrated.get_model("questions", "QuestionPaperUpload")
        CurrentStaged = migrated.get_model("questions", "ExtractedQuestion")
        CreditUse = migrated.get_model("payments", "CreditUse")
        CurrentEntitlement = migrated.get_model("payments", "PracticeEntitlement")
        CurrentPayment = migrated.get_model("payments", "Payment")
        TestSeries = migrated.get_model("exams", "TestSeries")

        self.assertFalse(CurrentUser.objects.get(pk=ids["student"]).verification_required)
        self.assertFalse(CurrentUser.objects.get(pk=ids["student"]).is_verified)
        self.assertTrue(CurrentUser.objects.get(pk=ids["other"]).is_verified)
        self.assertEqual(CurrentAttempt.objects.get(pk=ids["active"]).expires_at, deadline)
        self.assertEqual(CurrentAttempt.objects.get(pk=ids["active"]).status, "in_progress")
        self.assertEqual(CurrentAttempt.objects.get(pk=ids["old_result"]).score_obtained, Decimal("4.00"))
        saved_response = CurrentResponse.objects.get(pk=ids["response"])
        self.assertEqual((saved_response.selected_option_id, saved_response.correct_option_key_at_grading,
                          saved_response.marks_awarded), (ids["option"], "A", Decimal("4.00")))
        self.assertEqual(CurrentPaper.objects.get(pk=ids["paper"]).private_pdf_file.name, "papers/legacy.pdf")
        self.assertEqual(CurrentStaged.objects.get(pk=ids["staged"]).question_text, "Edited source work")
        saved_first = CurrentSession.objects.get(pk=ids["first"])
        saved_second = CurrentSession.objects.get(pk=ids["second"])
        self.assertEqual(saved_first.activation_id, saved_second.activation_id)
        self.assertTrue(saved_first.activation.is_legacy)
        self.assertEqual(saved_first.activation.ends_at - saved_first.activation.starts_at, timedelta(hours=24))
        self.assertFalse(saved_first.commercial_rules)
        self.assertTrue(saved_first.allow_retakes)  # retained, but new starts enforce no retakes
        self.assertFalse(saved_first.results_released)
        self.assertEqual(CurrentEntitlement.objects.get(pk=ids["entitlement"]).valid_until, until)
        self.assertEqual(CurrentPayment.objects.get(pk=ids["payment"]).provider_payment_id, "pay_LegacyMigration")
        self.assertTrue(CreditUse.objects.filter(user_id=ids["student"], kind="pdf", is_free=True).exists())
        self.assertTrue(CreditUse.objects.filter(user_id=ids["usage"], kind="pdf", is_free=True).exists())
        self.assertTrue(CreditUse.objects.filter(user_id=ids["student"], kind="classroom", is_free=True).exists())
        self.assertTrue(CreditUse.objects.filter(user_id=ids["other"], kind="classroom", is_free=True).exists())
        self.assertTrue(CreditUse.objects.filter(user_id=ids["teacher"], kind="activation", is_free=True).exists())
        self.assertFalse(CreditUse.objects.filter(user_id=ids["new"]).exists())
        self.assertEqual(TestSeries.objects.count(), 2)
        self.assertTrue(all(series.slots.count() == 16 and not series.slots.exclude(exam__isnull=True).exists()
                            for series in TestSeries.objects.all()))
