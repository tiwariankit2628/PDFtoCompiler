"""Private, local-only post-submission estimates; synthetic HTTP model stub only.

The tests exercise real loopback HTTP but do not run or claim a real model.
"""
from datetime import timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import uuid

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion, ExamResponse
from .models import PracticeLocalEstimate, Question, QuestionOption


class LocalStub:
    def __init__(self):
        self.calls = []
        self.cloud_disabled = True
        self.status_code = 200
        self.model_present = True
        self.answer = "B"
        self.reason = "A synthetic untrusted explanation."
        self.raw_answer = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, value):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                if status == 302:
                    self.send_header("Location", "https://example.invalid/do-not-follow")
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                outer.calls.append(("GET", self.path, None))
                if self.path == "/api/status":
                    self.respond(outer.status_code, {"cloud": {"disabled": outer.cloud_disabled}})
                elif self.path == "/api/tags":
                    self.respond(200, {"models": ([{"name": "test-local:latest", "size": 12345}] if outer.model_present else [])})
                else:
                    self.respond(404, {})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                payload = json.loads(body)
                outer.calls.append(("POST", self.path, payload))
                if self.path != "/api/generate":
                    self.respond(404, {})
                    return
                answer = outer.raw_answer if outer.raw_answer is not None else json.dumps({"answer": outer.answer, "reason": outer.reason})
                self.respond(200, {"done": True, "done_reason": "stop", "response": answer})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   LOCAL_PRACTICE_AI_ENABLED=True, LOCAL_PRACTICE_AI_MODEL="test-local:latest",
                   LOCAL_PRACTICE_AI_TIMEOUT="5", LOCAL_PRACTICE_AI_MAX_QUESTIONS="300")
class LocalPracticeEstimateTests(TestCase):
    def setUp(self):
        self.stub = LocalStub()
        self.addCleanup(self.stub.close)
        override = override_settings(LOCAL_PRACTICE_AI_PORT=str(self.stub.port))
        override.enable()
        self.addCleanup(override.disable)
        self.owner = User.objects.create_user(username="local-est-owner", email="local-est-owner@example.test", role=User.Roles.STUDENT)
        self.other = User.objects.create_user(username="local-est-other", email="local-est-other@example.test", role=User.Roles.STUDENT)
        self.category = Category.objects.create(name="Local estimate", slug="local-estimate")
        self.exam = Exam.objects.create(title="Private example", slug="private-local-estimate", category=self.category,
                                        creator=self.owner, is_private_practice=True)
        self.attempt = ExamAttempt.objects.create(exam=self.exam, student=self.owner,
                    status=ExamAttempt.AttemptStatus.SUBMITTED, total_marks=Decimal("12"), score_obtained=Decimal("4"), correct_count=1)
        self.response, self.question = self.add_question("What is two plus two?", ["Three", "Four"], selected="B")
        self.visual_response, _ = self.add_question("Which diagram is shown?", ["Circle", "Square"], selected="A")
        self.verified_response, self.verified = self.add_question("What is one plus one?", ["Two", "Three"], selected="A", verified=True)
        self.unanswered_response, _ = self.add_question("What is five plus five?", ["Ten", "Eleven"], selected=None)
        self.client.force_login(self.owner)

    def add_question(self, text, values, *, selected, verified=False):
        question = Question.objects.create(category=self.category, creator=self.owner, is_private_practice=True,
                                           subject="Math", question_text=text, key_verified=verified)
        options = [QuestionOption.objects.create(question=question, option_key=chr(65 + i), option_text=value,
                                                  is_correct=verified and i == 0, order=i)
                   for i, value in enumerate(values)]
        order = self.attempt.responses.count() + 1
        exam_question = ExamQuestion.objects.create(exam=self.exam, question=question, order=order)
        selected_option = next((option for option in options if option.option_key == selected), None)
        response = ExamResponse.objects.create(attempt=self.attempt, exam_question=exam_question,
                                               selected_option=selected_option, order=order,
                                               is_correct=True if verified else None,
                                               marks_awarded=Decimal("4") if verified else Decimal("0"))
        return response, question

    def url(self, name):
        return reverse("questions:" + name, args=[self.attempt.attempt_uuid])

    def start(self, data=None):
        return self.client.post(self.url("practice_estimate_start"), data or {"understand": "on"}, HTTP_ACCEPT="application/json")

    def step(self):
        return self.client.post(self.url("practice_estimate_step"), HTTP_ACCEPT="application/json")

    def test_explicit_start_result_workflow_preserves_verified_scores(self):
        result = self.client.get(self.url("practice_result"))
        self.assertContains(result, "Optional local AI comparison")
        self.assertEqual(self.stub.calls, [])
        refused = self.start({"understand": "no"})
        self.assertEqual(refused.status_code, 400)
        self.assertFalse(PracticeLocalEstimate.objects.exists())
        self.assertEqual(self.start().status_code, 200)
        job = PracticeLocalEstimate.objects.get(attempt=self.attempt)
        self.assertEqual(job.candidate_ids, [self.response.pk, self.visual_response.pk])
        self.assertEqual(self.stub.calls, [])  # starting alone sends nothing
        first = self.step()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), {"processed": 1, "total": 2, "complete": False})
        self.assertEqual([call[:2] for call in self.stub.calls], [("GET", "/api/status"), ("GET", "/api/tags"), ("POST", "/api/generate")])
        payload = self.stub.calls[-1][2]
        self.assertIn("What is two plus two?", payload["prompt"])
        self.assertNotIn("selected_option", payload["prompt"])
        self.assertNotIn("images", payload)
        self.assertEqual(payload["model"], "test-local:latest")
        second = self.step()
        self.assertTrue(second.json()["complete"])
        self.assertEqual(len(self.stub.calls), 3)  # visual question is skipped, not sent
        job.refresh_from_db()
        self.assertEqual(job.results[0]["key"], "B")
        self.assertTrue(job.results[0]["match"])
        self.assertEqual(job.results[1]["state"], "skipped")
        result = self.client.get(self.url("practice_result"))
        self.assertContains(result, "1 estimated correct")
        self.assertContains(result, "not a verified answer")
        self.assertContains(result, "suggests B")
        self.assertContains(result, "No answer key — ungraded")
        self.attempt.refresh_from_db(); self.response.refresh_from_db(); self.question.refresh_from_db()
        self.assertEqual(self.attempt.score_obtained, Decimal("4"))
        self.assertEqual(self.attempt.correct_count, 1)
        self.assertIsNone(self.response.is_correct)
        self.assertEqual(self.response.marks_awarded, Decimal("0"))
        self.assertFalse(self.question.key_verified)
        self.assertFalse(self.question.options.filter(is_correct=True).exists())
        self.assertTrue(self.verified.key_verified)
        self.assertEqual(self.client.post(self.url("practice_estimate_delete")).status_code, 302)
        self.assertFalse(PracticeLocalEstimate.objects.exists())
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.score_obtained, Decimal("4"))

    def test_cloud_must_be_disabled_before_any_question_is_sent_and_can_retry(self):
        self.start(); self.stub.cloud_disabled = False
        response = self.step()
        self.assertEqual(response.status_code, 503)
        self.assertIn("cloud is disabled", response.json()["error"])
        self.assertEqual([call[:2] for call in self.stub.calls], [("GET", "/api/status")])
        job = PracticeLocalEstimate.objects.get()
        self.assertEqual(job.results, [])
        self.assertIsNone(job.request_token)
        self.stub.cloud_disabled = True
        self.assertEqual(self.step().status_code, 200)
        self.assertEqual(self.stub.calls[-1][0], "POST")

    def test_missing_model_and_redirect_do_not_send_question_data(self):
        self.start(); self.stub.model_present = False
        self.assertEqual(self.step().status_code, 503)
        self.assertNotIn("POST", [call[0] for call in self.stub.calls])
        self.stub.calls.clear(); self.stub.status_code = 302
        self.assertEqual(self.step().status_code, 503)
        self.assertEqual([call[1] for call in self.stub.calls], ["/api/status"])

    def test_invalid_model_output_is_uncertain_and_html_is_escaped(self):
        self.start(); self.stub.raw_answer = '{"answer":"Z","reason":"unsafe"}'
        self.assertEqual(self.step().status_code, 200)
        job = PracticeLocalEstimate.objects.get()
        self.assertEqual(job.results[0]["state"], "uncertain")
        self.assertFalse(job.results[0].get("key"))
        # A valid suggestion may still contain untrusted markup; Django must escape it.
        job.delete(); self.start(); self.stub.raw_answer = None; self.stub.reason = "<script>alert(1)</script>"
        self.assertEqual(self.step().status_code, 200)
        page = self.client.get(self.url("practice_result"))
        self.assertContains(page, "&lt;script&gt;alert(1)&lt;/script&gt;", html=False)
        self.assertNotContains(page, "<script>alert(1)</script>")

    def test_owner_and_active_attempt_restrictions(self):
        self.start()
        self.client.force_login(self.other)
        for name in ("practice_estimate_start", "practice_estimate_step", "practice_estimate_delete"):
            self.assertEqual(self.client.post(self.url(name), {"understand": "on"}, HTTP_ACCEPT="application/json").status_code, 404)
        self.client.force_login(self.owner)
        another = Exam.objects.create(title="Active", slug="active-estimate-private", category=self.category,
                                      creator=self.owner, is_private_practice=True)
        ExamAttempt.objects.create(exam=another, student=self.owner, status=ExamAttempt.AttemptStatus.IN_PROGRESS,
                                   expires_at=timezone.now() + timedelta(minutes=5))
        self.assertEqual(self.step().status_code, 423)
        self.assertEqual(self.stub.calls, [])
        self.assertEqual(self.client.get(self.url("practice_result")).status_code, 302)

    def test_inflight_lease_prevents_duplicate_calls_and_can_expire(self):
        self.start(); job = PracticeLocalEstimate.objects.get()
        job.current_response_id = self.response.pk; job.request_token = uuid.uuid4(); job.request_started_at = timezone.now()
        job.save()
        self.assertEqual(self.step().status_code, 409)
        self.assertEqual(self.stub.calls, [])
        job.request_started_at = timezone.now() - timedelta(minutes=2); job.save()
        self.assertEqual(self.step().status_code, 200)
        job.refresh_from_db(); self.assertEqual(len(job.results), 1)

    def test_cloud_or_url_model_names_are_never_accepted(self):
        for model in ("example:cloud", "example:20b-cloud", "example-cloud:latest", "https://example.invalid/model", "host/model"):
            with self.subTest(model=model), override_settings(LOCAL_PRACTICE_AI_MODEL=model):
                self.assertEqual(self.start().status_code, 400)
        self.assertEqual(self.stub.calls, [])
        self.assertFalse(PracticeLocalEstimate.objects.exists())

    def test_a_new_active_attempt_hides_a_comparison_that_finishes_during_it(self):
        from unittest.mock import patch
        self.start()
        another = Exam.objects.create(title="Starting", slug="starting-estimate-private", category=self.category,
                                      creator=self.owner, is_private_practice=True)
        def while_model_is_running(response, options):
            ExamAttempt.objects.create(exam=another, student=self.owner, status=ExamAttempt.AttemptStatus.IN_PROGRESS,
                                       expires_at=timezone.now() + timedelta(minutes=5))
            return {"state": "suggested", "key": "B", "reason": "not disclosed during a test"}
        with patch("apps.questions.practice_estimate_views.estimate_response", side_effect=while_model_is_running):
            result = self.step()
        self.assertEqual(result.status_code, 423)
        self.assertNotIn("reason", result.json())
        self.assertNotIn("key", result.json())
        self.assertEqual(len(PracticeLocalEstimate.objects.get().results), 1)
        self.assertEqual(self.client.get(self.url("practice_result")).status_code, 302)

    def test_plan_quota_charged_once_per_new_comparison_not_per_step_or_resume(self):
        from apps.payments.models import PracticePlan, PracticeUsage
        from apps.payments.services import month_start
        plan = PracticePlan.objects.get(is_default=True)
        plan.monthly_ai_suggestions = 1; plan.save(update_fields=["monthly_ai_suggestions"])
        self.assertEqual(self.start().status_code, 200)
        self.assertEqual(self.start().status_code, 200)
        self.assertEqual(self.step().status_code, 200)
        self.assertEqual(self.step().status_code, 200)
        self.assertEqual(PracticeUsage.objects.get(user=self.owner, period=month_start()).ai_suggestions, 1)
        self.client.post(self.url("practice_estimate_delete"))
        rejected = self.start()
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("limit", rejected.json()["error"])
        self.assertFalse(PracticeLocalEstimate.objects.exists())

    def test_180_answered_questions_can_resume_without_changing_results(self):
        from unittest.mock import patch
        for number in range(178):
            self.add_question(f"Synthetic question {number + 1}?", ["First", "Second"], selected="B")
        self.assertEqual(self.start().json()["total"], 180)
        # Workflow/scale test only; this is not a real model or accuracy test.
        with patch("apps.questions.practice_estimate_views.estimate_response", return_value={"state": "suggested", "key": "B", "reason": "synthetic"}):
            for index in range(180):
                if index == 90:
                    self.assertEqual(self.start().json()["processed"], 90)  # resume does not restart
                result = self.step()
                self.assertEqual(result.status_code, 200, result.content)
        self.assertTrue(result.json()["complete"])
        job = PracticeLocalEstimate.objects.get(attempt=self.attempt)
        self.assertEqual(len(job.results), 180)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.score_obtained, Decimal("4"))
        self.assertEqual(self.attempt.correct_count, 1)
        self.assertFalse(self.exam.questions.filter(key_verified=True).exclude(pk=self.verified.pk).exists())

    def test_plain_table_text_is_sent_locally_but_merged_table_is_skipped(self):
        def cell(text, colspan=1):
            return {"text": text, "colspan": colspan, "rowspan": 1, "header": False}
        self.question.question_text = "Select the value in the table [[table:1]]"
        self.question.tables = [{"columns": 2, "rows": [[cell("A"), cell("2")], [cell("B"), cell("4")]]}]
        self.question.save(update_fields=["question_text", "tables"])
        self.start(); self.assertEqual(self.step().status_code, 200)
        payload = self.stub.calls[-1][2]
        self.assertIn("A | 2", payload["prompt"])
        self.assertIn("B | 4", payload["prompt"])
        self.assertNotIn("images", payload)
        PracticeLocalEstimate.objects.all().delete(); self.stub.calls.clear()
        self.question.tables = [{"columns": 2, "rows": [[cell("Merged heading", 2)], [cell("B"), cell("4")]]}]
        self.question.save(update_fields=["tables"])
        self.start(); self.assertEqual(self.step().status_code, 200)
        job = PracticeLocalEstimate.objects.get()
        self.assertEqual(job.results[0]["state"], "skipped")
        self.assertIn("merged", job.results[0]["reason"])
        self.assertEqual(self.stub.calls, [])

    def test_deleting_attempt_removes_only_its_private_estimate(self):
        self.start(); self.assertEqual(PracticeLocalEstimate.objects.count(), 1)
        self.attempt.delete()
        self.assertFalse(PracticeLocalEstimate.objects.exists())
        self.assertTrue(Question.objects.filter(pk=self.question.pk).exists())
