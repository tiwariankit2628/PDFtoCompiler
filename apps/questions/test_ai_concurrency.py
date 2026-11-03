"""Ensure a slow AI request doesn't hold exam-start locks or duplicate billing."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from unittest.mock import patch

from django.db import close_old_connections
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion
from apps.payments.models import PracticePlan, PracticeUsage
from .models import AIAnswerSuggestion, ExtractedQuestion, Question, QuestionOption, QuestionPaperUpload


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   AI_ANSWER_ENABLED=True, AI_ANSWER_MODEL="mock-model", GEMINI_API_KEY="test-only")
class AIConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user("ai_race", email="ai_race@example.test", password="x", role=User.Roles.STUDENT)
        plan = PracticePlan.objects.get(is_default=True); plan.monthly_ai_suggestions = 1; plan.save()
        category = Category.objects.create(name="AI Race", slug="ai-race")
        paper = QuestionPaperUpload.objects.create(title="Awaiting AI", category=category, uploaded_by=self.user,
            is_student_private=True, status=QuestionPaperUpload.ProcessingStatus.EXTRACTED)
        self.staged = ExtractedQuestion.objects.create(paper=paper, question_number=1, question_text="Which?",
            options=[{"key": "A", "text": "yes"}, {"key": "B", "text": "no"}])
        bank = Question.objects.create(category=category, creator=self.user, is_private_practice=True,
            question_text="Independent test question", subject="Science")
        QuestionOption.objects.create(question=bank, option_key="A", option_text="yes")
        QuestionOption.objects.create(question=bank, option_key="B", option_text="no")
        self.exam = Exam.objects.create(title="Independent", slug="ai-race-exam", category=category, creator=self.user,
            status=Exam.ExamStatus.PUBLISHED, is_private_practice=True, duration_minutes=30, max_attempts=0)
        ExamQuestion.objects.create(exam=self.exam, question=bank, order=1)

    def test_duplicate_request_is_reused_and_test_starts_while_provider_is_waiting(self):
        clients = [Client(), Client(), Client()]
        for client in clients: client.force_login(self.user)
        entered, release = Event(), Event()
        result = {"option_key": "A", "confidence": Decimal("0.9"), "explanation": "Mock answer",
                  "uncertainty": "", "prompt_tokens": 1, "output_tokens": 1, "estimated_cost_usd": None}
        def slow_suggest(question, images):
            entered.set()
            if not release.wait(12): raise AssertionError("Test did not release provider")
            return result
        def submit():
            close_old_connections()
            try:
                return clients[0].post(reverse("questions:practice_answer_request", args=[self.staged.pk]), {"consent": "yes"}).status_code
            finally:
                close_old_connections()
        with patch("apps.questions.answer_views.GeminiAnswerClient") as provider:
            provider.return_value.calls = 2; provider.return_value.suggest.side_effect = slow_suggest
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(submit)
                try:
                    self.assertTrue(entered.wait(10), "Provider was never entered")
                    duplicate = clients[1].post(reverse("questions:practice_answer_request", args=[self.staged.pk]), {"consent": "yes"})
                    self.assertEqual(duplicate.status_code, 302)
                    started = clients[2].post(reverse("exams:exam_start", args=[self.exam.slug]))
                    self.assertEqual(started.status_code, 302)
                    self.assertEqual(ExamAttempt.objects.filter(exam=self.exam, status="in_progress").count(), 1)
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=15), 302)
            self.assertEqual(provider.return_value.suggest.call_count, 1)
        self.assertEqual(AIAnswerSuggestion.objects.count(), 1)
        self.assertEqual(PracticeUsage.objects.get(user=self.user).ai_suggestions, 1)
        self.staged.refresh_from_db(); self.assertEqual(self.staged.correct_option, "")
        blocked = clients[1].get(reverse("questions:practice_answer_suggestion", args=[self.staged.pk]))
        self.assertIn(str(ExamAttempt.objects.get(exam=self.exam).attempt_uuid), blocked.url)
