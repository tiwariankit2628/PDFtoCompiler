"""Upgrade from the previous schema without rewriting historical records."""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class TypedQuestionMigrationTests(TransactionTestCase):
    """Uses only Django's disposable test DB, never an operator database."""
    migrate_from = [("questions", "0016_flag_pdf_derived_questions"), ("exams", "0012_seed_empty_categories"),
                    ("accounts", "0002_user_assigned_categories")]
    migrate_to = [("questions", "0018_question_target_type_questionpaperupload_target_type"),
                  ("exams", "0014_exam_target_type"),
                  ("accounts", "0003_user_exam_track_alter_user_target_exam")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.addCleanup(self.restore_latest)
        self.old = executor.loader.project_state(self.migrate_from).apps

    def restore_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_upgrade_keeps_keys_answers_scores_expiry_and_assignments(self):
        User = self.old.get_model("accounts", "User")
        Category = self.old.get_model("exams", "Category")
        Exam = self.old.get_model("exams", "Exam")
        ExamQuestion = self.old.get_model("exams", "ExamQuestion")
        ExamAttempt = self.old.get_model("exams", "ExamAttempt")
        ExamResponse = self.old.get_model("exams", "ExamResponse")
        Paper = self.old.get_model("questions", "QuestionPaperUpload")
        Staged = self.old.get_model("questions", "ExtractedQuestion")
        Question = self.old.get_model("questions", "Question")
        Option = self.old.get_model("questions", "QuestionOption")
        teacher = User.objects.create(username="migration_typed_teacher", email="migration-t@example.test", role="TEACHER", password="!")
        student = User.objects.create(username="migration_typed_student", email="migration-s@example.test", role="STUDENT", password="!")
        category = Category.objects.create(name="Typed migration", slug="typed-migration")
        teacher.assigned_categories.add(category)
        paper = Paper.objects.create(title="Existing PDF", category=category, uploaded_by=teacher, status="extracted")
        staged = Staged.objects.create(paper=paper, question_number=109, question_text="Old multiple",
            detected_type="mcq_multiple", options=[{"key": "A", "text": "One"}, {"key": "B", "text": "Two"}],
            correct_option="A", answer_status="manually_verified")
        question = Question.objects.create(category=category, creator=teacher, source_paper=paper, is_pdf_derived=True,
            question_type="mcq_multiple", question_text="Old multiple", answer_status="manually_verified", subject="Physics")
        option = Option.objects.create(question=question, option_key="A", option_text="One", is_correct=True)
        Option.objects.create(question=question, option_key="B", option_text="Two", is_correct=False)
        exam = Exam.objects.create(title="Existing test", slug="existing-typed-migration", category=category,
            creator=teacher, status="published", duration_minutes=45, total_marks=4)
        exam_question = ExamQuestion.objects.create(exam=exam, question=question, order=1, positive_marks=4, negative_marks=1)
        deadline = timezone.now() + timedelta(minutes=32)
        final = ExamAttempt.objects.create(exam=exam, student=student, status="submitted", score_obtained=4,
            percentage=100, correct_count=1, total_marks=4, expires_at=deadline)
        final_response = ExamResponse.objects.create(attempt=final, exam_question=exam_question, selected_option=option,
            is_correct=True, marks_awarded=4, order=1)
        active = ExamAttempt.objects.create(exam=exam, student=student, status="in_progress", total_marks=4, expires_at=deadline)
        active_response = ExamResponse.objects.create(attempt=active, exam_question=exam_question, selected_option=option, order=1)
        ids = (teacher.pk, category.pk, staged.pk, question.pk, option.pk, exam.pk, final.pk, final_response.pk, active.pk, active_response.pk)

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        from apps.accounts.models import User as CurrentUser
        from apps.exams.models import Exam as CurrentExam, ExamAttempt as CurrentAttempt, ExamResponse as CurrentResponse
        from apps.questions.models import ExtractedQuestion as CurrentStaged, Question as CurrentQuestion
        t, c, s, q, opt, ex, fin, finresp, act, actresp = ids
        self.assertTrue(CurrentUser.objects.get(pk=t).assigned_categories.filter(pk=c).exists())
        self.assertEqual(CurrentExam.objects.get(pk=ex).duration_minutes, 45)
        staged_now = CurrentStaged.objects.get(pk=s)
        self.assertEqual((staged_now.detected_type, staged_now.correct_option, staged_now.answer_status),
                         ("mcq_multiple", "A", "manually_verified"))
        self.assertFalse(staged_now.typed_key_verified)
        current_question = CurrentQuestion.objects.get(pk=q)
        self.assertFalse(current_question.typed_key_verified)
        self.assertIsNone(current_question.numeric_key)
        self.assertTrue(current_question.options.get(pk=opt).is_correct)
        final_now = CurrentAttempt.objects.get(pk=fin)
        self.assertEqual((final_now.score_obtained, final_now.correct_count, final_now.partial_count, final_now.expires_at),
                         (Decimal("4"), 1, 0, deadline))
        final_choice = CurrentResponse.objects.get(pk=finresp)
        self.assertEqual((final_choice.selected_option_id, final_choice.is_correct, final_choice.marks_awarded),
                         (opt, True, Decimal("4")))
        self.assertEqual(final_choice.selected_option_ids, [])
        self.assertIsNone(final_choice.numerical_response)
        self.assertIsNone(final_choice.typed_grading_snapshot)
        self.assertFalse(final_now.submit_attempt())
        final_now.refresh_from_db(); self.assertEqual(final_now.score_obtained, Decimal("4"))
        # An active old single-FK response survives the upgrade but cannot be
        # reinterpreted as a complete set or a verified typed key.
        active_now = CurrentAttempt.objects.get(pk=act)
        self.assertEqual(active_now.expires_at, deadline)
        active_now.submit_attempt()
        active_choice = CurrentResponse.objects.get(pk=actresp)
        self.assertEqual(active_choice.selected_option_id, opt)
        self.assertEqual(active_choice.selected_option_ids, [])
        self.assertEqual(active_choice.typed_grading_snapshot["outcome"], "ungraded")
        self.assertFalse(active_choice.key_confirmed_at_grading)
        self.assertEqual(active_now.score_obtained, Decimal("0"))
