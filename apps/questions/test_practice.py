"""End-to-end student PDF practice, timer, scoring, and privacy regressions."""
import io
import json
import os
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pymupdf as fitz
from PIL import Image
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.models import Exam, ExamAttempt, ExamQuestion, ExamResponse
from apps.exams.tasks import expire_private_practice_attempts
from .models import ExtractedFigure, ExtractedQuestion, Question, QuestionPaperUpload
from .practice_forms import PracticeSettingsForm, practice_question_error
from .private_storage import private_practice_storage
from .test_pdf_layout import make_two_column_pdf


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"], CELERY_TASK_ALWAYS_EAGER=True)
class StudentPracticeTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.override = override_settings(
            MEDIA_ROOT=os.path.join(self.root.name, "public"),
            STATIC_ROOT=os.path.join(self.root.name, "static"),
            PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private"),
        )
        self.override.enable()
        os.makedirs(os.path.join(self.root.name, "static"), exist_ok=True)
        self.addCleanup(self.override.disable)
        self.student = User.objects.create_user(username="owner", email="owner@example.test", password="x", role=User.Roles.STUDENT)
        self.other = User.objects.create_user(username="other", email="other@example.test", password="x", role=User.Roles.STUDENT)
        self.teacher = User.objects.create_user(username="teacher", email="teacher@example.test", password="x", role=User.Roles.TEACHER, is_staff=True)
        self.admin = User.objects.create_superuser(username="admin", email="admin@example.test", password="x", role=User.Roles.SUPER_ADMIN)
        self.client.force_login(self.student)

    def upload(self):
        response = self.client.post(reverse("questions:practice_upload"), {
            "title": "Secret physics practice", "subject": "Physics",
            "pdf_file": SimpleUploadedFile("paper.pdf", make_two_column_pdf(), content_type="application/pdf"),
        })
        self.assertEqual(response.status_code, 302)
        paper = QuestionPaperUpload.objects.get(uploaded_by=self.student, is_student_private=True)
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.EXTRACTED, paper.extraction_log)
        self.assertGreaterEqual(paper.extracted_questions.count(), 2)
        return paper

    def prepare(self, paper):
        # A malformed extraction is deliberately not silently omitted: the
        # owner must edit it or explicitly exclude it.
        for question in paper.extracted_questions.filter(include_in_practice=True).prefetch_related("figures"):
            if practice_question_error(question):
                self.client.post(reverse("questions:practice_question_include", args=[question.pk]))
        return list(paper.extracted_questions.filter(include_in_practice=True).prefetch_related("figures"))

    def create_exam(self, paper, duration=37, keys=""):
        questions = self.prepare(paper)
        self.assertGreaterEqual(len(questions), 2)
        response = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {
            "duration_minutes": str(duration), "answer_key_text": keys, "acknowledge_review": "on",
        })
        self.assertEqual(response.status_code, 302, getattr(response, "context", ""))
        paper.refresh_from_db()
        self.assertIsNotNone(paper.practice_exam_id)
        return paper.practice_exam

    def start(self, exam):
        response = self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        self.assertEqual(response.status_code, 302)
        attempt = ExamAttempt.objects.get(exam=exam, status=ExamAttempt.AttemptStatus.IN_PROGRESS)
        self.assertRedirects(response, reverse("exams:exam_session", args=[attempt.attempt_uuid]), fetch_redirect_response=False)
        return attempt

    def test_full_pdf_to_private_exam_and_exact_duration(self):
        paper = self.upload()
        self.assertFalse(paper.pdf_file)
        self.assertTrue(os.path.isfile(paper.private_pdf_file.path))
        self.assertEqual(os.stat(paper.private_pdf_file.path).st_mode & 0o777, 0o600)
        self.assertNotIn(os.path.realpath(self.root.name + "/public"), os.path.realpath(paper.private_pdf_file.path))
        with self.assertRaises(ValueError):
            _ = paper.private_pdf_file.url
        settings_response = self.client.get(reverse("questions:practice_settings", args=[paper.pk]))
        self.assertContains(settings_response, "Test preview")
        self.assertContains(settings_response, "15 min")
        self.assertContains(settings_response, "120 min")
        self.assertContains(settings_response, "Not selected")
        included = self.prepare(paper)
        exam = self.create_exam(paper, 37)
        self.assertTrue(exam.is_private_practice)
        self.assertEqual(exam.duration_minutes, 37)
        self.assertEqual(exam.total_questions_count, len(included))
        self.assertEqual(exam.total_marks, sum((q.positive_marks for q in included), Decimal("0.00")))
        self.assertTrue(exam.questions.filter(is_private_practice=True, creator=self.student).exists())
        self.assertFalse(exam.questions.filter(options__is_correct=True).exists())
        self.assertFalse(self.student.free_test_attempted)
        instructions = self.client.get(reverse("exams:exam_instructions", args=[exam.slug]))
        self.assertContains(instructions, "37 Min")
        self.assertContains(instructions, "PRIVATE PRACTICE")
        attempt = self.start(exam)
        self.assertEqual(attempt.expires_at - attempt.started_at, timedelta(minutes=37))
        self.assertEqual(attempt.responses.count(), len(included))
        self.student.refresh_from_db()
        self.assertFalse(self.student.free_test_attempted)
        session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(session.status_code, 200)
        self.assertIn("no-store", session["Cache-Control"])
        self.assertNotContains(session, '"is_correct"')

    def test_required_duration_rejects_missing_non_integer_and_out_of_bounds(self):
        paper = self.upload()
        self.prepare(paper)
        for value in ("", "0", "4", "361", "30.5", "-5", "abc"):
            with self.subTest(value=value):
                response = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {
                    "duration_minutes": value, "acknowledge_review": "on",
                })
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
                paper.refresh_from_db()
                self.assertIsNone(paper.practice_exam_id)
        self.assertEqual(Exam.objects.filter(is_private_practice=True).count(), 0)

    def test_duration_presets_custom_boundaries_and_configurable_limits(self):
        paper = self.upload()
        questions = self.prepare(paper)
        for value in (5, 15, 30, 45, 60, 90, 120, 137, 360):
            with self.subTest(value=value):
                form = PracticeSettingsForm({"duration_minutes": value, "acknowledge_review": True}, questions=questions)
                self.assertTrue(form.is_valid(), form.errors)
        with override_settings(PRACTICE_MIN_DURATION_MINUTES=10, PRACTICE_MAX_DURATION_MINUTES=60):
            response = self.client.get(reverse("questions:practice_settings", args=[paper.pk]))
            self.assertContains(response, "60 min")
            self.assertNotContains(response, 'data-minutes="90"')
            for value, valid in ((9, False), (10, True), (60, True), (61, False)):
                form = PracticeSettingsForm({"duration_minutes": value, "acknowledge_review": True}, questions=questions)
                self.assertEqual(form.is_valid(), valid, form.errors)
        with override_settings(PRACTICE_MIN_DURATION_MINUTES=0):
            with self.assertRaises(ImproperlyConfigured):
                PracticeSettingsForm(questions=questions)

    def test_preview_and_server_validation_block_invalid_or_unreviewed_items(self):
        paper = self.upload()
        question = paper.extracted_questions.first()
        question.options = [{"key": "A", "text": "Only one"}]
        question.is_flagged = True
        question.save(update_fields=["options", "is_flagged"])
        url = reverse("questions:practice_settings", args=[paper.pk])
        response = self.client.get(url)
        self.assertContains(response, "Needs editing")
        response = self.client.post(url, {"duration_minutes": 30, "acknowledge_review": "on"})
        self.assertContains(response, "Edit or exclude invalid questions")
        self.client.post(reverse("questions:practice_question_include", args=[question.pk]))
        self.prepare(paper)
        response = self.client.post(url, {"duration_minutes": 30})
        self.assertIn("acknowledge_review", response.context["form"].errors, response.context["form"].errors)
        self.assertEqual(Exam.objects.filter(is_private_practice=True).count(), 0)

    def test_answer_key_validation_never_guesses_or_accepts_unknown_options(self):
        paper = self.upload()
        questions = self.prepare(paper)
        number = questions[0].question_number
        for text in (f"{number}: Z", f"{number}: A, {number}: B", "99999: A", f"{number}: E"):
            with self.subTest(text=text):
                form = PracticeSettingsForm({"duration_minutes": 30, "acknowledge_review": True, "answer_key_text": text}, questions=questions)
                self.assertFalse(form.is_valid())
                self.assertIn("answer_key_text", form.errors)
        form = PracticeSettingsForm({"duration_minutes": 30, "acknowledge_review": True, "answer_key_text": f"{number}: 1"}, questions=questions)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.answer_keys[number], "A")

    def test_edit_question_inclusion_and_manual_question(self):
        paper = self.upload()
        question = paper.extracted_questions.first()
        response = self.client.post(reverse("questions:practice_question_edit", args=[question.pk]), {
            "question_text": "Checked question?", "option_A": "yes", "option_B": "no", "option_C": "", "option_D": "", "option_E": "",
            "correct_option": "B", "positive_marks": "2.00", "negative_marks": "0.50", "reviewed": "on", "explanation": "A note",
        })
        self.assertEqual(response.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.correct_option, "B")
        self.assertFalse(question.is_flagged)
        self.assertEqual(question.positive_marks, Decimal("2.00"))
        self.client.post(reverse("questions:practice_question_include", args=[question.pk]))
        question.refresh_from_db()
        self.assertFalse(question.include_in_practice)
        self.client.post(reverse("questions:practice_question_include", args=[question.pk]))
        response = self.client.post(reverse("questions:practice_question_add", args=[paper.pk]))
        self.assertEqual(response.status_code, 302)
        manual = paper.extracted_questions.order_by("-question_number").first()
        self.assertTrue(manual.is_flagged)
        self.assertIn(str(manual.pk), response.url)

    def test_timer_resume_refresh_and_offline_sweep_never_restart(self):
        paper = self.upload()
        exam = self.create_exam(paper, 30)
        start_time = timezone.now().replace(microsecond=0)
        with patch("django.utils.timezone.now", return_value=start_time):
            attempt = self.start(exam)
        original_expiry = attempt.expires_at
        with patch("django.utils.timezone.now", return_value=start_time + timedelta(minutes=9)):
            again = self.client.post(reverse("exams:exam_start", args=[exam.slug]))
            self.assertIn(str(attempt.attempt_uuid), again.url)
            self.client.get(reverse("exams:exam_instructions", args=[exam.slug]))
            sync = self.client.get(reverse("exams:exam_sync_status", args=[attempt.attempt_uuid])).json()
            self.assertEqual(sync["remaining_seconds"], 21 * 60)
            self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, original_expiry)
        self.assertEqual(ExamAttempt.objects.filter(exam=exam).count(), 1)
        # The periodic task submits while the browser is closed, and is idempotent.
        with patch("django.utils.timezone.now", return_value=original_expiry + timedelta(seconds=50)):
            self.assertEqual(expire_private_practice_attempts(), 1)
            self.assertEqual(expire_private_practice_attempts(), 0)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TIMED_OUT)
        self.assertEqual(attempt.expires_at, original_expiry)
        self.assertEqual(attempt.time_taken_seconds, 30 * 60)
        self.assertEqual(self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).status_code, 302)

    def test_expired_start_redirects_to_completion_not_new_attempt(self):
        paper = self.upload()
        exam = self.create_exam(paper, 15)
        attempt = self.start(exam)
        attempt.expires_at = timezone.now() - timedelta(seconds=1)
        attempt.save(update_fields=["expires_at"])
        response = self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TIMED_OUT)
        self.assertIn(str(attempt.attempt_uuid), response.url)
        self.assertEqual(ExamAttempt.objects.filter(exam=exam).count(), 1)
        # A subsequent explicit start is a fresh attempt, not a resumed timer.
        next_attempt = self.start(exam)
        self.assertNotEqual(next_attempt.pk, attempt.pk)

    def test_save_after_expiry_is_rejected_and_existing_answer_preserved(self):
        paper = self.upload()
        exam = self.create_exam(paper)
        attempt = self.start(exam)
        response = attempt.responses.first()
        options = list(response.exam_question.question.options.all())
        save_url = reverse("exams:exam_save_answer", args=[attempt.attempt_uuid])
        data = {"response_id": response.pk, "selected_option_id": options[0].pk, "action": "save_next"}
        saved = self.client.post(save_url, data=json.dumps(data), content_type="application/json").json()
        self.assertEqual(saved["status"], "success")
        attempt.expires_at = timezone.now() - timedelta(seconds=1)
        attempt.save(update_fields=["expires_at"])
        data["selected_option_id"] = options[1].pk
        result = self.client.post(save_url, data=json.dumps(data), content_type="application/json").json()
        self.assertEqual(result["status"], "expired")
        response.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(response.selected_option_id, options[0].pk)
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TIMED_OUT)

    def test_submit_after_deadline_is_timed_out_and_unique_active_constraint(self):
        paper = self.upload()
        exam = self.create_exam(paper, 45)
        attempt = self.start(exam)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExamAttempt.objects.create(exam=exam, student=self.student, is_private_practice=False)
        attempt.expires_at = timezone.now() - timedelta(seconds=1)
        attempt.save(update_fields=["expires_at"])
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TIMED_OUT)

    def test_partial_and_missing_keys_are_ungraded_not_wrong(self):
        paper = self.upload()
        questions = self.prepare(paper)
        number = questions[0].question_number
        exam = self.create_exam(paper, 15, f"{number}: A")
        attempt = self.start(exam)
        responses = list(attempt.responses.select_related("exam_question__question").order_by("order"))
        keyed = responses[0]
        unknown = responses[1]
        keyed.selected_option = keyed.exam_question.question.options.get(option_key="A")
        unknown.selected_option = unknown.exam_question.question.options.first()
        keyed.save(update_fields=["selected_option"])
        unknown.save(update_fields=["selected_option"])
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]))
        keyed.refresh_from_db()
        unknown.refresh_from_db()
        attempt.refresh_from_db()
        self.assertTrue(keyed.is_correct)
        self.assertIsNone(unknown.is_correct)
        self.assertEqual(unknown.marks_awarded, Decimal("0.00"))
        self.assertEqual(attempt.correct_count, 1)
        self.assertEqual(attempt.incorrect_count, 0)
        self.assertEqual(attempt.score_obtained, keyed.exam_question.positive_marks)
        self.assertIsNone(attempt.rank)
        review = self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        self.assertContains(review, "No answer key — ungraded")
        self.assertContains(review, "Score on keyed questions")
        self.assertContains(review, str(keyed.exam_question.positive_marks))
        self.assertEqual(review.context["graded_marks"], keyed.exam_question.positive_marks)
        self.assertEqual(review.context["unkeyed_count"], len(responses) - 1)
        for name in ("student_result_detail", "student_result_review", "student_result_pdf"):
            r = self.client.get(reverse("exams:" + name, args=[attempt.attempt_uuid]))
            self.assertRedirects(r, reverse("questions:practice_result", args=[attempt.attempt_uuid]), fetch_redirect_response=False)

    def test_figure_files_are_private_and_survive_into_candidate_test(self):
        paper = self.upload()
        question = paper.extracted_questions.first()
        self.assertTrue(question.private_image)
        self.assertFalse(question.image)
        private_url = reverse("questions:practice_extracted_image", args=[question.pk])
        image_response = self.client.get(private_url)
        self.assertEqual(image_response.status_code, 200)
        self.assertIn("no-store", image_response["Cache-Control"])
        image_response.close()
        crop_url = reverse("questions:practice_figures", args=[question.pk])
        self.assertEqual(self.client.get(crop_url).status_code, 200)
        crop = self.client.post(crop_url, {"action": "crop", "page_number": question.page_number, "x0": "0.05", "y0": "0.05", "x1": "0.3", "y1": "0.3", "target": "A"})
        self.assertEqual(crop.status_code, 302)
        figure = ExtractedFigure.objects.get(question=question)
        self.assertTrue(figure.private_image)
        self.assertFalse(figure.image)
        self.assertEqual(self.client.get(reverse("questions:practice_figure_file", args=[figure.pk])).status_code, 200)
        exam = self.create_exam(paper)
        attempt = self.start(exam)
        session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        bank_question = exam.questions.get(source_extracted_item=question)
        self.assertContains(session, reverse("questions:practice_bank_image", args=[bank_question.pk]))
        bank_figure = bank_question.figures.first()
        self.assertContains(session, reverse("questions:practice_bank_figure", args=[bank_figure.pk]))
        self.assertEqual(self.client.get(reverse("questions:practice_bank_figure", args=[bank_figure.pk])).status_code, 200)
        self.assertEqual(self.client.get("/media/" + figure.private_image.name).status_code, 404)

    def test_owner_only_student_paper_exam_attempt_and_teacher_admin_endpoints(self):
        paper = self.upload()
        question = paper.extracted_questions.first()
        exam = self.create_exam(paper)
        attempt = self.start(exam)
        bank = exam.questions.first()
        self.teacher.assigned_categories.add(paper.category)
        owner_urls = [
            reverse("questions:practice_pdf", args=[paper.pk]),
            reverse("questions:practice_settings", args=[paper.pk]),
            reverse("questions:practice_extracted_image", args=[question.pk]),
            reverse("questions:practice_bank_image", args=[bank.pk]),
            reverse("exams:exam_instructions", args=[exam.slug]),
            reverse("questions:practice_result", args=[attempt.attempt_uuid]),
        ]
        self.client.force_login(self.other)
        for url in owner_urls:
            with self.subTest(actor="other student", url=url):
                self.assertIn(self.client.get(url).status_code, (403, 404))
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[exam.slug])).status_code, 404)
        self.assertEqual(self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).status_code, 403)
        self.assertEqual(self.client.get(reverse("exams:exam_sync_status", args=[attempt.attempt_uuid])).status_code, 403)
        self.assertNotContains(self.client.get(reverse("exams:exam_catalog")), exam.title)
        self.assertNotContains(self.client.get(reverse("core:student_dashboard")), exam.title)
        for actor in (self.teacher, self.admin):
            self.client.force_login(actor)
            for url in owner_urls:
                with self.subTest(actor=actor.username, url=url):
                    self.assertIn(self.client.get(url).status_code, (403, 404))
            teacher_urls = [
                reverse("questions:paper_review", args=[paper.pk]), reverse("questions:paper_download", args=[paper.pk]),
                reverse("questions:extracted_edit", args=[question.pk]), reverse("questions:figure_manage", args=[question.pk]),
                reverse("questions:question_edit", args=[bank.pk]), reverse("questions:question_preview", args=[bank.pk]),
                reverse("exams:exam_edit", args=[exam.pk]), reverse("exams:exam_preview", args=[exam.pk]),
                reverse("exams:exam_questions", args=[exam.pk]), reverse("exams:teacher_exam_analytics", args=[exam.pk]),
                reverse("exams:teacher_export_results_csv", args=[exam.pk]), reverse("exams:exam_leaderboard", args=[exam.slug]),
                reverse("exams:student_result_detail", args=[attempt.attempt_uuid]),
            ]
            for url in teacher_urls:
                with self.subTest(actor=actor.username, url=url):
                    self.assertEqual(self.client.get(url).status_code, 404)
            for url in (reverse("questions:paper_list"), reverse("questions:question_bank"), reverse("exams:exam_list")):
                self.assertNotContains(self.client.get(url), paper.title)
        self.client.force_login(self.admin)
        for url in (reverse("admin:exams_exam_change", args=[exam.pk]), reverse("admin:exams_examattempt_change", args=[attempt.pk]),
                    reverse("admin:questions_questionpaperupload_change", args=[paper.pk]), reverse("admin:questions_question_change", args=[bank.pk]),
                    reverse("admin:questions_extractedquestion_change", args=[question.pk])):
            self.assertEqual(self.client.get(url).status_code, 302)  # admin masks unavailable objects via redirect
        for url in (reverse("admin:exams_exam_changelist"), reverse("admin:questions_questionpaperupload_changelist")):
            self.assertNotContains(self.client.get(url), paper.title)

    def test_student_upload_rejects_invalid_files_and_private_storage_misconfiguration(self):
        bad = SimpleUploadedFile("bad.pdf", b"not a pdf", content_type="application/pdf")
        response = self.client.post(reverse("questions:practice_upload"), {"title": "Bad", "pdf_file": bad})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invalid or damaged")
        self.assertFalse(QuestionPaperUpload.objects.exists())
        for path in ("", "relative/path", self.root.name, os.path.join(self.root.name, "public", "exposed")):
            with self.subTest(path=path), override_settings(PRIVATE_PRACTICE_ROOT=path):
                with self.assertRaises(ImproperlyConfigured):
                    _ = private_practice_storage.location

    def test_existing_category_name_does_not_block_self_service(self):
        from apps.exams.models import Category
        Category.objects.create(name="Student Private Practice (System)", slug="some-existing-category")
        paper = self.upload()
        self.assertEqual(paper.category.slug, "student-private-practice")
        self.assertFalse(paper.category.is_active)

    def test_non_student_cannot_use_student_upload(self):
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(reverse("questions:practice_upload")).status_code, 403)

    def test_keyless_test_is_not_given_a_score_or_pass_fail(self):
        paper = self.upload()
        exam = self.create_exam(paper, 120)
        attempt = self.start(exam)
        response = attempt.responses.first()
        response.selected_option = response.exam_question.question.options.first()
        response.save(update_fields=["selected_option"])
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]))
        attempt.refresh_from_db()
        response.refresh_from_db()
        self.assertIsNone(response.is_correct)
        self.assertEqual(attempt.incorrect_count, 0)
        self.assertEqual(attempt.score_obtained, Decimal("0.00"))
        result = self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        self.assertContains(result, "Not graded")
        self.assertNotContains(result, "NOT QUALIFIED")
        self.assertEqual(result.context["keyed_count"], 0)
        completed = self.client.get(reverse("exams:exam_completed", args=[attempt.attempt_uuid]))
        self.assertNotContains(completed, "NOT QUALIFIED")
        self.assertContains(completed, "ungraded")

    def test_duplicate_creation_is_idempotent_and_content_cannot_be_edited_after_creation(self):
        paper = self.upload()
        question = paper.extracted_questions.first()
        exam = self.create_exam(paper, 90)
        again = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {"duration_minutes": "15"})
        self.assertRedirects(again, reverse("exams:exam_instructions", args=[exam.slug]), fetch_redirect_response=False)
        exam.refresh_from_db()
        self.assertEqual(exam.duration_minutes, 90)
        self.assertEqual(Exam.objects.filter(is_private_practice=True).count(), 1)
        self.assertEqual(self.client.get(reverse("questions:practice_question_edit", args=[question.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("questions:practice_question_include", args=[question.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("questions:practice_question_add", args=[paper.pk])).status_code, 404)

    def test_unrecognized_pdf_supports_manual_question_creation(self):
        document = fitz.open()
        document.new_page()
        source = document.write()
        document.close()
        response = self.client.post(reverse("questions:practice_upload"), {
            "title": "Manual", "pdf_file": SimpleUploadedFile("blank.pdf", source, content_type="application/pdf"),
        })
        self.assertEqual(response.status_code, 302)
        paper = QuestionPaperUpload.objects.get(title="Manual")
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.FAILED)
        add = self.client.post(reverse("questions:practice_question_add", args=[paper.pk]))
        self.assertEqual(add.status_code, 302)
        q = paper.extracted_questions.get()
        edit = self.client.post(reverse("questions:practice_question_edit", args=[q.pk]), {
            "question_text": "A manual question?", "option_A": "Yes", "option_B": "No", "option_C": "", "option_D": "", "option_E": "",
            "correct_option": "", "positive_marks": "1", "negative_marks": "0", "reviewed": "on",
        })
        self.assertEqual(edit.status_code, 302)
        response = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {"duration_minutes": "15"})
        self.assertEqual(response.status_code, 302)
        paper.refresh_from_db()
        self.assertEqual(paper.practice_exam.total_questions_count, 1)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_queue_failure_preserves_pdf_and_enables_manual_recovery(self):
        with patch("apps.questions.practice_views.extract_private_practice_paper.delay", side_effect=ConnectionError("broker secret")):
            response = self.client.post(reverse("questions:practice_upload"), {
                "title": "Queue failure", "pdf_file": SimpleUploadedFile("paper.pdf", make_two_column_pdf(), content_type="application/pdf"),
            })
        self.assertEqual(response.status_code, 302)
        paper = QuestionPaperUpload.objects.get(title="Queue failure")
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.FAILED)
        self.assertTrue(os.path.exists(paper.private_pdf_file.path))
        self.assertNotIn("broker secret", paper.extraction_log)
        self.assertEqual(self.client.post(reverse("questions:practice_question_add", args=[paper.pk])).status_code, 302)

    def test_staff_post_actions_and_django_admin_relation_widgets_cannot_reach_private_data(self):
        paper = self.upload()
        question = paper.extracted_questions.first()
        exam = self.create_exam(paper)
        attempt = self.start(exam)
        bank = exam.questions.first()
        self.teacher.assigned_categories.add(paper.category)
        for actor in (self.teacher, self.admin):
            self.client.force_login(actor)
            post_urls = [
                reverse("questions:paper_bulk_approve", args=[paper.pk]), reverse("questions:paper_import_answer_key", args=[paper.pk]),
                reverse("questions:extracted_approve", args=[question.pk]), reverse("questions:extracted_delete", args=[question.pk]),
                reverse("questions:extracted_toggle_flag", args=[question.pk]), reverse("questions:question_delete", args=[bank.pk]),
                reverse("exams:exam_delete", args=[exam.pk]), reverse("exams:exam_duplicate", args=[exam.pk]),
                reverse("exams:exam_toggle_publish", args=[exam.pk]), reverse("exams:exam_add_questions_bulk", args=[exam.pk]),
                reverse("exams:exam_add_random_questions", args=[exam.pk]), reverse("exams:exam_remove_question", args=[exam.pk, bank.pk]),
            ]
            for url in post_urls:
                with self.subTest(actor=actor.username, url=url):
                    self.assertEqual(self.client.post(url, {"question_ids": [bank.pk]}).status_code, 404)
        self.client.force_login(self.admin)
        for name in ("admin:exams_examattempt_add", "admin:exams_examresponse_add", "admin:exams_examsecuritylog_add", "admin:questions_question_add"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, paper.title)
        self.assertTrue(Exam.objects.filter(pk=exam.pk).exists())
        self.assertTrue(ExamAttempt.objects.filter(pk=attempt.pk).exists())
        self.assertTrue(Question.objects.filter(pk=bank.pk).exists())

    def test_reconnect_to_list_finalizes_expired_attempt(self):
        paper = self.upload()
        exam = self.create_exam(paper)
        attempt = self.start(exam)
        attempt.expires_at = timezone.now() - timedelta(seconds=10)
        attempt.save(update_fields=["expires_at"])
        response = self.client.get(reverse("questions:practice_list"))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TIMED_OUT)
        self.assertContains(response, "Review")

    def test_sqlite_lock_retry_resumes_same_attempt(self):
        from django.db import OperationalError
        from apps.exams.views_candidate import ExamStartView
        paper = self.upload()
        exam = self.create_exam(paper)
        original = ExamStartView._start_private_once
        calls = []
        def locked_once(view, request, exam):
            calls.append(1)
            if len(calls) == 1:
                raise OperationalError("database is locked")
            return original(view, request, exam)
        with patch.object(ExamStartView, "_start_private_once", locked_once), patch("apps.exams.views_candidate.time.sleep"):
            attempt = self.start(exam)
        self.assertEqual(len(calls), 2)
        self.assertEqual(attempt.expires_at - attempt.started_at, timedelta(minutes=37))

    def test_private_source_page_and_figures_block_other_users(self):
        paper = self.upload()
        q = paper.extracted_questions.first()
        crop = reverse("questions:practice_figures", args=[q.pk])
        self.client.post(crop, {"action": "crop", "page_number": q.page_number, "x0": "0.1", "y0": "0.1", "x1": "0.2", "y1": "0.2", "target": ""})
        figure = q.figures.get()
        urls = [reverse("questions:practice_source_page", args=[q.pk, q.page_number]),
                reverse("questions:practice_figures", args=[q.pk]), reverse("questions:practice_figure_file", args=[figure.pk])]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response["Cache-Control"])
            response.close()
        self.client.force_login(self.other)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_question_image_upload_checks_file_and_supports_image_only_option(self):
        paper = self.upload()
        q = paper.extracted_questions.first()
        url = reverse("questions:practice_figures", args=[q.pk])
        bad = SimpleUploadedFile("fake.png", b"not an image", content_type="image/png")
        self.client.post(url, {"action": "upload", "target": "E", "image": bad})
        self.assertEqual(q.figures.count(), 0)
        buffer = io.BytesIO()
        Image.new("RGB", (12, 12), "blue").save(buffer, format="PNG")
        image = SimpleUploadedFile("image.png", buffer.getvalue(), content_type="image/png")
        self.client.post(url, {"action": "upload", "target": "E", "image": image})
        q.refresh_from_db()
        self.assertTrue(q.figures.filter(option_key="E").exists())
        self.assertIn("E", [opt["key"] for opt in q.options])
        self.assertEqual(practice_question_error(q), "")
        exam = self.create_exam(paper)
        bank = exam.questions.get(source_extracted_item=q)
        self.assertTrue(bank.options.get(option_key="E").figures.exists())


    def test_optional_cloud_requires_explicit_consent_configuration_and_page_limits(self):
        from .test_cloud_pipeline import CLOUD_SETTINGS, synthetic_pdf
        def post(data):
            return self.client.post(reverse("questions:practice_upload"), {
                "title": "Cloud request", "pdf_file": SimpleUploadedFile("cloud.pdf", synthetic_pdf(), content_type="application/pdf"),
                "extraction_mode": "cloud", **data,
            })
        with override_settings(**CLOUD_SETTINGS), patch("apps.questions.practice_views.extract_cloud_paper") as task:
            response = post({})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Explicit consent is required")
            task.assert_not_called()
            self.assertFalse(QuestionPaperUpload.objects.exists())
        with override_settings(PDF_CLOUD_ENABLED=False):
            response = post({"cloud_consent": "on"})
            self.assertContains(response, "Cloud extraction is not configured")
        limits = {**CLOUD_SETTINGS, "PDF_CLOUD_MAX_PAGES": 1}
        with override_settings(**limits):
            response = post({"cloud_consent": "on"})
            self.assertContains(response, "limited to 1 pages")
        self.assertFalse(QuestionPaperUpload.objects.exists())

    def test_consented_mock_cloud_private_files_image_pool_and_cbt(self):
        from unittest.mock import Mock
        from .cloud_extractor import CloudPDFQuestionExtractor
        from .test_cloud_pipeline import CLOUD_SETTINGS, synthetic_pdf, mathpix_result, gemini_questions
        mathpix = Mock(pdf_id="mock-private-id")
        mathpix.read_pdf.return_value = mathpix_result()
        mathpix.delete.return_value = True
        gemini = Mock()
        gemini.interpret.return_value = gemini_questions()
        def mocked_task(paper_id):
            return CloudPDFQuestionExtractor(QuestionPaperUpload.objects.get(pk=paper_id), mathpix, gemini).extract()
        with override_settings(**CLOUD_SETTINGS), patch("apps.questions.practice_views.extract_cloud_paper", side_effect=mocked_task):
            response = self.client.post(reverse("questions:practice_upload"), {
                "title": "Private cloud test", "extraction_mode": "cloud", "cloud_consent": "on",
                "pdf_file": SimpleUploadedFile("cloud.pdf", synthetic_pdf(), content_type="application/pdf"),
            })
        self.assertEqual(response.status_code, 302)
        paper = QuestionPaperUpload.objects.get(title="Private cloud test")
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.EXTRACTED, paper.extraction_log)
        self.assertIsNotNone(paper.cloud_consent_at)
        mathpix.read_pdf.assert_called_once()
        mathpix.delete.assert_called_once()
        gemini.interpret.assert_called_once()
        self.assertEqual(paper.figures.count(), 5)
        self.assertTrue(all(f.private_image and not f.image for f in paper.figures.all()))
        self.assertFalse(os.path.exists(os.path.join(self.root.name, "public")))
        pool_url = reverse("questions:practice_figure_pool", args=[paper.pk])
        self.assertEqual(self.client.get(pool_url).status_code, 200)
        unassigned = paper.figures.get(question__isnull=True)
        question = paper.extracted_questions.get(question_number=2)
        response = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {"duration_minutes": "60", "acknowledge_review": "on"})
        self.assertContains(response, "Review or acknowledge unassigned images")
        self.client.post(pool_url, {"action": "assign", "figure_id": unassigned.pk, "question_id": question.pk, "target": "A"})
        unassigned.refresh_from_db()
        self.assertEqual(unassigned.question_id, question.pk)
        self.assertEqual(unassigned.option_key, "A")
        response = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {"duration_minutes": "60", "acknowledge_review": "on"})
        self.assertEqual(response.status_code, 302)
        paper.refresh_from_db()
        attempt = self.start(paper.practice_exam)
        session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(session.status_code, 200)
        self.assertEqual(paper.practice_exam.questions.filter(figures__private_image__isnull=False).distinct().count(), 2)
        for figure in paper.figures.all():
            self.assertNotContains(session, "/media/" + figure.private_image.name)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("questions:practice_figure_file", args=[unassigned.pk])).status_code, 404)

    def test_cloud_worker_rechecks_consent_and_removes_private_partial_figures_on_failure(self):
        from pathlib import Path
        from unittest.mock import Mock
        from apps.exams.models import Category
        from .cloud_extractor import CloudPDFQuestionExtractor
        from .test_cloud_pipeline import CLOUD_SETTINGS, synthetic_pdf, mathpix_result, gemini_questions
        category = Category.objects.create(name="Private cloud category", slug="private-cloud-category", is_active=False)
        paper = QuestionPaperUpload.objects.create(
            title="No consent", category=category, uploaded_by=self.student, is_student_private=True,
            extraction_mode="cloud", private_pdf_file=SimpleUploadedFile("cloud.pdf", synthetic_pdf()),
        )
        mathpix, gemini = Mock(pdf_id="mock-id"), Mock()
        with override_settings(**CLOUD_SETTINGS):
            self.assertEqual(CloudPDFQuestionExtractor(paper, mathpix, gemini).extract(), 0)
        mathpix.read_pdf.assert_not_called()
        paper.refresh_from_db()
        self.assertEqual(paper.status, QuestionPaperUpload.ProcessingStatus.FAILED)
        paper.status = QuestionPaperUpload.ProcessingStatus.PENDING
        paper.cloud_consent_at = timezone.now()
        paper.save(update_fields=["status", "cloud_consent_at"])
        mathpix.read_pdf.return_value = mathpix_result()
        mathpix.delete.return_value = True
        gemini.interpret.return_value = gemini_questions()
        original = ExtractedFigure.save
        calls = []
        def fail_on_second(instance, *args, **kwargs):
            calls.append(instance)
            if len(calls) == 2:
                raise RuntimeError("simulated failure")
            return original(instance, *args, **kwargs)
        with override_settings(**CLOUD_SETTINGS), patch.object(ExtractedFigure, "save", fail_on_second):
            self.assertEqual(CloudPDFQuestionExtractor(paper, mathpix, gemini).extract(), 0)
        self.assertFalse(paper.extracted_questions.exists())
        self.assertFalse(paper.figures.exists())
        for figure in calls:
            self.assertFalse(Path(figure.private_image.path).exists())



    def test_owner_deletes_private_paper_test_attempts_and_files(self):
        paper = self.upload()
        question = paper.extracted_questions.first()
        self.client.post(reverse("questions:practice_figures", args=[question.pk]), {
            "action": "crop", "page_number": question.page_number, "x0": ".1", "y0": ".1", "x1": ".3", "y1": ".3", "target": "A",
        })
        paths = [paper.private_pdf_file.path, question.private_image.path, question.figures.get().private_image.path]
        exam = self.create_exam(paper)
        attempt = self.start(exam)
        bank_ids = list(exam.questions.values_list("id", flat=True))
        delete_url = reverse("questions:practice_delete", args=[paper.pk])
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(delete_url).status_code, 404)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(delete_url).status_code, 403)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(delete_url).status_code, 405)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(delete_url)
        self.assertRedirects(response, reverse("questions:practice_list"), fetch_redirect_response=False)
        self.assertFalse(QuestionPaperUpload.objects.filter(pk=paper.pk).exists())
        self.assertFalse(Exam.objects.filter(pk=exam.pk).exists())
        self.assertFalse(ExamAttempt.objects.filter(pk=attempt.pk).exists())
        self.assertFalse(Question.objects.filter(pk__in=bank_ids).exists())
        for path in paths:
            self.assertFalse(os.path.exists(path))

    def test_deleting_a_pending_paper_does_not_race_extraction(self):
        paper = self.upload()
        paper.status = QuestionPaperUpload.ProcessingStatus.PROCESSING
        paper.save(update_fields=["status"])
        response = self.client.post(reverse("questions:practice_delete", args=[paper.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(QuestionPaperUpload.objects.filter(pk=paper.pk).exists())
        self.assertTrue(os.path.exists(paper.private_pdf_file.path))



    def test_admin_parent_deletion_cannot_preview_or_cascade_private_material(self):
        paper = self.upload()
        category = paper.category
        exam = self.create_exam(paper)
        self.start(exam)
        self.client.force_login(self.admin)
        # Admin's normal deletion confirmation traverses related objects; it
        # must never list the private paper or test title through its parent.
        self.assertEqual(self.client.get(reverse("admin:accounts_user_delete", args=[self.student.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:accounts_user_delete", args=[self.student.pk]), {"post": "yes"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("accounts:admin_delete", args=[self.student.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("admin:exams_category_delete", args=[category.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:exams_category_delete", args=[category.pk]), {"post": "yes"}).status_code, 403)
        for model, pk in (("accounts_user", self.student.pk), ("exams_category", category.pk)):
            url = reverse("admin:" + model + "_changelist")
            listing = self.client.get(url)
            self.assertNotContains(listing, 'value="delete_selected"')
            response = self.client.post(url, {"action": "delete_selected", "_selected_action": [pk]})
            self.assertNotContains(response, paper.title)
        response = self.client.post(reverse("exams:category_delete", args=[category.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(QuestionPaperUpload.objects.filter(pk=paper.pk).exists())
        self.assertTrue(Exam.objects.filter(pk=exam.pk).exists())

    def test_category_deletion_protects_private_uncompiled_papers(self):
        paper = self.upload()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("admin:exams_category_delete", args=[paper.category_id])).status_code, 403)
        self.client.post(reverse("exams:category_delete", args=[paper.category_id]))
        self.assertTrue(QuestionPaperUpload.objects.filter(pk=paper.pk).exists())

    def test_timeout_sweep_handles_attempt_deleted_by_owner(self):
        paper = self.upload()
        exam = self.create_exam(paper)
        attempt = self.start(exam)
        attempt.expires_at = timezone.now() - timedelta(seconds=1)
        attempt.save(update_fields=["expires_at"])
        attempt.delete()
        self.assertFalse(attempt.auto_timeout_if_expired())



@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PrivateAttemptConcurrencyTests(TransactionTestCase):
    """Racing starts share one timer; SQLite may safely ask one client to retry."""
    def test_simultaneous_start_requests_share_one_attempt(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        from django.test import Client
        from apps.exams.models import Category
        from apps.payments.models import PracticeUsage
        from .models import QuestionOption

        student = User.objects.create_user(username="race", email="race@example.test", password="x", role=User.Roles.STUDENT)
        category = Category.objects.create(name="Practice race", slug="practice-race", is_active=False)
        exam = Exam.objects.create(
            title="Concurrent practice", category=category, creator=student, is_private_practice=True,
            status=Exam.ExamStatus.PUBLISHED, duration_minutes=45, is_free=True, max_attempts=0,
        )
        q = Question.objects.create(category=category, creator=student, is_private_practice=True, subject="General", question_text="Pick one")
        QuestionOption.objects.create(question=q, option_key="A", option_text="One")
        QuestionOption.objects.create(question=q, option_key="B", option_text="Two")
        ExamQuestion.objects.create(exam=exam, question=q, order=1)
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(student)
        barrier = Barrier(2)

        def post(client):
            close_old_connections()
            barrier.wait(timeout=10)
            response = client.post(reverse("exams:exam_start", args=[exam.slug]))
            close_old_connections()
            return response.status_code, response.get("Location"), response.get("Retry-After")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(post, clients))
        self.assertTrue(all(status in (302, 503) for status, _, _ in results), results)
        self.assertIn(302, [status for status, _, _ in results])
        self.assertEqual(ExamAttempt.objects.filter(exam=exam, status="in_progress").count(), 1)
        attempt = ExamAttempt.objects.get(exam=exam)
        session_url = reverse("exams:exam_session", args=[attempt.attempt_uuid])
        for index, (status, location, retry_after) in enumerate(results):
            if status == 503:
                self.assertEqual(retry_after, "1")
                # Once contention has cleared, the same client resumes the
                # original attempt rather than creating a new timer or quota use.
                response = clients[index].post(reverse("exams:exam_start", args=[exam.slug]))
                self.assertEqual(response.status_code, 302)
                location = response.url
            self.assertEqual(location, session_url)
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at - attempt.started_at, timedelta(minutes=45))
        self.assertEqual(ExamAttempt.objects.filter(exam=exam).count(), 1)
        self.assertEqual(PracticeUsage.objects.get(user=student).attempts, 1)
