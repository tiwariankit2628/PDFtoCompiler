"""Conservative JEE/NEET migration against an isolated, disposable database."""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class ExamTargetMigrationTests(TransactionTestCase):
    migrate_from = [("accounts", "0002_user_assigned_categories"),
                    ("exams", "0013_examresponse_numerical_response_and_more"),
                    ("questions", "0017_extractedquestion_multi_scoring_mode_and_more")]
    migrate_to = [("accounts", "0003_user_exam_track_alter_user_target_exam"),
                  ("exams", "0014_exam_target_type"),
                  ("questions", "0018_question_target_type_questionpaperupload_target_type")]

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

    def test_only_explicit_labels_and_source_relationships_are_classified(self):
        User = self.old.get_model("accounts", "User")
        Category = self.old.get_model("exams", "Category")
        Exam = self.old.get_model("exams", "Exam")
        ExamQuestion = self.old.get_model("exams", "ExamQuestion")
        ExamAttempt = self.old.get_model("exams", "ExamAttempt")
        ExamResponse = self.old.get_model("exams", "ExamResponse")
        Paper = self.old.get_model("questions", "QuestionPaperUpload")
        Question = self.old.get_model("questions", "Question")
        Option = self.old.get_model("questions", "QuestionOption")

        teacher = User.objects.create(username="migration-target-t", email="mt-t@example.test", role="TEACHER", target_exam="JEE Main")
        jee = User.objects.create(username="migration-target-j", email="mt-j@example.test", role="STUDENT", target_exam="JEE-Main")
        neet = User.objects.create(username="migration-target-n", email="mt-n@example.test", role="STUDENT", target_exam="NEET-UG")
        ambiguous = User.objects.create(username="migration-target-u", email="mt-u@example.test", role="STUDENT", target_exam="JEE and NEET")
        category = Category.objects.create(name="Migration targets", slug="migration-targets")
        teacher.assigned_categories.add(category)
        exam = Exam.objects.create(title="Legacy practice", slug="migration-practice", category=category, creator=jee,
                                   is_private_practice=True, status="published", duration_minutes=37, target_exam="")
        paper = Paper.objects.create(title="Not used for inference", category=category, uploaded_by=jee,
                                     is_student_private=True, target_exam="JEE Main", practice_exam=exam)
        question = Question.objects.create(category=category, creator=jee, source_paper=paper,
                                           is_private_practice=True, question_text="Old question", subject="Physics")
        option = Option.objects.create(question=question, option_key="A", option_text="Old answer", is_correct=True)
        eq = ExamQuestion.objects.create(exam=exam, question=question, order=1, positive_marks=4, negative_marks=1)
        deadline = timezone.now() + timedelta(minutes=20)
        attempt = ExamAttempt.objects.create(exam=exam, student=jee, status="submitted", score_obtained=4,
                                              total_marks=4, expires_at=deadline)
        response = ExamResponse.objects.create(attempt=attempt, exam_question=eq, selected_option=option,
                                               is_correct=True, marks_awarded=4, order=1)
        other_paper = Paper.objects.create(title="NEET in title is not proof", category=category,
                                           uploaded_by=teacher, target_exam="2025 JEE Main")
        other_question = Question.objects.create(category=category, creator=teacher, source_paper=other_paper,
                                                 question_text="Unclassified source", subject="Biology")
        recognized_exam = Exam.objects.create(title="Advanced", slug="migration-advanced", category=category,
                                              creator=teacher, target_exam="JEE Advanced")
        ambiguous_exam = Exam.objects.create(title="NEET in title alone", slug="migration-ambiguous", category=category,
                                             creator=teacher, target_exam="JEE or NEET")
        ids = (teacher.pk, jee.pk, neet.pk, ambiguous.pk, exam.pk, paper.pk, question.pk,
               other_paper.pk, other_question.pk, recognized_exam.pk, ambiguous_exam.pk, attempt.pk, response.pk, option.pk)

        MigrationExecutor(connection).migrate(self.migrate_to)
        from apps.accounts.models import User as CurrentUser
        from apps.exams.models import Exam as CurrentExam, ExamAttempt as CurrentAttempt, ExamResponse as CurrentResponse
        from apps.questions.models import Question as CurrentQuestion, QuestionPaperUpload as CurrentPaper

        t, j, n, u, ex, p, q, op, oq, recognized, unknown, a, r, opt = ids
        self.assertEqual((CurrentUser.objects.get(pk=j).exam_track, CurrentUser.objects.get(pk=n).exam_track,
                          CurrentUser.objects.get(pk=u).exam_track, CurrentUser.objects.get(pk=t).exam_track),
                         ("jee", "neet", "", ""))
        self.assertEqual(CurrentUser.objects.get(pk=j).target_exam, "JEE-Main")
        self.assertEqual(CurrentUser.objects.get(pk=u).target_exam, "JEE and NEET")
        self.assertTrue(CurrentUser.objects.get(pk=t).assigned_categories.filter(pk=category.pk).exists())
        self.assertEqual(CurrentPaper.objects.get(pk=p).target_type, "jee_main")
        self.assertEqual(CurrentQuestion.objects.get(pk=q).target_type, "jee_main")
        self.assertEqual(CurrentExam.objects.get(pk=ex).target_type, "jee_main")
        self.assertEqual((CurrentPaper.objects.get(pk=op).target_type, CurrentQuestion.objects.get(pk=oq).target_type), ("", ""))
        self.assertEqual(CurrentExam.objects.get(pk=recognized).target_type, "jee_advanced")
        self.assertEqual(CurrentExam.objects.get(pk=unknown).target_type, "")
        saved = CurrentAttempt.objects.get(pk=a)
        self.assertEqual((saved.score_obtained, saved.expires_at, saved.exam.duration_minutes), (Decimal("4"), deadline, 37))
        saved_response = CurrentResponse.objects.get(pk=r)
        self.assertEqual((saved_response.selected_option_id, saved_response.marks_awarded), (opt, Decimal("4")))
