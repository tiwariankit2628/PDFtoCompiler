"""Optional real-browser coverage of typed authoring, CBT and result review."""
import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from decimal import Decimal
from datetime import timedelta
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.models import Category, ClassroomSession, Exam, ExamAttempt, ExamQuestion, ExamResponse
from apps.questions.models import ExtractedQuestion, Question, QuestionOption, QuestionPaperUpload


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium typed-question workflow")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class TypedQuestionBrowserTests(StaticLiveServerTestCase):
    def test_student_and_teacher_review_create_resume_and_grade_typed_questions(self):
        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install tests/browser dependencies first.")
        with tempfile.TemporaryDirectory() as root, override_settings(
            MEDIA_ROOT=os.path.join(root, "media"), PRIVATE_PRACTICE_ROOT=os.path.join(root, "private")
        ):
            password = "TypedBrowserPass987!"
            student = User.objects.create_user("typed_browser_s", email="typed-browser-s@example.test", password=password, role="STUDENT", exam_track="jee")
            teacher = User.objects.create_user("typed_browser_t", email="typed-browser-t@example.test", password=password, role="TEACHER")
            category = Category.objects.create(name="Typed browser", slug="typed-browser")
            def make_paper(owner, **flags):
                paper = QuestionPaperUpload.objects.create(title="Typed browser paper", category=category, uploaded_by=owner,
                    status=QuestionPaperUpload.ProcessingStatus.EXTRACTED, image_first=False, **flags, target_type="jee")
                result = []
                for number, kind in ((1, "mcq_multiple"), (2, "numerical"), (3, "mcq_single")):
                    result.append(ExtractedQuestion.objects.create(paper=paper, question_number=number,
                        question_text=f"Browser question {number}", detected_type=kind, is_flagged=False,
                        options=[] if kind == "numerical" else [{"key": key, "text": f"Choice {key}"} for key in "ABCD"],
                        explanation=f"Checked explanation {number}", positive_marks=4, negative_marks=1))
                return paper, result
            practice, sq = make_paper(student, is_student_private=True)
            classroom, tq = make_paper(teacher, is_classroom_private=True)
            # A public question can have an older single-FK response after its
            # owner explicitly corrects the type. Exercise re-answer in a real UI.
            legacy_question = Question.objects.create(category=category, creator=teacher, subject="Math",
                question_text="Legacy numerical", question_type="numerical", numeric_key=Decimal("3.5"),
                numeric_tolerance=0, typed_key_verified=True, answer_status="manually_verified", target_type="jee")
            legacy_option = QuestionOption.objects.create(question=legacy_question, option_key="A", option_text="Old choice")
            legacy_exam = Exam.objects.create(title="Legacy numerical", slug="legacy-browser-number", category=category,
                creator=teacher, status="published", duration_minutes=15, total_marks=4, is_free=True, target_type="jee")
            eq = ExamQuestion.objects.create(exam=legacy_exam, question=legacy_question, order=1, positive_marks=4, negative_marks=1)
            legacy_attempt = ExamAttempt.objects.create(exam=legacy_exam, student=student, total_marks=4,
                last_active_question=eq, expires_at=timezone.now() + timedelta(minutes=15))
            legacy_response = ExamResponse.objects.create(attempt=legacy_attempt, exam_question=eq, order=1,
                selected_option=legacy_option, is_visited=True)
            data = {
                "student": student.username, "teacher": teacher.username, "password": password,
                "login": reverse("accounts:login"), "join": reverse("exams:classroom_join"),
                "legacySession": reverse("exams:exam_session", args=[legacy_attempt.attempt_uuid]),
                "practice": reverse("questions:practice_settings", args=[practice.pk]),
                "practiceMulti": reverse("questions:practice_question_edit", args=[sq[0].pk]),
                "practiceNumber": reverse("questions:practice_question_edit", args=[sq[1].pk]),
                "classroom": reverse("questions:classroom_paper_review", args=[classroom.pk]),
                "classroomMulti": reverse("questions:classroom_question_edit", args=[tq[0].pk]),
            }
            env = os.environ.copy()
            env.update(TYPED_TEST_URL=self.live_server_url, TYPED_TEST_DATA=json.dumps(data))
            script = Path(settings.BASE_DIR) / "tests" / "browser" / "typed-question-smoke.cjs"
            result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=180)
            self.assertEqual(result.returncode, 0, f"Typed browser workflow failed:\n{result.stdout}\n{result.stderr}")
            self.assertIn("TYPED_BROWSER_OK", result.stdout)
            legacy_response.refresh_from_db(); legacy_attempt.refresh_from_db()
            self.assertIsNone(legacy_response.selected_option_id)
            self.assertEqual(legacy_response.numerical_response, Decimal("3.5"))
            self.assertEqual(legacy_response.typed_grading_snapshot["outcome"], "correct")
            self.assertEqual(legacy_attempt.score_obtained, Decimal("4"))
            practice.refresh_from_db()
            private = practice.practice_exam.attempts.get(student=student)
            self.assertEqual((private.status, private.score_obtained, private.partial_count),
                             (ExamAttempt.AttemptStatus.SUBMITTED, Decimal("9"), 1))
            self.assertEqual(private.expires_at - private.started_at, timedelta(minutes=17))
            session = ClassroomSession.objects.get(teacher=teacher, exam__is_classroom_only=True)
            self.assertEqual(session.exam.duration_minutes, 23)
            classroom_attempt = session.attempts.get(student=student)
            self.assertEqual((classroom_attempt.score_obtained, classroom_attempt.partial_count), (Decimal("9"), 1))
            self.assertEqual([r.typed_grading_snapshot["outcome"] for r in classroom_attempt.responses.order_by("order")[:2]],
                             ["partial", "correct"])
            self.client.force_login(teacher)
            leaderboard = self.client.get(reverse("exams:classroom_leaderboard", args=[session.pk]))
            self.assertContains(leaderboard, "Partial credit")
            self.assertEqual(leaderboard.context["leaderboard"][0]["partial"], 1)
            export = self.client.get(reverse("exams:classroom_export", args=[session.pk]))
            row = next(csv.DictReader(io.StringIO(export.content.decode())))
            self.assertEqual(row["Partial credit"], "1")
            self.assertEqual(row["Correct"], "2")
            self.assertEqual(row["Incorrect"], "0")
