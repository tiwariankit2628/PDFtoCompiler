"""Optional end-to-end check on an operator-supplied, locally held key photo.

Neither image nor ground truth is stored in the repository. Set LOCAL_KEY_PHOTO
and LOCAL_KEY_TRUTH (180 visually checked numeric answers in question order) to
exercise this test with the supplied four-column key. Local English OCR data is
found from the environment or the project requirements, without a download.
"""
import os
import tempfile
import uuid
from unittest import skipUnless
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category, ClassroomSession
from .answer_key_image import AnswerImageError, _english_data_directory
from .models import AnswerStatus, ExtractedQuestion, QuestionPaperUpload


PHOTO = os.environ.get("LOCAL_KEY_PHOTO", "")
TRUTH = os.environ.get("LOCAL_KEY_TRUTH", "")
try:
    OCR_DATA = os.path.join(_english_data_directory(), "eng.traineddata")
except AnswerImageError:
    OCR_DATA = ""


@skipUnless(os.path.isfile(PHOTO) and os.path.isfile(TRUTH) and os.path.isfile(OCR_DATA),
            "Optional local key-photo and checked-truth fixtures are not configured")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LocalKeyPhotoWorkflowTests(TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        override = override_settings(MEDIA_ROOT=os.path.join(root.name, "media"),
                                     PRIVATE_PRACTICE_ROOT=os.path.join(root.name, "private"))
        override.enable()
        self.addCleanup(override.disable)
        with open(PHOTO, "rb") as source:
            self.photo = source.read()
        with open(TRUTH, encoding="utf-8") as source:
            self.truth = source.read().split()
        self.assertEqual(len(self.truth), 180)
        self.assertTrue(all(answer in "1234" and len(answer) == 1 for answer in self.truth))
        self.assertEqual(self.truth[158], "3")
        self.category = Category.objects.create(name="Local photo workflow", slug="local-photo-workflow")
        self.student = User.objects.create_user("photo-student", email="photo-s@example.test", password="x", role="STUDENT")
        self.teacher = User.objects.create_user("photo-teacher", email="photo-t@example.test", password="x", role="TEACHER")

    def make_paper(self, owner, **flags):
        paper = QuestionPaperUpload.objects.create(title="Local photo workflow", category=self.category,
                    uploaded_by=owner, status=QuestionPaperUpload.ProcessingStatus.EXTRACTED,
                    image_first=False, **flags)
        combinations = ["A, B and E only", "B, C and D only", "A, B and D only", "A, D and E only"]
        questions = []
        for number in range(1, 181):
            options = combinations if number == 159 else ["First", "Second", "Third", "Fourth"]
            questions.append(ExtractedQuestion(paper=paper, question_number=number,
                question_text="Which statements is/are correct?" if number == 159 else f"Synthetic question {number}?",
                detected_type="mcq_multiple" if number == 159 else "mcq_single",
                options=[{"key": key, "text": text} for key, text in zip("ABCD", options)]))
        ExtractedQuestion.objects.bulk_create(questions)
        return paper, combinations

    def preview(self, url):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No external request allowed")):
            response = self.client.post(url, {"images": SimpleUploadedFile("local-key.jpeg", self.photo,
                                                                           content_type="image/jpeg")})
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        return response.json()["rows"]

    def test_photo_review_type_correction_and_explicit_confirmation_for_student_and_teachers(self):
        scenarios = (
            (self.student, {"is_student_private": True}, "questions:practice_question_edit", "questions:practice_answer_image_preview"),
            (self.teacher, {"is_classroom_private": True}, "questions:classroom_question_edit", "questions:classroom_answer_image_preview"),
            (self.teacher, {}, "questions:extracted_edit", "questions:teacher_answer_image_preview"),
        )
        for owner, flags, edit_view, preview_view in scenarios:
            with self.subTest(view=edit_view):
                paper, combinations = self.make_paper(owner, **flags)
                question = paper.extracted_questions.get(question_number=159)
                self.client.force_login(owner)
                before = self.preview(reverse(preview_view, args=[paper.pk]))
                old = next(row for row in before if row["number"] == 159)
                self.assertEqual((old["status"], old["answer"]), ("unsupported_type", ""))
                question.refresh_from_db()
                self.assertEqual((question.detected_type, question.correct_option, question.answer_status),
                                 ("mcq_multiple", "", AnswerStatus.NEEDS_REVIEW))

                # This POST simulates a human inspecting the paper and making
                # the explicit type correction; OCR itself never changes it.
                prefix = "option_text_" if edit_view == "questions:extracted_edit" else "option_"
                data = {"question_number": 159, "detected_type": "mcq_single", "question_text": question.question_text,
                        "positive_marks": "4", "negative_marks": "1", "correct_option": "", "reviewed": "on",
                        **{f"{prefix}{key}": text for key, text in zip("ABCD", combinations)}}
                self.assertEqual(self.client.post(reverse(edit_view, args=[question.pk]), data).status_code, 302)
                result = self.preview(reverse(preview_view, args=[paper.pk]))
                correct, wrong = [], []
                for row in result:
                    expected = "ABCD"[int(self.truth[row["number"] - 1]) - 1]
                    if row["answer"] == expected:
                        correct.append(row["number"])
                    elif row["answer"]:
                        wrong.append(row["number"])
                self.assertGreaterEqual(len(correct), 160)
                self.assertEqual(wrong, [])
                self.assertEqual(next(row for row in result if row["number"] == 159)["answer"], "C")
                self.assertFalse(paper.extracted_questions.exclude(correct_option="").exists())

                # Simulate a person checking/correcting every value, including
                # blanks, before explicitly confirming the complete key.
                checked = ", ".join(f"{i}: {'ABCD'[int(answer) - 1]}" for i, answer in enumerate(self.truth, 1))
                if flags.get("is_student_private"):
                    url = reverse("questions:practice_settings", args=[paper.pk])
                    base = {"duration_minutes": "20", "answer_key_text": checked}
                    response = self.client.post(url, base)
                    self.assertContains(response, "Confirm that you checked the entered answer key")
                    response = self.client.post(url, dict(base, confirm_answer_key="on"))
                    self.assertEqual(response.status_code, 302)
                    paper.refresh_from_db()
                    self.assertEqual(paper.practice_exam.exam_questions.count(), 180)
                    bank = paper.practice_exam.exam_questions.get(order=159).question
                    self.assertTrue(bank.key_verified)
                    self.assertEqual(bank.options.get(is_correct=True).option_key, "C")
                elif flags.get("is_classroom_private"):
                    url = reverse("questions:classroom_paper_review", args=[paper.pk])
                    base = {"duration_minutes": "20", "creation_token": str(uuid.uuid4()), "answer_key_text": checked}
                    response = self.client.post(url, base)
                    self.assertEqual(response.status_code, 200)  # confirmation missing
                    response = self.client.post(url, dict(base, confirm_review="on"))
                    self.assertEqual(response.status_code, 302)
                    session = ClassroomSession.objects.get(teacher=owner, exam__title=paper.title)
                    self.assertEqual(session.exam.exam_questions.count(), 180)
                    bank = session.exam.exam_questions.get(order=159).question
                    self.assertEqual(bank.options.get(is_correct=True).option_key, "C")
                    self.assertEqual(bank.answer_status, AnswerStatus.MANUALLY_VERIFIED)
                else:
                    url = reverse("questions:paper_import_answer_key", args=[paper.pk])
                    response = self.client.post(url, {"answer_key_text": checked})
                    self.assertEqual(response.status_code, 200)  # confirmation missing
                    response = self.client.post(url, {"answer_key_text": checked, "confirm_checked": "on"})
                    self.assertEqual(response.status_code, 302)
                    question.refresh_from_db()
                    self.assertEqual((question.correct_option, question.answer_status), ("C", AnswerStatus.MANUALLY_VERIFIED))
