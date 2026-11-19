"""Account deletion cannot accidentally cascade saved assessment records."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.exams.models import Category, Exam, ExamAttempt
from apps.questions.models import Question, QuestionPaperUpload
from .models import User
from .record_safety import has_protected_account_records


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AccountRecordSafetyTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("records-root", "records-root@example.test", "SafePass987!")
        self.client.force_login(self.root)
        self.category = Category.objects.create(name="Records safety", slug="records-safety")

    def user(self, username, role="TEACHER"):
        return User.objects.create_user(username, email=f"{username}@example.test", role=role, exam_track="jee")

    def test_public_assessment_records_and_authorship_block_account_deletion(self):
        owner, author, uploader = self.user("public-owner"), self.user("public-author"), self.user("public-uploader")
        student = self.user("public-student", "STUDENT")
        admin_author = self.user("public-admin", "ADMIN")
        exam = Exam.objects.create(title="Institutional test", slug="protected-institutional-test",
            category=self.category, creator=owner, target_type="jee_main", status="published")
        question = Question.objects.create(category=self.category, creator=author, subject="Math",
                                           target_type="jee_main", question_text="Saved question")
        admin_question = Question.objects.create(category=self.category, creator=admin_author, subject="Math",
                                                 target_type="jee_main", question_text="Admin question")
        paper = QuestionPaperUpload.objects.create(category=self.category, uploaded_by=uploader, title="Saved paper")
        deadline = timezone.now() + timedelta(minutes=15)
        attempt = ExamAttempt.objects.create(exam=exam, student=student, status="submitted", score_obtained=4,
                                             expires_at=deadline)
        for user in (owner, author, uploader, student, admin_author):
            with self.subTest(username=user.username):
                self.assertTrue(has_protected_account_records(user))
                url = reverse("admin:accounts_user_delete", args=[user.pk])
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 403)
                self.assertTrue(User.objects.filter(pk=user.pk).exists())
        self.assertRedirects(self.client.post(reverse("accounts:admin_delete", args=[admin_author.pk])),
                             reverse("accounts:admin_list"), fetch_redirect_response=False)
        self.assertTrue(User.objects.filter(pk=admin_author.pk).exists())
        self.assertTrue(Exam.objects.filter(pk=exam.pk, creator=owner).exists())
        self.assertTrue(Question.objects.filter(pk=question.pk, creator=author).exists())
        self.assertTrue(Question.objects.filter(pk=admin_question.pk, creator=admin_author).exists())
        self.assertTrue(QuestionPaperUpload.objects.filter(pk=paper.pk, uploaded_by=uploader).exists())
        attempt.refresh_from_db()
        self.assertEqual((attempt.score_obtained, attempt.expires_at), (Decimal("4.00"), deadline))

    def test_empty_account_can_still_be_deleted(self):
        empty = self.user("empty-account", "STUDENT")
        self.assertFalse(has_protected_account_records(empty))
        self.assertEqual(self.client.post(reverse("admin:accounts_user_delete", args=[empty.pk]),
                                          {"post": "yes"}).status_code, 302)
        self.assertFalse(User.objects.filter(pk=empty.pk).exists())
