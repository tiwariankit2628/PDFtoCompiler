"""Local answer-key photo parsing, validation, privacy and grading regressions."""
import io
import os
import tempfile
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

import pymupdf as fitz
from PIL import Image
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.models import Category, ClassroomSession, Exam, ExamAttempt
from .answer_key_image import (AnswerImageError, MAX_IMAGE_BYTES, _prepare_image,
                               parse_positioned_words, preview_answer_images, suggest_for_questions)
from .answer_key_validation import parse_checked_answer_key
from .models import AnswerStatus, ExtractedFigure, ExtractedQuestion, Question, QuestionFigure, QuestionOption, QuestionPaperUpload


OPTIONS = [{"key": k, "text": f"Choice {k}"} for k in "ABCD"]


def question(number, **kwargs):
    values = dict(question_number=number, options=list(OPTIONS), correct_option="", answer_status="needs_review",
                  detected_type="mcq_single", status="extracted", approved_question_id=None)
    values.update(kwargs)
    return SimpleNamespace(**values)


def words(*lines):
    """Compact positioned-word fixture: each row is a list of (x, token)."""
    return [(x, 30 * y, x + max(10, len(token) * 7), 30 * y + 12, token)
            for y, line in enumerate(lines) for x, token in line]


def png_upload(name="answer.png", size=(500, 180)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, "PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def synthetic_key_png():
    # The text is rasterized; the tested OCR receives a PNG, not embedded PDF text.
    with fitz.open() as document:
        page = document.new_page(width=750, height=250)
        page.insert_text((35, 55), "1: A    2: B    3: C", fontsize=22)
        page.insert_text((35, 105), "4: D    5: A    6: B", fontsize=22)
        return page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")


class AnswerImageParserTests(SimpleTestCase):
    def test_common_pair_and_horizontal_layouts(self):
        pairs = parse_positioned_words(words(
            [(0, "1:A"), (80, "2:B"), (160, "3:C")],
            [(0, "4:D"), (80, "5:A"), (160, "6:B")],
        ))
        self.assertEqual(pairs, [(1, "A"), (2, "B"), (3, "C"), (4, "D"), (5, "A"), (6, "B")])
        horizontal = words([(0, "Question"), (110, "1"), (160, "2"), (210, "3")],
                           [(0, "Answer"), (110, "B"), (160, "1"), (210, "D")])
        self.assertEqual(parse_positioned_words(horizontal), [(1, "B"), (2, "1"), (3, "D")])
        numeric_table = words([(0, "Q"), (100, "Answer")], [(0, "1"), (100, "3")],
                              [(0, "2"), (100, "1")])
        self.assertEqual(parse_positioned_words(numeric_table), [(1, "3"), (2, "1")])

    def test_ambiguous_numeric_rows_and_multi_answers_are_not_guessed(self):
        for line in ([(0, "1"), (20, "2")],
                     [(0, "1"), (20, "2"), (40, "3"), (60, "4")],
                     [(0, "1:A,C")], [(0, "1"), (25, "A"), (50, "C")],
                     [(0, "1:A/B")]):
            with self.subTest(line=line):
                pairs, unclear, count = parse_positioned_words(words(line), include_issues=True)
                self.assertEqual(pairs, [])
                self.assertIn(1, unclear)
                self.assertEqual(count, 1)
        # A bad token isn't corrected into a number, and the whole row is not
        # partially accepted just because its first pair looked plausible.
        pairs, unclear, count = parse_positioned_words(words([(0, "3"), (30, "B"), (100, "#4"), (130, "A")]), include_issues=True)
        self.assertEqual(pairs, [])
        self.assertIn(3, unclear)
        self.assertNotIn(4, unclear)
        self.assertEqual(count, 1)

    def test_duplicates_unknown_unsupported_and_invalid_choices_are_flagged(self):
        qs = [question(1), question(2, options=OPTIONS[:2]), question(3),
              question(4, detected_type="mcq_multiple"), question(5, status="approved"),
              question(6, options=[OPTIONS[0], OPTIONS[0]]), question(7), question(7)]
        result = suggest_for_questions([(1, "A"), (1, "B"), (2, "4"), (4, "C"),
                                        (5, "A"), (6, "A"), (7, "B"), (99, "B")],
                                       qs, exclude_approved=True, unclear_numbers=[3])
        self.assertEqual([row["status"] for row in result["rows"]], [
            "duplicate", "invalid_option", "unclear", "unsupported_type", "already_approved",
            "invalid_choices", "ambiguous_question", "ambiguous_question"])
        self.assertTrue(all(not row["answer"] for row in result["rows"]))
        self.assertEqual(result["unmatched_numbers"], [99])

    def test_manual_key_is_strict_and_numeric_mapping_is_explicit(self):
        qs = [question(1), question(2), question(3)]
        self.assertEqual(parse_checked_answer_key("1: A, 2 - 2; 3 (D)", qs), {1: "A", 2: "B", 3: "D"})
        for value in ("12", "1:A,C", "1: A 1: B", "1: A, 9: B", "1: E", "1:3.5", "1:AB"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_checked_answer_key(value, qs)
        with self.assertRaises(ValidationError):
            parse_checked_answer_key("1:A", [question(1), question(1)])
        with self.assertRaises(ValidationError):
            parse_checked_answer_key("1:A", [question(1, detected_type="numerical")])
        with self.assertRaises(ValidationError):
            parse_checked_answer_key("1:A", [question(1, status="approved")], exclude_approved=True)

    def test_bounded_images_and_fail_closed_without_ocr(self):
        for uploads in ([], [png_upload() for _ in range(4)],
                        [SimpleUploadedFile("bad.png", b"not a PNG")],
                        [SimpleUploadedFile("huge.png", b"x" * (MAX_IMAGE_BYTES + 1))],
                        [png_upload(size=(29, 60))], [png_upload(size=(4000, 3001))]):
            with self.subTest(count=len(uploads)), self.assertRaises(AnswerImageError):
                preview_answer_images(uploads, [question(1)])
        # Even valid images are not sent elsewhere or turned into guesses if
        # local language data is missing.
        with patch("apps.questions.answer_key_image._english_data_directory", side_effect=AnswerImageError("Unavailable")), \
             patch("apps.questions.answer_key_image._ocr_words") as ocr:
            with self.assertRaisesMessage(AnswerImageError, "Unavailable"):
                preview_answer_images([png_upload()], [question(1)])
            ocr.assert_not_called()

    @skipUnless(os.path.isfile(os.path.join(os.environ.get("TESSDATA_PREFIX", ""), "eng.traineddata")),
                "Local English OCR language data not configured")
    def test_real_local_ocr_on_a_synthetic_raster_key(self):
        upload = SimpleUploadedFile("key.png", synthetic_key_png(), content_type="image/png")
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No external request allowed")):
            result = preview_answer_images([upload], [question(i) for i in range(1, 7)])
        self.assertEqual([row["answer"] for row in result["rows"]], list("ABCDAB"))
        self.assertEqual(result["detected_count"], 6)
        self.assertTrue(all(row["status"] == "detected" for row in result["rows"]))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AnswerImageEndpointTests(TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = root.name
        settings = override_settings(MEDIA_ROOT=os.path.join(self.root, "media"),
                                     PRIVATE_PRACTICE_ROOT=os.path.join(self.root, "private"),
                                     STATIC_ROOT=os.path.join(self.root, "static"))
        settings.enable()
        self.addCleanup(settings.disable)
        self.category = Category.objects.create(name="Key tests", slug="key-tests")
        self.student = User.objects.create_user(username="key_student", email="ks@example.test", password="x", role="STUDENT")
        self.other = User.objects.create_user(username="key_other", email="ko@example.test", password="x", role="STUDENT")
        self.teacher = User.objects.create_user(username="key_teacher", email="kt@example.test", password="x", role="TEACHER")
        self.other_teacher = User.objects.create_user(username="key_teacher2", email="kt2@example.test", password="x", role="TEACHER")
        self.admin = User.objects.create_superuser(username="key_admin", email="ka@example.test", password="x", role="SUPER_ADMIN")
        self.student_paper = self.make_paper(self.student, is_student_private=True)
        self.classroom_paper = self.make_paper(self.teacher, is_classroom_private=True)
        self.teacher_paper = self.make_paper(self.teacher)

    def make_paper(self, owner, **flags):
        paper = QuestionPaperUpload.objects.create(title="Key preview paper", category=self.category,
                    uploaded_by=owner, status=QuestionPaperUpload.ProcessingStatus.EXTRACTED,
                    image_first=False, **flags)
        for number in (1, 2):
            ExtractedQuestion.objects.create(paper=paper, question_number=number, question_text=f"Question {number}?",
                                             options=list(OPTIONS), detected_type="mcq_single")
        return paper

    def preview(self, name, paper, client=None):
        client = client or self.client
        with patch("apps.questions.answer_key_image._english_data_directory", return_value="local-test"), \
             patch("apps.questions.answer_key_image._ocr_words", return_value=words([(0, "1:A"), (100, "2:B")])):
            return client.post(reverse(name, args=[paper.pk]), {"images": png_upload()})

    def test_preview_is_owner_scoped_no_store_and_never_saves_keys_or_images(self):
        self.client.force_login(self.student)
        before = list(self.student_paper.extracted_questions.values_list("correct_option", "answer_status"))
        response = self.preview("questions:practice_answer_image_preview", self.student_paper)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual([row["answer"] for row in response.json()["rows"]], ["A", "B"])
        self.assertEqual(before, list(self.student_paper.extracted_questions.values_list("correct_option", "answer_status")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "media")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "private")))
        self.client.force_login(self.other)
        self.assertEqual(self.preview("questions:practice_answer_image_preview", self.student_paper).status_code, 404)
        self.client.force_login(self.teacher)
        self.assertEqual(self.preview("questions:practice_answer_image_preview", self.student_paper).status_code, 403)
        self.client.logout()
        self.assertEqual(self.preview("questions:practice_answer_image_preview", self.student_paper).status_code, 302)

    def test_classroom_and_teacher_keys_are_scoped_and_not_interchangeable(self):
        self.client.force_login(self.teacher)
        for name, paper in (("questions:classroom_answer_image_preview", self.classroom_paper),
                            ("questions:teacher_answer_image_preview", self.teacher_paper)):
            response = self.preview(name, paper)
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.preview("questions:teacher_answer_image_preview", self.classroom_paper).status_code, 404)
        self.client.force_login(self.student)
        self.assertEqual(self.preview("questions:classroom_answer_image_preview", self.classroom_paper).status_code, 403)
        self.assertEqual(self.preview("questions:teacher_answer_image_preview", self.teacher_paper).status_code, 403)
        self.client.force_login(self.other_teacher)
        self.other_teacher.assigned_categories.add(self.category)
        self.assertEqual(self.client.get(reverse("questions:paper_review", args=[self.teacher_paper.pk])).status_code, 200)
        self.assertNotContains(self.client.get(reverse("questions:paper_review", args=[self.teacher_paper.pk])), "Upload answer-key images")
        self.assertEqual(self.preview("questions:teacher_answer_image_preview", self.teacher_paper).status_code, 403)
        self.assertEqual(self.preview("questions:classroom_answer_image_preview", self.classroom_paper).status_code, 404)
        self.client.force_login(self.admin)
        self.assertEqual(self.preview("questions:teacher_answer_image_preview", self.teacher_paper).status_code, 200)
        self.assertEqual(self.preview("questions:classroom_answer_image_preview", self.classroom_paper).status_code, 404)

    def test_csrf_required_and_upload_error_is_no_store(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.student)
        endpoint = reverse("questions:practice_answer_image_preview", args=[self.student_paper.pk])
        self.assertEqual(client.post(endpoint, {"images": png_upload()}).status_code, 403)
        client.get(reverse("questions:practice_settings", args=[self.student_paper.pk]))
        token = client.cookies["csrftoken"].value
        response = client.post(endpoint, {"images": SimpleUploadedFile("bad.png", b"bad")}, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 400)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("readable", response.json()["error"])

    def test_active_student_attempt_blocks_preview_before_ocr(self):
        exam = Exam.objects.create(title="Active", category=self.category, creator=self.student,
                                   is_private_practice=True, status="published", duration_minutes=20)
        attempt = ExamAttempt.objects.create(exam=exam, student=self.student, is_private_practice=True,
                      expires_at=timezone.now() + timedelta(minutes=20), status="in_progress")
        self.client.force_login(self.student)
        with patch("apps.questions.answer_key_image_views.preview_answer_images") as reader:
            response = self.client.post(reverse("questions:practice_answer_image_preview", args=[self.student_paper.pk]),
                                        {"images": png_upload()})
        self.assertRedirects(response, reverse("exams:exam_session", args=[attempt.attempt_uuid]), fetch_redirect_response=False)
        self.assertIn("no-store", response["Cache-Control"])
        reader.assert_not_called()

    def test_teacher_import_requires_confirmation_validates_whole_key_and_preserves_bank(self):
        self.client.force_login(self.teacher)
        url = reverse("questions:paper_import_answer_key", args=[self.teacher_paper.pk])
        q1, q2 = list(self.teacher_paper.extracted_questions.order_by("question_number"))
        for data in ({"answer_key_text": "1:A"},
                     {"answer_key_text": "1:A, 1:B", "confirm_checked": "on"},
                     {"answer_key_text": "1:A,C", "confirm_checked": "on"},
                     {"answer_key_text": "1:A, 999:B", "confirm_checked": "on"}):
            with self.subTest(data=data):
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 200)
                self.assertIn("no-store", response["Cache-Control"])
                self.assertContains(response, data["answer_key_text"])
                q1.refresh_from_db(); q2.refresh_from_db()
                self.assertEqual((q1.correct_option, q2.correct_option), ("", ""))
        q2.status = ExtractedQuestion.Status.APPROVED
        q2.save(update_fields=["status"])
        response = self.client.post(url, {"answer_key_text": "1:A, 2:B", "confirm_checked": "on"})
        self.assertContains(response, "already in the question bank")
        q1.refresh_from_db(); self.assertEqual(q1.correct_option, "")
        q2.status = ExtractedQuestion.Status.EXTRACTED; q2.save(update_fields=["status"])
        response = self.client.post(url, {"answer_key_text": "1:A, 2:2", "confirm_checked": "on"})
        self.assertEqual(response.status_code, 302)
        q1.refresh_from_db(); q2.refresh_from_db()
        self.assertEqual((q1.correct_option, q2.correct_option), ("A", "B"))
        self.assertEqual(q1.answer_status, AnswerStatus.MANUALLY_VERIFIED)
        response = self.client.post(url, {"answer_key_text": "1:B", "confirm_checked": "on"})
        self.assertContains(response, "Confirm replacement")
        q1.refresh_from_db(); self.assertEqual(q1.correct_option, "A")
        response = self.client.post(url, {"answer_key_text": "1:B", "confirm_checked": "on", "overwrite_existing": "on"})
        self.assertEqual(response.status_code, 302)
        q1.refresh_from_db(); self.assertEqual(q1.correct_option, "B")

    def test_student_bulk_key_requires_server_side_confirmation(self):
        self.client.force_login(self.student)
        url = reverse("questions:practice_settings", args=[self.student_paper.pk])
        response = self.client.post(url, {"duration_minutes": "20", "answer_key_text": "1:A"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirm that you checked the entered answer key")
        self.student_paper.refresh_from_db()
        self.assertIsNone(self.student_paper.practice_exam_id)
        q = self.student_paper.extracted_questions.get(question_number=1)
        self.assertEqual(q.answer_status, AnswerStatus.NEEDS_REVIEW)

    def test_unconfirmed_prefilled_key_never_becomes_confirmed_from_unrelated_edits(self):
        scenarios = (
            (self.student, self.student_paper, "questions:practice_question_edit", AnswerStatus.STUDENT_CONFIRMED),
            (self.teacher, self.classroom_paper, "questions:classroom_question_edit", AnswerStatus.MANUALLY_VERIFIED),
            (self.teacher, self.teacher_paper, "questions:extracted_edit", AnswerStatus.MANUALLY_VERIFIED),
        )
        for owner, paper, view, verified in scenarios:
            with self.subTest(view=view):
                q = paper.extracted_questions.get(question_number=1)
                q.correct_option, q.answer_status = "B", AnswerStatus.NEEDS_REVIEW
                q.save(update_fields=["correct_option", "answer_status"])
                self.client.force_login(owner)
                common = {"question_number": 1, "detected_type": "mcq_single", "question_text": "Unrelated edit",
                          "positive_marks": "4", "negative_marks": "1", "correct_option": "B"}
                if view == "questions:extracted_edit":
                    common.update({f"option_text_{k}": f"Choice {k}" for k in "ABCD"})
                else:
                    common.update({f"option_{k}": f"Choice {k}" for k in "ABCD"})
                url = reverse(view, args=[q.pk])
                self.assertEqual(self.client.post(url, common).status_code, 302)
                q.refresh_from_db()
                self.assertEqual((q.correct_option, q.answer_status), ("B", AnswerStatus.NEEDS_REVIEW))
                checked = dict(common, confirm_answer_key="on")
                self.assertEqual(self.client.post(url, checked).status_code, 302)
                q.refresh_from_db(); self.assertEqual(q.answer_status, verified)
                self.assertEqual(self.client.post(url, common).status_code, 302)
                q.refresh_from_db(); self.assertEqual(q.answer_status, verified)  # unchanged, previously checked
                changed = dict(common, correct_option="A")
                self.assertEqual(self.client.post(url, changed).status_code, 302)
                q.refresh_from_db()
                self.assertEqual((q.correct_option, q.answer_status), ("A", AnswerStatus.NEEDS_REVIEW))

    def test_question_or_choice_edits_require_fresh_confirmation_in_all_three_flows(self):
        scenarios = (
            (self.student, self.student_paper, "questions:practice_question_edit", AnswerStatus.STUDENT_CONFIRMED),
            (self.teacher, self.classroom_paper, "questions:classroom_question_edit", AnswerStatus.MANUALLY_VERIFIED),
            (self.teacher, self.teacher_paper, "questions:extracted_edit", AnswerStatus.MANUALLY_VERIFIED),
        )
        for owner, paper, view, verified in scenarios:
            with self.subTest(view=view):
                q = paper.extracted_questions.get(question_number=1)
                q.correct_option, q.answer_status = "B", verified
                q.save(update_fields=["correct_option", "answer_status"])
                self.client.force_login(owner)
                data = {"question_number": 1, "detected_type": "mcq_single", "question_text": q.question_text,
                        "positive_marks": "4", "negative_marks": "1", "correct_option": "B"}
                prefix = "option_text_" if view == "questions:extracted_edit" else "option_"
                data.update({f"{prefix}{k}": f"Choice {k}" for k in "ABCD"})
                url = reverse(view, args=[q.pk])
                self.assertEqual(self.client.post(url, data).status_code, 302)
                q.refresh_from_db(); self.assertEqual(q.answer_status, verified)
                changed = dict(data, **{f"{prefix}B": "A changed choice"})
                self.assertEqual(self.client.post(url, changed).status_code, 302)
                q.refresh_from_db(); self.assertEqual(q.answer_status, AnswerStatus.NEEDS_REVIEW)
                checked = dict(changed, confirm_answer_key="on")
                self.assertEqual(self.client.post(url, checked).status_code, 302)
                q.refresh_from_db(); self.assertEqual(q.answer_status, verified)
                changed_stem = dict(changed, question_text="Changed question")
                self.assertEqual(self.client.post(url, changed_stem).status_code, 302)
                q.refresh_from_db(); self.assertEqual(q.answer_status, AnswerStatus.NEEDS_REVIEW)
                self.assertEqual(self.client.post(url, dict(changed_stem, confirm_answer_key="on")).status_code, 302)
                q.refresh_from_db(); self.assertEqual(q.answer_status, verified)
                unsupported = dict(changed_stem, detected_type="mcq_multiple", correct_option="")
                self.assertEqual(self.client.post(url, unsupported).status_code, 302)
                q.refresh_from_db(); self.assertEqual(q.answer_status, AnswerStatus.NEEDS_REVIEW)

    def test_teacher_individual_key_rejects_invalid_or_nonexistent_labels_without_losing_edits(self):
        q = self.teacher_paper.extracted_questions.get(question_number=1)
        self.client.force_login(self.teacher)
        data = {"question_number": 1, "detected_type": "mcq_single", "question_text": q.question_text,
                "positive_marks": "4", "negative_marks": "1", "confirm_answer_key": "on",
                **{f"option_text_{key}": f"Edited {key}" for key in "ABCD"}}
        url = reverse("questions:extracted_edit", args=[q.pk])
        for key in ("X", "E"):
            response = self.client.post(url, dict(data, correct_option=key))
            self.assertEqual(response.status_code, 200)
            self.assertIn("correct_option", response.context["form"].errors)
            self.assertContains(response, 'value="Edited B"')
            q.refresh_from_db()
            self.assertEqual(q.correct_option, "")
            self.assertEqual(q.answer_status, AnswerStatus.NEEDS_REVIEW)

    def test_private_and_public_bank_edits_do_not_confirm_prefilled_or_changed_keys(self):
        for private in (True, False):
            with self.subTest(private=private):
                owner = self.student if private else self.teacher
                q = Question.objects.create(category=self.category, creator=owner, subject="Science",
                    question_text="A bank question?", is_private_practice=private, is_pdf_derived=not private,
                    answer_status=AnswerStatus.NEEDS_REVIEW, key_verified=False)
                for i, key in enumerate("ABCD"):
                    QuestionOption.objects.create(question=q, option_key=key, option_text=f"Choice {key}",
                                                  is_correct=key == "B", order=i)
                self.client.force_login(owner)
                if private:
                    url = reverse("questions:practice_bank_edit", args=[q.pk])
                    data = {"question_text": q.question_text, "subject": "Science", "topic": "", "chapter": "",
                            "difficulty": "medium", "language": "en", "positive_marks": "4", "negative_marks": "1",
                            "correct_option": "B", **{f"option_{k}": f"Choice {k}" for k in "ABCD"}}
                    option_name = "option_B"
                else:
                    url = reverse("questions:question_edit", args=[q.pk])
                    data = {"category": self.category.pk, "subject": "Science", "topic": "", "chapter": "",
                            "difficulty": "medium", "language": "en", "question_type": "mcq_single",
                            "question_text": q.question_text, "positive_marks": "4", "negative_marks": "1",
                            "correct_option": "B", **{f"option_text_{k}": f"Choice {k}" for k in "ABCD"}}
                    option_name = "option_text_B"
                self.assertContains(self.client.get(url), "I checked this answer")
                self.assertEqual(self.client.post(url, data).status_code, 302)
                q.refresh_from_db(); self.assertFalse(q.key_verified)
                self.assertEqual(q.answer_status, AnswerStatus.NEEDS_REVIEW)
                self.assertEqual(self.client.post(url, dict(data, confirm_answer_key="on")).status_code, 302)
                q.refresh_from_db()
                expected = AnswerStatus.STUDENT_CONFIRMED if private else AnswerStatus.MANUALLY_VERIFIED
                self.assertEqual(q.answer_status, expected)
                if private: self.assertTrue(q.key_verified)
                changed = dict(data, **{option_name: "Changed B"})
                self.assertEqual(self.client.post(url, changed).status_code, 302)
                q.refresh_from_db(); self.assertEqual(q.answer_status, AnswerStatus.NEEDS_REVIEW)
                self.assertFalse(q.key_verified)
                if private:
                    form = self.client.get(url).context["form"]
                    self.assertEqual(form.initial["correct_option"], "B")
                    self.assertEqual(self.client.post(url, changed).status_code, 302)
                    q.refresh_from_db()
                    self.assertTrue(q.options.get(option_key="B").is_correct)
                    self.assertFalse(q.key_verified)

    def test_student_crop_only_choices_require_explicit_labels_and_reach_bank_test(self):
        paper = self.student_paper
        paper.image_first = True
        paper.save(update_fields=["image_first"])
        q = paper.extracted_questions.get(question_number=1)
        q.is_image_first = True
        q.options = []
        q.save(update_fields=["is_image_first", "options"])
        paper.extracted_questions.exclude(pk=q.pk).update(include_in_practice=False)
        self.client.force_login(self.student)
        url = reverse("questions:practice_question_edit", args=[q.pk])
        payload = {"detected_type": "mcq_single", "question_text": q.question_text,
                   "positive_marks": "4", "negative_marks": "1", "visible_crop_choices": ["A", "B", "C", "D"],
                   "correct_option": "B", "confirm_answer_key": "on", "reviewed": "on"}
        denied = self.client.post(url, payload)
        self.assertEqual(denied.status_code, 200)
        self.assertIn("visible_crop_choices", denied.context["form"].errors)
        ExtractedFigure.objects.create(paper=paper, question=q, source="question_crop", page_number=1,
                                       private_image=png_upload("question.png"))
        denied = self.client.post(url, dict(payload, visible_crop_choices=[]))
        self.assertEqual(denied.status_code, 200)  # no labels inferred from image
        saved = self.client.post(url, payload)
        self.assertEqual(saved.status_code, 302, saved.context and saved.context["form"].errors)
        q.refresh_from_db()
        self.assertEqual([option["key"] for option in q.options], list("ABCD"))
        self.assertTrue(all(option["text"] == "" and option["visible_in_crop"] for option in q.options))
        created = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {
            "duration_minutes": "20", "acknowledge_review": "on", "acknowledge_unassigned": "on"})
        self.assertEqual(created.status_code, 302, created.context and created.context["form"].errors)
        paper.refresh_from_db(); exam = paper.practice_exam
        bank = exam.questions.get()
        self.assertTrue(bank.key_verified)
        self.assertEqual(list(bank.options.values_list("option_key", flat=True)), list("ABCD"))
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[exam.slug])).status_code, 302)
        attempt = ExamAttempt.objects.get(exam=exam)
        response = attempt.responses.get(); response.selected_option = bank.options.get(option_key="B")
        response.save(update_fields=["selected_option"])
        attempt.submit_attempt()
        self.assertTrue(attempt.responses.get().is_correct)
        bank_edit = {"question_text": q.question_text, "subject": "General", "topic": "Updated", "chapter": "",
                     "difficulty": "medium", "language": "en", "positive_marks": "4", "negative_marks": "1",
                     "correct_option": "B"}
        self.assertEqual(self.client.post(reverse("questions:practice_bank_edit", args=[bank.pk]), bank_edit).status_code, 302)
        future = Question.objects.get(creator=self.student, is_private_practice=True, is_bank_archived=False)
        self.assertTrue(future.key_verified)
        self.assertEqual(future.options.count(), 4)
        created = self.client.post(reverse("questions:practice_bank_test"), {"title": "Crop bank test",
                    "duration_minutes": "20", "question_ids": [future.pk]})
        self.assertEqual(created.status_code, 302)
        bank_exam = Exam.objects.get(title="Crop bank test")
        self.assertTrue(bank_exam.image_first_mode)
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[bank_exam.slug])).status_code, 302)
        bank_attempt = ExamAttempt.objects.get(exam=bank_exam)
        page = self.client.get(reverse("exams:exam_session", args=[bank_attempt.attempt_uuid]))
        self.assertTrue(page.context["questions_data"][0]["image_first"])
        self.assertEqual(len(page.context["questions_data"][0]["crop_urls"]), 1)

    def test_student_and_teacher_can_recrop_original_and_must_recheck_key(self):
        with fitz.open() as document:
            page = document.new_page(width=400, height=300)
            page.insert_text((30, 60), "1. Original question?  A. Yes   B. No")
            pdf = document.tobytes()
        for private, owner, paper, view, verified in (
            (True, self.student, self.student_paper, "questions:practice_figures", AnswerStatus.STUDENT_CONFIRMED),
            (False, self.teacher, self.teacher_paper, "questions:figure_manage", AnswerStatus.MANUALLY_VERIFIED),
        ):
            with self.subTest(view=view):
                paper.image_first, paper.total_pages = True, 1
                field = paper.private_pdf_file if private else paper.pdf_file
                field.save("source.pdf", SimpleUploadedFile("source.pdf", pdf, content_type="application/pdf"), save=False)
                paper.save()
                q = paper.extracted_questions.get(question_number=1)
                q.is_image_first, q.correct_option, q.answer_status = True, "B", verified
                q.save(update_fields=["is_image_first", "correct_option", "answer_status"])
                self.client.force_login(owner)
                url = reverse(view, args=[q.pk])
                self.assertContains(self.client.get(url), "Original question image")
                posted = self.client.post(url, {"action": "crop", "target": "__crop__", "page_number": "1",
                    "x0": ".05", "y0": ".05", "x1": ".9", "y1": ".5"})
                self.assertEqual(posted.status_code, 302)
                q.refresh_from_db()
                self.assertEqual(q.answer_status, AnswerStatus.NEEDS_REVIEW)
                self.assertEqual(q.correct_option, "B")  # retained as an unconfirmed suggestion
                crop = q.figures.get(source="question_crop")
                field = crop.private_image if private else crop.image
                field.storage.delete(field.name)  # saved reference but lost file
                review_name = "questions:practice_settings" if private else "questions:paper_review"
                review = self.client.get(reverse(review_name, args=[paper.pk]))
                self.assertContains(review, "An original image file is unavailable on page 1")

    def test_teacher_crop_only_choices_remain_usable_in_text_first_exam(self):
        paper = self.teacher_paper
        paper.image_first = True; paper.save(update_fields=["image_first"])
        q = paper.extracted_questions.get(question_number=1)
        q.is_image_first = True; q.options = []; q.save(update_fields=["is_image_first", "options"])
        self.client.force_login(self.teacher)
        url = reverse("questions:extracted_edit", args=[q.pk])
        data = {"question_number": 1, "detected_type": "mcq_single", "question_text": "",
                "positive_marks": "4", "negative_marks": "1", "correct_option": "B", "confirm_answer_key": "on",
                "visible_crop_choices": ["A", "B", "C", "D"]}
        denied = self.client.post(url, data)
        self.assertEqual(denied.status_code, 200)
        self.assertIn("visible_crop_choices", denied.context["form"].errors)
        ExtractedFigure.objects.create(paper=paper, question=q, source="question_crop", page_number=1,
                                       image=png_upload("teacher-question.png"))
        saved = self.client.post(url, data)
        self.assertEqual(saved.status_code, 302, saved.context and saved.context["form"].errors)
        q.refresh_from_db()
        self.assertTrue(all(option["text"] == "" and option["visible_in_crop"] for option in q.options))
        self.assertEqual(q.answer_status, AnswerStatus.MANUALLY_VERIFIED)
        approved = self.client.post(reverse("questions:extracted_approve", args=[q.pk]))
        self.assertEqual(approved.status_code, 302)
        q.refresh_from_db(); bank = q.approved_question
        self.assertIsNotNone(bank)
        self.assertTrue(bank.is_image_first)
        self.assertEqual(bank.options.count(), 4)
        edit = {"category": self.category.pk, "subject": "General", "topic": "Edited metadata", "chapter": "",
                "difficulty": "medium", "language": "en", "question_type": "mcq_single", "question_text": "",
                "positive_marks": "4", "negative_marks": "1", "correct_option": "B"}
        self.assertEqual(self.client.post(reverse("questions:question_edit", args=[bank.pk]), edit).status_code, 302)
        bank.refresh_from_db()
        self.assertEqual(bank.options.count(), 4)
        self.assertEqual(bank.answer_status, AnswerStatus.MANUALLY_VERIFIED)
        # Even if an institutional exam otherwise uses text mode and shuffling,
        # an image-only question must display its original and keep choice order.
        exam = Exam.objects.create(title="Teacher crop-only", category=self.category, creator=self.teacher,
            status=Exam.ExamStatus.PUBLISHED, is_free=True, duration_minutes=20,
            image_first_mode=False, shuffle_questions=False, shuffle_options=True)
        from apps.exams.models import ExamQuestion
        ExamQuestion.objects.create(exam=exam, question=bank, order=1, positive_marks=4, negative_marks=1)
        exam.recalculate_marks()
        bank.source_regions = [{"page": 1, "box": [0, 0, .5, .5]}, {"page": 2, "box": [0, 0, .5, .5]}]
        bank.save(update_fields=["source_regions"])
        builder = self.client.get(reverse("exams:exam_questions", args=[exam.pk]))
        self.assertContains(builder, "A possible continuation has no original question image on page 2")
        self.assertContains(builder, "Repair images")
        self.assertContains(builder, "Original question image")
        bank.source_regions = []; bank.save(update_fields=["source_regions"])
        self.client.force_login(self.student)
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[exam.slug])).status_code, 302)
        attempt = ExamAttempt.objects.get(exam=exam, student=self.student)
        page = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        item = page.context["questions_data"][0]
        self.assertTrue(item["image_first"])
        self.assertEqual(len(item["crop_urls"]), 1)
        self.assertEqual([option["key"] for option in item["options"]], list("ABCD"))
        self.assertFalse(exam.image_first_mode)  # the teacher's setting was not changed

    def test_public_bank_recrop_preserves_original_role_and_marks_answer_unconfirmed(self):
        paper = self.teacher_paper
        with fitz.open() as document:
            page = document.new_page(width=400, height=300)
            page.insert_text((30, 60), "Original question? A. Yes B. No")
            pdf = document.tobytes()
        paper.image_first, paper.total_pages = True, 1
        paper.pdf_file.save("public.pdf", SimpleUploadedFile("public.pdf", pdf), save=False)
        paper.save()
        staged = paper.extracted_questions.get(question_number=1)
        bank = Question.objects.create(category=self.category, creator=self.teacher, source_paper=paper,
            subject="Science", question_text=staged.question_text, is_pdf_derived=True, is_image_first=True,
            answer_status=AnswerStatus.MANUALLY_VERIFIED)
        for key in "ABCD":
            QuestionOption.objects.create(question=bank, option_key=key, option_text=f"Choice {key}", is_correct=key == "B")
        staged.approved_question, staged.status = bank, ExtractedQuestion.Status.APPROVED
        staged.save(update_fields=["approved_question", "status"])
        crop = QuestionFigure.objects.create(question=bank, source="question_crop", page_number=1,
                                              image=png_upload("public-crop.png"))
        self.client.force_login(self.teacher)
        url = reverse("questions:bank_figure_manage", args=[bank.pk])
        self.assertContains(self.client.get(url), "Original question image")
        saved = self.client.post(url, {"action": "crop", "replace_id": crop.pk, "target": "__crop__",
                                      "page_number": "1", "x0": ".05", "y0": ".05", "x1": ".9", "y1": ".5"})
        self.assertEqual(saved.status_code, 302)
        bank.refresh_from_db(); crop.refresh_from_db()
        self.assertEqual(crop.source, "question_crop")
        self.assertEqual(bank.answer_status, AnswerStatus.NEEDS_REVIEW)
        self.assertTrue(bank.options.get(option_key="B").is_correct)
        preview = self.client.get(reverse("questions:question_preview", args=[bank.pk]))
        self.assertContains(preview, "Unconfirmed suggestion — ungraded")
        self.assertNotContains(preview, "Correct Answer Key")
        self.assertContains(self.client.get(reverse("questions:question_bank")), "Unconfirmed")

    def test_teacher_approval_preserves_suggestion_but_never_promotes_it_to_a_bank_key(self):
        self.client.force_login(self.teacher)
        q = self.teacher_paper.extracted_questions.get(question_number=1)
        q.correct_option, q.answer_status, q.is_image_first = "B", AnswerStatus.NEEDS_REVIEW, True
        q.save(update_fields=["correct_option", "answer_status", "is_image_first"])
        self.assertEqual(self.client.post(reverse("questions:extracted_approve", args=[q.pk])).status_code, 302)
        q.refresh_from_db()
        self.assertEqual((q.correct_option, q.answer_status), ("B", AnswerStatus.NEEDS_REVIEW))
        bank = q.approved_question
        self.assertTrue(bank.is_image_first)
        self.assertEqual(bank.answer_status, AnswerStatus.NEEDS_REVIEW)
        self.assertFalse(bank.options.filter(is_correct=True).exists())

    def test_reviewed_student_key_is_graded_but_unconfirmed_or_absent_stays_ungraded(self):
        self.client.force_login(self.student)
        q1, q2 = list(self.student_paper.extracted_questions.order_by("question_number"))
        q2.correct_option = "B"  # historical unconfirmed suggestion, not a verified key
        q2.save(update_fields=["correct_option"])
        preview = self.preview("questions:practice_answer_image_preview", self.student_paper)
        self.assertEqual(preview.json()["rows"][0]["answer"], "A")
        self.assertFalse(preview.json()["rows"][1]["existing_confirmed"])
        response = self.client.post(reverse("questions:practice_settings", args=[self.student_paper.pk]),
                                    {"duration_minutes": "20", "answer_key_text": "1:A", "confirm_answer_key": "on"})
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        self.student_paper.refresh_from_db(); q2.refresh_from_db()
        exam = self.student_paper.practice_exam
        first, second = [eq.question for eq in exam.exam_questions.select_related("question").order_by("order")]
        self.assertTrue(first.key_verified)
        self.assertFalse(second.key_verified)
        self.assertEqual(q2.correct_option, "B")  # source preserved, not silently verified
        self.assertEqual(q2.answer_status, AnswerStatus.NEEDS_REVIEW)
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[exam.slug])).status_code, 302)
        attempt = ExamAttempt.objects.get(exam=exam, student=self.student)
        items = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).context["questions_data"]
        for item, key in zip(items, ("A", "B")):
            selected = next(option["id"] for option in item["options"] if option["key"] == key)
            self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
                             data=__import__("json").dumps({"response_id": item["response_id"], "selected_option_id": selected, "action": "navigate"}),
                             content_type="application/json")
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), data="{}", content_type="application/json")
        attempt.refresh_from_db()
        self.assertEqual(attempt.correct_count, 1)
        self.assertEqual(attempt.incorrect_count, 0)
        second_response = attempt.responses.get(order=2)
        self.assertIsNone(second_response.is_correct)

    def test_classroom_image_preview_does_not_release_keys_to_students(self):
        self.client.force_login(self.teacher)
        preview = self.preview("questions:classroom_answer_image_preview", self.classroom_paper)
        self.assertEqual(preview.status_code, 200)
        response = self.client.post(reverse("questions:classroom_paper_review", args=[self.classroom_paper.pk]), {
            "creation_token": str(uuid.uuid4()), "duration_minutes": "20", "answer_key_text": "1:A",
            "confirm_review": "on", "show_results": "",
        })
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        session = ClassroomSession.objects.get(teacher=self.teacher)
        self.client.force_login(self.student)
        self.client.post(reverse("exams:classroom_join"), {"code": session.code})
        self.client.post(reverse("exams:classroom_take", args=[session.pk]))
        attempt = session.attempts.get(student=self.student)
        screen = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertNotContains(screen, '"is_correct"')
        self.assertNotContains(screen, '"correct_option"')
        item = screen.context["questions_data"][1]
        self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
                         data=__import__("json").dumps({"response_id": item["response_id"], "selected_option_id": item["options"][0]["id"], "action": "navigate"}),
                         content_type="application/json")
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), data="{}", content_type="application/json")
        attempt.refresh_from_db()
        self.assertEqual(attempt.incorrect_count, 0)
        self.assertIsNone(attempt.responses.get(order=2).is_correct)
        review = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        self.assertNotContains(review, "Key at grading")
        self.assertNotContains(review, "Question 1?")
