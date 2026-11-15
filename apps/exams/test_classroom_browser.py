"""Optional real-browser check of the live classroom monitor."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category, ClassroomParticipant, ClassroomSession, Exam, ExamQuestion
from apps.questions.models import AnswerStatus, Question, QuestionOption


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium classroom test")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ClassroomBrowserTests(StaticLiveServerTestCase):
    def test_join_submit_and_live_monitor_updates_without_reload(self):
        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install tests/browser dependencies first.")
        password = "ClassroomBrowserPass987!"
        teacher = User.objects.create_user(username="live_teacher", email="live-teacher@example.test",
                                           password=password, role=User.Roles.TEACHER)
        student = User.objects.create_user(username="live_student", email="live-student@example.test",
                                           password=password, role=User.Roles.STUDENT,
                                           first_name="<img src=x onerror=alert(1)>")
        category = Category.objects.create(name="Live browser", slug="live-browser")
        exam = Exam.objects.create(title="Live browser exam", slug="live-browser-exam", creator=teacher,
                                   category=category, status=Exam.ExamStatus.PUBLISHED,
                                   shuffle_questions=False, shuffle_options=False, is_free=True)
        question = Question.objects.create(category=category, creator=teacher, subject="Math",
                                           question_text="What is 2 + 2?", answer_status=AnswerStatus.MANUALLY_VERIFIED)
        QuestionOption.objects.create(question=question, option_key="A", option_text="Four", is_correct=True)
        QuestionOption.objects.create(question=question, option_key="B", option_text="Five", is_correct=False)
        ExamQuestion.objects.create(exam=exam, question=question, order=1, positive_marks=4, negative_marks=1)
        session = ClassroomSession.create_for(exam, teacher)
        fillers = [User(username=f"live_filler_{i}", email=f"live-filler-{i}@example.test", role=User.Roles.STUDENT)
                   for i in range(25)]
        User.objects.bulk_create(fillers)
        ClassroomParticipant.objects.bulk_create([ClassroomParticipant(session=session, student=user) for user in fillers])
        data = {
            "password": password, "teacher": teacher.username, "student": student.username, "code": session.code,
            "login": reverse("accounts:login"), "join": reverse("exams:classroom_join"),
            "session": reverse("exams:classroom_session", args=[session.pk]),
        }
        env = os.environ.copy()
        env.update(CLASSROOM_TEST_URL=self.live_server_url, CLASSROOM_TEST_DATA=json.dumps(data))
        script = Path(settings.BASE_DIR) / "tests" / "browser" / "classroom-smoke.cjs"
        result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, f"Classroom browser test failed:\n{result.stdout}\n{result.stderr}")
        self.assertIn("CLASSROOM_BROWSER_OK", result.stdout)
