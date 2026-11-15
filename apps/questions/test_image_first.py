"""Image-first practice: extraction crops, CBT display, A-D mapping, grading.

All PDFs are synthetic. No external service is contacted and no accuracy is
claimed. Image-first shows the original-PDF crop; answers bind by option id so
scoring and the 1-4 -> A-D mapping are preserved.
"""
import io
import os
import tempfile
from datetime import timedelta

import pymupdf as fitz
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion, ExamResponse
from .extractor import PDFQuestionExtractor
from .image_first import detect_crop_answer_warning, question_crop_regions, render_question_crops
from .models import (
    AnswerStatus, ExtractedFigure, ExtractedQuestion, Question, QuestionFigure,
    QuestionOption, QuestionPaperUpload,
)
from .test_pdf_layout import make_two_column_pdf


def png(color="red", size=(60, 40)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


class CropHelperUnitTests(SimpleTestCase):
    def _page_doc(self, text):
        doc = fitz.open()
        page = doc.new_page(width=400, height=300)
        page.insert_text((30, 60), text, fontsize=11)
        return doc

    def test_answer_warning_flags_printed_solution(self):
        doc = self._page_doc("Q1 What is 2+2? Answer: B because it is four.")
        try:
            regions = [{"page": 1, "box": [0.0, 0.0, 1.0, 1.0]}]
            self.assertTrue(detect_crop_answer_warning(doc, regions))
        finally:
            doc.close()

    def test_answer_warning_ignores_ordinary_question_wording(self):
        doc = self._page_doc("Q1 Which of the following best answers the query about forces?")
        try:
            regions = [{"page": 1, "box": [0.0, 0.0, 1.0, 1.0]}]
            self.assertEqual(detect_crop_answer_warning(doc, regions), "")
        finally:
            doc.close()

    def test_render_crops_returns_png_bytes(self):
        doc = self._page_doc("Q1 Render me.")
        try:
            crops = render_question_crops(doc, [{"page": 1, "box": [0.0, 0.0, 1.0, 0.5]}])
            self.assertEqual(len(crops), 1)
            page_number, box, data = crops[0]
            self.assertEqual(page_number, 1)
            self.assertTrue(data.startswith(b"\x89PNG"))
        finally:
            doc.close()

    def test_regions_prefers_source_regions_then_primary(self):
        q = ExtractedQuestion(source_regions=[{"page": 2, "box": [0, 0, 1, 1]}], primary_image_region={"page": 1, "box": [0, 0, 1, 1]})
        self.assertEqual(question_crop_regions(q), [{"page": 2, "box": [0, 0, 1, 1]}])
        q2 = ExtractedQuestion(source_regions=[], primary_image_region={"page": 3, "box": [0, 0, 1, 1]})
        self.assertEqual(question_crop_regions(q2), [{"page": 3, "box": [0, 0, 1, 1]}])
        self.assertEqual(question_crop_regions(ExtractedQuestion()), [])


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"], CELERY_TASK_ALWAYS_EAGER=True)
class ImageFirstExtractionTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        override = override_settings(
            MEDIA_ROOT=os.path.join(self.root.name, "public"),
            STATIC_ROOT=os.path.join(self.root.name, "static"),
            PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private"),
        )
        override.enable()
        self.addCleanup(override.disable)
        self.teacher = User.objects.create_user(username="if_teacher", email="if_teacher@example.test", role=User.Roles.TEACHER, is_staff=True)
        self.category = Category.objects.create(name="Tests", slug="tests", is_active=True)

    def test_image_first_extraction_creates_question_crops(self):
        paper = QuestionPaperUpload.objects.create(
            title="IF teacher", category=self.category, subject="Science", uploaded_by=self.teacher,
            image_first=True, pdf_file=SimpleUploadedFile("if.pdf", make_two_column_pdf(), content_type="application/pdf"),
        )
        self.assertGreaterEqual(PDFQuestionExtractor(paper).extract(), 1)
        for q in paper.extracted_questions.all():
            self.assertTrue(q.is_image_first)
            crops = q.figures.filter(source="question_crop")
            self.assertGreaterEqual(crops.count(), 1, f"Q{q.question_number} missing a crop")
            for crop in crops:
                self.assertEqual(crop.option_key, "")
                self.assertTrue(crop.image)  # public paper -> public storage

    def test_image_first_off_generates_no_crops(self):
        paper = QuestionPaperUpload.objects.create(
            title="Plain teacher", category=self.category, subject="Science", uploaded_by=self.teacher,
            image_first=False, pdf_file=SimpleUploadedFile("plain.pdf", make_two_column_pdf(), content_type="application/pdf"),
        )
        self.assertGreaterEqual(PDFQuestionExtractor(paper).extract(), 1)
        self.assertFalse(paper.figures.filter(source="question_crop").exists())
        self.assertFalse(paper.extracted_questions.filter(is_image_first=True).exists())


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"], CELERY_TASK_ALWAYS_EAGER=True)
class ImageFirstPrivatePracticeTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        override = override_settings(
            MEDIA_ROOT=os.path.join(self.root.name, "public"),
            STATIC_ROOT=os.path.join(self.root.name, "static"),
            PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private"),
        )
        override.enable()
        os.makedirs(os.path.join(self.root.name, "static"), exist_ok=True)
        self.addCleanup(override.disable)
        self.student = User.objects.create_user(username="if_owner", email="if_owner@example.test", password="x", role=User.Roles.STUDENT)
        self.other = User.objects.create_user(username="if_other", email="if_other@example.test", password="x", role=User.Roles.STUDENT)
        self.client.force_login(self.student)

    def _upload(self, image_first=True):
        response = self.client.post(reverse("questions:practice_upload"), {
            "title": "Secret IF practice", "subject": "Physics",
            "image_first": "on" if image_first else "",
            "pdf_file": SimpleUploadedFile("paper.pdf", make_two_column_pdf(), content_type="application/pdf"),
        })
        self.assertEqual(response.status_code, 302)
        paper = QuestionPaperUpload.objects.get(uploaded_by=self.student, is_student_private=True)
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.EXTRACTED, paper.extraction_log)
        return paper

    def _create_exam(self, paper, keys=""):
        for question in paper.extracted_questions.all():
            from .practice_forms import practice_question_error
            if practice_question_error(question):
                self.client.post(reverse("questions:practice_question_include", args=[question.pk]))
        included = list(paper.extracted_questions.filter(include_in_practice=True))
        response = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {
            "duration_minutes": "30", "answer_key_text": keys, "acknowledge_review": "on", "passing_percentage": "0",
        })
        self.assertEqual(response.status_code, 302, getattr(response, "context", ""))
        paper.refresh_from_db()
        return paper.practice_exam, included

    def _start(self, exam):
        self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        return ExamAttempt.objects.get(exam=exam, status=ExamAttempt.AttemptStatus.IN_PROGRESS)

    def test_image_first_private_flow_marks_exam_and_bank(self):
        paper = self._upload(image_first=True)
        self.assertTrue(paper.image_first)
        self.assertTrue(paper.figures.filter(source="question_crop", private_image__isnull=False).exists())
        exam, included = self._create_exam(paper)
        self.assertTrue(exam.image_first_mode)
        self.assertTrue(exam.questions.filter(is_image_first=True).exists())
        self.assertTrue(QuestionFigure.objects.filter(question__in=exam.questions.all(), source="question_crop").exists())

    def test_cbt_payload_is_image_first_with_crop_and_fixed_order_no_answer_leak(self):
        paper = self._upload(image_first=True)
        exam, _ = self._create_exam(paper)
        attempt = self._start(exam)
        session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(session.status_code, 200)
        data = session.context["questions_data"]
        q0 = data[0]
        self.assertTrue(q0["image_first"])
        self.assertTrue(q0["crop_urls"])
        # Options present with letters, in fixed order, and no correct answer leaks.
        keys = [o["key"] for o in q0["options"]]
        self.assertEqual(keys, sorted(keys))
        for opt in q0["options"]:
            self.assertNotIn("is_correct", opt)
        # The private crop is served through an authorized view, not /media/.
        crop = paper.figures.filter(source="question_crop").first()
        self.assertNotContains(session, "/media/" + crop.private_image.name)
        # Another student cannot fetch the crop file.
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(q0["crop_urls"][0]).status_code, 404)

    def test_image_first_question_without_confirmed_key_stays_ungraded(self):
        paper = self._upload(image_first=True)
        exam, included = self._create_exam(paper)  # no keys -> nothing confirmed
        self.assertFalse(exam.questions.filter(key_verified=True).exists())
        attempt = self._start(exam)
        resp = attempt.responses.order_by("order").first()
        opt = resp.exam_question.question.options.first()
        # Save an answer by option id (as the UI does) via the JSON endpoint.
        import json as _json
        result = self.client.post(
            reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
            data=_json.dumps({"response_id": resp.id, "selected_option_id": opt.id, "action": "save_next"}),
            content_type="application/json",
        )
        self.assertEqual(result.status_code, 200)
        resp.refresh_from_db()
        self.assertEqual(resp.selected_option_id, opt.id)
        attempt.submit_attempt()
        resp.refresh_from_db()
        # No confirmed key -> ungraded (is_correct is None), never marked wrong.
        self.assertIsNone(resp.is_correct)
        self.assertEqual(float(resp.marks_awarded), 0.0)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PublicImageFirstShuffleTests(TestCase):
    """A public image-first question must not shuffle its options (they would no
    longer match the printed A/B/C/D in the crop), while scoring stays by id."""

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        override = override_settings(MEDIA_ROOT=os.path.join(self.root.name, "public"), STATIC_ROOT=os.path.join(self.root.name, "static"))
        override.enable()
        self.addCleanup(override.disable)
        self.category = Category.objects.create(name="Pub", slug="pub", is_active=True)
        self.teacher = User.objects.create_user(username="pub_teacher", email="pub_teacher@example.test", role=User.Roles.TEACHER, is_staff=True)
        self.student = User.objects.create_user(username="pub_student", email="pub_student@example.test", role=User.Roles.STUDENT)

    def test_public_image_first_keeps_options_in_order(self):
        bank = Question.objects.create(
            category=self.category, subject="Science", question_text="See image", is_image_first=True,
            creator=self.teacher, key_verified=True,
        )
        for i, key in enumerate("ABCD"):
            QuestionOption.objects.create(question=bank, option_key=key, option_text="", is_correct=(key == "C"), order=i)
        QuestionFigure.objects.create(question=bank, image=SimpleUploadedFile("crop.png", png(), content_type="image/png"), source="question_crop", order=0)
        exam = Exam.objects.create(
            title="Public IF", slug="public-if", category=self.category, creator=self.teacher,
            status=Exam.ExamStatus.PUBLISHED, shuffle_options=True, image_first_mode=True,
        )
        eq = ExamQuestion.objects.create(exam=exam, question=bank, order=1, positive_marks=4, negative_marks=1)
        attempt = ExamAttempt.objects.create(exam=exam, student=self.student, expires_at=timezone.now() + timedelta(minutes=30))
        resp = ExamResponse.objects.create(attempt=attempt, exam_question=eq, order=1)
        self.client.force_login(self.student)
        session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(session.status_code, 200)
        q0 = session.context["questions_data"][0]
        self.assertTrue(q0["image_first"])
        # Options are NOT shuffled for image-first, so they match the printed image.
        self.assertEqual([o["key"] for o in q0["options"]], list("ABCD"))
        self.assertTrue(q0["crop_urls"])
        # Scoring binds by option id: choosing C (the key) scores the positive marks.
        resp.selected_option = bank.options.get(option_key="C")
        resp.save(update_fields=["selected_option"])
        attempt.submit_attempt()
        resp.refresh_from_db()
        self.assertTrue(resp.is_correct)
        self.assertEqual(float(resp.marks_awarded), 4.0)
