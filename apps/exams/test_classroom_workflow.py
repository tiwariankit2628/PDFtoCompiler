"""Real local PDF-to-classroom regression tests (no provider calls or mocks)."""
import csv
import io
import json
import os
import tempfile
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import skipUnless

import pymupdf as fitz
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.models import ClassroomSession, Exam, ExamAttempt
from apps.exams.tasks import expire_classroom_attempts
from apps.questions.models import AnswerStatus, QuestionPaperUpload
from apps.questions.practice_forms import practice_question_error
from apps.questions.test_helpers import response_body


def classroom_pdf():
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((35, 70), "Q1 What is two plus two?")
    page.draw_rect(fitz.Rect(75, 85, 150, 115))
    page.insert_text((45, 140), "(A) Four")
    page.insert_text((45, 160), "(B) Five")
    page.insert_text((45, 180), "(C) Six")
    page.insert_text((45, 200), "(D) Seven")
    page.insert_text((35, 290), "Q2 Which color is the sky?")
    page.insert_text((45, 315), "(A) Blue")
    page.insert_text((45, 335), "(B) Green")
    page.insert_text((45, 355), "(C) Orange")
    page.insert_text((45, 375), "(D) Red")
    data = doc.write()
    doc.close()
    return data


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ClassroomWorkflowTests(TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        override = override_settings(MEDIA_ROOT=os.path.join(root.name, "media"),
            STATIC_ROOT=os.path.join(root.name, "static"), PRIVATE_PRACTICE_ROOT=os.path.join(root.name, "private"))
        override.enable()
        self.addCleanup(override.disable)
        os.makedirs(os.path.join(root.name, "static"), exist_ok=True)
        self.teacher = User.objects.create_user(username="class_teacher", email="class_teacher@test.example", password="test", role=User.Roles.TEACHER)
        self.other_teacher = User.objects.create_user(username="class_other", email="class_other@test.example", password="test", role=User.Roles.TEACHER)
        self.student = User.objects.create_user(username="class_student", email="class_student@test.example", password="test", role=User.Roles.STUDENT, exam_track="jee")
        self.other_student = User.objects.create_user(username="class_student2", email="class_student2@test.example", password="test", role=User.Roles.STUDENT, exam_track="jee")

    def upload(self):
        self.client.force_login(self.teacher)
        response = self.client.post(reverse("questions:classroom_paper_upload"), {
            "title": "Local PDF classroom", "subject": "Science", "image_first": "on",
            "pdf_file": SimpleUploadedFile("paper.pdf", classroom_pdf(), content_type="application/pdf"),
            "target_type": "jee",
        })
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        paper = QuestionPaperUpload.objects.get(uploaded_by=self.teacher, is_classroom_private=True)
        self.assertEqual(paper.status, paper.ProcessingStatus.EXTRACTED, paper.extraction_log)
        self.assertEqual(paper.extracted_questions.count(), 2, paper.extraction_log)
        self.assertTrue(paper.private_pdf_file)
        self.assertFalse(paper.pdf_file)
        return paper

    def create_test(self, paper, keys="1: A", **extra):
        self.client.force_login(self.teacher)
        for question in paper.extracted_questions.prefetch_related("figures"):
            error = practice_question_error(question, allow_confirmed_crop_choices=True)
            self.assertFalse(error, error)
        response = self.client.post(reverse("questions:classroom_paper_review", args=[paper.pk]), {
            "creation_token": str(uuid.uuid4()), "duration_minutes": "37", "answer_key_text": keys,
            "confirm_review": "on", "acknowledge_review": "on", "acknowledge_unassigned": "on", **extra,
        })
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["form"].errors)
        session = ClassroomSession.objects.get(teacher=self.teacher, exam__is_classroom_only=True)
        return session

    def join_start(self, session, student=None):
        student = student or self.student
        self.client.force_login(student)
        joined = self.client.post(reverse("exams:classroom_join"), {"code": session.code.lower()})
        self.assertRedirects(joined, reverse("exams:classroom_take", args=[session.pk]), fetch_redirect_response=False)
        start = self.client.post(reverse("exams:classroom_take", args=[session.pk]))
        attempt = session.attempts.get(student=student, status=ExamAttempt.AttemptStatus.IN_PROGRESS)
        self.assertRedirects(start, reverse("exams:exam_session", args=[attempt.attempt_uuid]), fetch_redirect_response=False)
        return attempt

    def test_real_pdf_review_create_join_save_submit_and_private_access(self):
        paper = self.upload()
        review = self.client.get(reverse("questions:classroom_paper_review", args=[paper.pk]))
        self.assertContains(review, "Test preview")
        self.assertContains(review, "Original question image")
        session = self.create_test(paper)
        exam = session.exam
        self.assertEqual(exam.duration_minutes, 37)
        self.assertTrue(exam.is_classroom_only)
        self.assertEqual(exam.exam_questions.count(), 2)
        self.assertTrue(all(q.is_classroom_snapshot and q.is_image_first for q in exam.questions.all()))
        self.assertTrue(exam.questions.filter(figures__source="question_crop", figures__private_image__gt="").exists())
        attempt = self.join_start(session)
        self.assertEqual(attempt.expires_at - attempt.started_at, timedelta(minutes=37))
        screen = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(screen.status_code, 200)
        self.assertIn("no-store", screen["Cache-Control"])
        payload = screen.context["questions_data"]
        self.assertTrue(all(item["image_first"] for item in payload))
        self.assertNotContains(screen, '"is_correct"')
        image_url = payload[0]["crop_urls"][0]
        image = self.client.get(image_url)
        self.assertEqual(image.status_code, 200)
        self.assertIn("no-store", image["Cache-Control"])
        self.assertTrue(response_body(image))
        self.client.force_login(self.other_student)
        self.assertIn(self.client.get(image_url).status_code, (403, 404))
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[exam.slug])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("exams:exam_catalog")), exam.title)
        self.client.force_login(self.student)
        for item in payload:
            option = next(opt for opt in item["options"] if opt["key"] == "A")
            result = self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]), data=json.dumps({
                "response_id": item["response_id"], "selected_option_id": option["id"], "action": "navigate"}), content_type="application/json")
            self.assertEqual(result.json()["status"], "success")
        original_expiry = attempt.expires_at
        resumed = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertTrue(all(q["selected_option_id"] for q in resumed.context["questions_data"]))
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, original_expiry)
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), data="{}", content_type="application/json")
        attempt.refresh_from_db()
        self.assertEqual(attempt.score_obtained, Decimal("4.00"))
        self.assertEqual(attempt.responses.filter(is_correct=True).count(), 1)
        self.assertEqual(attempt.responses.filter(is_correct__isnull=True, selected_option__isnull=False).count(), 1)
        self.client.force_login(self.teacher)
        monitor = self.client.get(reverse("exams:classroom_monitor", args=[session.pk])).json()
        self.assertEqual(monitor["counts"]["submitted"], 1)
        self.assertEqual(monitor["rows"][0]["score_label"], "Partial · unranked")
        export = self.client.get(reverse("exams:classroom_export", args=[session.pk]))
        row = list(csv.DictReader(io.StringIO(export.content.decode())))[0]
        self.assertEqual(row["Rank"], "")
        self.assertEqual(row["Score (confirmed keys)"], "4.00")
        self.assertEqual(row["Ungraded"], "1")

    def test_private_source_page_and_join_code_pages_are_not_cacheable(self):
        paper = self.upload()
        question = paper.extracted_questions.order_by("question_number").first()
        page = self.client.get(reverse("questions:classroom_source_page", args=[question.pk, 1]))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page["Content-Type"], "image/png")
        self.assertEqual(page["X-Content-Type-Options"], "nosniff")
        self.assertTrue(page.content.startswith(b"\x89PNG"))
        self.assertIn("private", page["Cache-Control"])
        self.assertIn("no-store", page["Cache-Control"])
        for name in ("classroom_hub",):
            response = self.client.get(reverse(f"exams:{name}"))
            self.assertIn("private", response["Cache-Control"])
            self.assertIn("no-store", response["Cache-Control"])
        session = self.create_test(paper, keys="1:A, 2:A")
        self.client.force_login(self.student)
        join = self.client.get(reverse("exams:classroom_join") + f"?code={session.code}")
        self.assertIn("no-store", join["Cache-Control"])
        self.client.post(reverse("exams:classroom_join"), {"code": session.code})
        take = self.client.get(reverse("exams:classroom_take", args=[session.pk]))
        self.assertIn("private", take["Cache-Control"])
        self.assertIn("no-store", take["Cache-Control"])

    def test_image_only_choices_require_explicit_labels_and_actual_crop(self):
        paper = self.upload()
        question = paper.extracted_questions.order_by("question_number").first()
        crop = question.figures.get(source="question_crop", option_key="")
        question.options = []  # simulate a diagram-only choice list OCR could not parse
        question.correct_option = ""
        question.answer_status = AnswerStatus.NEEDS_REVIEW
        question.save(update_fields=["options", "correct_option", "answer_status"])
        self.assertIn("At least two", practice_question_error(question, allow_confirmed_crop_choices=True))
        edit_url = reverse("questions:classroom_question_edit", args=[question.pk])
        payload = {
            "question_number": question.question_number, "detected_type": question.detected_type,
            "question_text": question.question_text, "positive_marks": "4", "negative_marks": "1",
            "visible_crop_choices": ["A", "B", "C", "D"], "correct_option": "C", "confirm_answer_key": "on", "reviewed": "on",
        }
        response = self.client.post(edit_url, payload)
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        question.refresh_from_db()
        self.assertEqual([option["key"] for option in question.options], list("ABCD"))
        self.assertTrue(all(option["text"] == "" and option["visible_in_crop"] for option in question.options))
        self.assertEqual(question.correct_option, "C")
        self.assertEqual(question.answer_status, AnswerStatus.MANUALLY_VERIFIED)
        self.assertEqual(practice_question_error(question, allow_confirmed_crop_choices=True), "")
        self.assertIn("needs text", practice_question_error(question))  # student practice is unchanged

        # Removing the source crop must invalidate these labels, not silently
        # manufacture text or choices; a forged crop selection is rejected.
        crop.source = "manual_crop"
        crop.save(update_fields=["source"])
        question.refresh_from_db()
        self.assertIn("needs text", practice_question_error(question, allow_confirmed_crop_choices=True))
        response = self.client.post(edit_url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("visible_crop_choices", response.context["form"].errors)
        crop.source = "question_crop"
        crop.save(update_fields=["source"])
        session = self.create_test(paper, keys="")
        snapshot = session.exam.exam_questions.get(order=1).question
        self.assertEqual(list(snapshot.options.order_by("option_key").values_list("option_key", flat=True)), list("ABCD"))
        self.assertEqual(snapshot.options.get(is_correct=True).option_key, "C")
        self.assertTrue(all(not option.option_text for option in snapshot.options.all()))
        attempt = self.join_start(session)
        screen = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        item = screen.context["questions_data"][0]
        self.assertTrue(item["image_first"])
        self.assertEqual([option["key"] for option in item["options"]], list("ABCD"))
        self.assertTrue(all(not option["text"] for option in item["options"]))

    def test_private_paper_snapshot_code_and_image_access_are_isolated(self):
        paper = self.upload()
        question = paper.extracted_questions.first()
        self.other_teacher.assigned_categories.add(paper.category)
        private_urls = [
            reverse("questions:classroom_paper_review", args=[paper.pk]),
            reverse("questions:classroom_paper_pdf", args=[paper.pk]),
            reverse("questions:classroom_question_edit", args=[question.pk]),
            reverse("questions:classroom_figures", args=[question.pk]),
            reverse("questions:classroom_source_page", args=[question.pk, 1]),
            reverse("questions:classroom_figure_file", args=[question.figures.first().pk]),
            reverse("questions:paper_review", args=[paper.pk]),
            reverse("questions:extracted_edit", args=[question.pk]),
        ]
        self.client.force_login(self.other_teacher)
        for url in private_urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        self.assertNotContains(self.client.get(reverse("questions:paper_list")), paper.title)
        self.assertNotContains(self.client.get(reverse("exams:classroom_hub")), paper.title)
        session = self.create_test(paper, keys="1:A, 2:A")
        snapshot = session.exam.exam_questions.first().question
        self.client.force_login(self.other_teacher)
        self.assertEqual(self.client.get(reverse("questions:question_preview", args=[snapshot.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("exams:classroom_session", args=[session.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("exams:teacher_exam_analytics", args=[session.exam.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("questions:question_bank")), snapshot.question_text)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("exams:exam_instructions", args=[session.exam.slug])).status_code, 404)
        self.assertEqual(self.client.get(reverse("exams:classroom_take", args=[session.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("questions:classroom_paper_pdf", args=[paper.pk])).status_code, 403)
        self.assertNotContains(self.client.get(reverse("exams:exam_catalog")), session.exam.title)
        # A participant's image URL cannot be used by a second student or
        # another teacher, even if they are in the same category.
        attempt = self.join_start(session)
        data = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).context["questions_data"]
        image_url = data[0]["crop_urls"][0]
        self.client.force_login(self.other_teacher)
        self.assertIn(self.client.get(image_url).status_code, (403, 404))
        self.client.force_login(self.other_student)
        self.assertIn(self.client.get(image_url).status_code, (403, 404))
        self.assertEqual(self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).status_code, 403)

    def test_hidden_results_release_and_active_retake_do_not_leak_images(self):
        paper = self.upload()
        session = self.create_test(paper, keys="1:A, 2:A", allow_retakes="on")
        attempt = self.join_start(session)
        image_url = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).context["questions_data"][0]["crop_urls"][0]
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), data="{}", content_type="application/json")
        detail = reverse("exams:student_result_detail", args=[attempt.attempt_uuid])
        review = reverse("exams:student_result_review", args=[attempt.attempt_uuid])
        pdf = reverse("exams:student_result_pdf", args=[attempt.attempt_uuid])
        self.assertContains(self.client.get(detail), "Results are under evaluation")
        unreleased_review = self.client.get(review)
        self.assertNotContains(unreleased_review, "What is two plus two")
        self.assertNotContains(unreleased_review, image_url)
        self.assertEqual(self.client.get(pdf).status_code, 403)
        self.assertEqual(self.client.get(image_url).status_code, 403)
        self.client.force_login(self.other_teacher)
        self.assertEqual(self.client.post(reverse("exams:classroom_control", args=[session.pk]),
                                          {"action": "release_results", "confirm": "yes"}).status_code, 403)
        self.client.force_login(self.teacher)
        self.client.post(reverse("exams:classroom_control", args=[session.pk]),
                         {"action": "release_results", "confirm": "yes"})
        session.refresh_from_db()
        self.assertTrue(session.results_released)
        self.client.force_login(self.student)
        self.assertContains(self.client.get(detail), "SCORE ON CONFIRMED KEYS", html=False)
        released_review = self.client.get(review)
        self.assertContains(released_review, image_url)
        self.assertContains(released_review, "Original question image")
        self.assertContains(released_review, "What is two plus two")  # available in collapsed accessible fallback
        self.assertContains(released_review, '<details data-image-fallback class="small mb-3" >')
        self.assertEqual(self.client.get(pdf).status_code, 200)
        image = self.client.get(image_url)
        self.assertEqual(image.status_code, 200)
        self.assertTrue(response_body(image))
        # A new live retake prevents viewing previous solutions and their images.
        second = self.join_start(session)
        self.assertNotEqual(attempt.pk, second.pk)
        self.assertRedirects(self.client.get(review), reverse("exams:exam_session", args=[second.attempt_uuid]), fetch_redirect_response=False)
        self.assertEqual(self.client.get(image_url).status_code, 403)

    def test_teacher_keys_are_snapshotted_and_create_post_is_idempotent(self):
        paper = self.upload()
        token = str(uuid.uuid4())
        data = {"creation_token": token, "duration_minutes": "21", "answer_key_text": "1:A, 2:B",
                "confirm_review": "on", "acknowledge_review": "on", "acknowledge_unassigned": "on"}
        url = reverse("questions:classroom_paper_review", args=[paper.pk])
        first = self.client.post(url, data)
        self.assertEqual(first.status_code, 302, first.context and first.context["form"].errors)
        second = self.client.post(url, data)
        self.assertEqual(first["Location"], second["Location"])
        self.assertEqual(ClassroomSession.objects.filter(teacher=self.teacher).count(), 1)
        session = ClassroomSession.objects.get(teacher=self.teacher)
        self.assertEqual(session.exam.duration_minutes, 21)
        snapshot = session.exam.exam_questions.get(order=1).question
        self.assertEqual(snapshot.options.get(is_correct=True).option_key, "A")
        staged = paper.extracted_questions.order_by("question_number").first()
        staged.question_text = "Changed later on the staged paper"
        staged.correct_option = "B"
        staged.answer_status = AnswerStatus.MANUALLY_VERIFIED
        staged.save(update_fields=["question_text", "correct_option", "answer_status"])
        snapshot.refresh_from_db()
        self.assertNotEqual(snapshot.question_text, staged.question_text)
        self.assertEqual(snapshot.options.get(is_correct=True).option_key, "A")
        data.update(creation_token=str(uuid.uuid4()), answer_key_text="")
        created = self.client.post(url, data)
        self.assertEqual(created.status_code, 302, created.context and created.context["form"].errors)
        self.assertEqual(ClassroomSession.objects.filter(teacher=self.teacher).count(), 2)
        newest = ClassroomSession.objects.exclude(pk=session.pk).get(teacher=self.teacher)
        self.assertEqual(newest.exam.exam_questions.get(order=1).question.options.get(is_correct=True).option_key, "B")

    def test_timeout_is_server_authoritative_and_sweep_preserves_answers(self):
        paper = self.upload()
        session = self.create_test(paper, keys="1:A, 2:A")
        attempt = self.join_start(session)
        data = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).context["questions_data"]
        choice = next(option for option in data[0]["options"] if option["key"] == "A")
        self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]), data=json.dumps({
            "response_id": data[0]["response_id"], "selected_option_id": choice["id"], "action": "navigate"}), content_type="application/json")
        early = self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), data='{"is_timeout":true}', content_type="application/json")
        self.assertEqual(early.json()["status"], "active")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.IN_PROGRESS)
        attempt.started_at = timezone.now() - timedelta(minutes=38)
        attempt.expires_at = attempt.started_at + timedelta(minutes=37)
        attempt.save(update_fields=["started_at", "expires_at"])
        self.assertEqual(expire_classroom_attempts(), 1)
        self.assertEqual(expire_classroom_attempts(), 0)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TIMED_OUT)
        self.assertEqual(attempt.score_obtained, Decimal("4.00"))
        self.assertEqual(attempt.time_taken_seconds, 37 * 60)
        late = self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]), data=json.dumps({
            "response_id": data[0]["response_id"], "action": "clear"}), content_type="application/json")
        self.assertEqual(late.json()["status"], "expired")
        self.assertEqual(attempt.responses.get(pk=data[0]["response_id"]).selected_option_id, choice["id"])

    def test_ranked_export_is_topper_first_and_counts_distinct_retakes(self):
        paper = self.upload()
        session = self.create_test(paper, keys="1:A, 2:A", allow_retakes="on")
        self.student.first_name = "=Dangerous"
        self.student.save(update_fields=["first_name"])
        for student, answers in [(self.student, "AA"), (self.other_student, "BA")]:
            attempt = self.join_start(session, student)
            screen = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
            for item, key in zip(screen.context["questions_data"], answers):
                choice = next(option for option in item["options"] if option["key"] == key)
                self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]), data=json.dumps({
                    "response_id": item["response_id"], "selected_option_id": choice["id"], "action": "navigate"}), content_type="application/json")
            self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), data="{}", content_type="application/json")
        retake = self.join_start(session, self.student)
        self.client.force_login(self.teacher)
        monitor = self.client.get(reverse("exams:classroom_monitor", args=[session.pk])).json()
        self.assertEqual(monitor["counts"]["joined"], 2)
        self.assertEqual(monitor["counts"]["started"], 2)
        self.assertEqual(monitor["counts"]["in_progress"], 1)
        self.assertEqual(monitor["counts"]["submitted"], 2)
        row = next(row for row in monitor["rows"] if row["username"] == self.student.username)
        self.assertEqual(row["status"], "in_progress")
        self.assertEqual(row["score"], 8.0)
        self.assertNotEqual(row["attempt_uuid"], str(retake.attempt_uuid))
        csv_response = self.client.get(reverse("exams:classroom_export", args=[session.pk]))
        rows = list(csv.DictReader(io.StringIO(csv_response.content.decode())))
        self.assertEqual([row["Username"] for row in rows], [self.student.username, self.other_student.username])
        self.assertEqual([row["Rank"] for row in rows], ["1", "2"])
        self.assertEqual(rows[0]["Name"], "'=Dangerous")
        self.assertEqual(Decimal(rows[0]["Score (confirmed keys)"]), Decimal("8"))
        self.assertEqual(Decimal(rows[1]["Score (confirmed keys)"]), Decimal("3"))
        self.assertEqual(rows[0]["Status"], "in_progress")

    @skipUnless(os.environ.get("TESSDATA_PREFIX") and os.path.isfile(os.path.join(os.environ.get("TESSDATA_PREFIX", ""), "eng.traineddata")),
                "Optional real local Tesseract language data required")
    def test_scanned_pdf_runs_real_local_ocr_and_preserves_original_crops(self):
        digital = fitz.open(stream=classroom_pdf(), filetype="pdf")
        scanned = fitz.open()
        for page in digital:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            target = scanned.new_page(width=page.rect.width, height=page.rect.height)
            target.insert_image(target.rect, stream=pix.tobytes("png"))
        pdf = scanned.write()
        scanned.close()
        digital.close()
        self.client.force_login(self.teacher)
        response = self.client.post(reverse("questions:classroom_paper_upload"), {
            "title": "Real local OCR classroom", "image_first": "on",
            "pdf_file": SimpleUploadedFile("scanned.pdf", pdf, content_type="application/pdf"),
            "target_type": "jee",
        })
        self.assertEqual(response.status_code, 302)
        paper = QuestionPaperUpload.objects.get(uploaded_by=self.teacher, title="Real local OCR classroom")
        self.assertTrue(paper.ocr_used, paper.extraction_log)
        self.assertEqual(paper.status, paper.ProcessingStatus.EXTRACTED, paper.extraction_log)
        self.assertEqual(paper.extracted_questions.count(), 2, paper.extraction_log)
        self.assertTrue(paper.figures.filter(source="question_crop", private_image__gt="").exists())
        self.assertTrue(all(q.is_flagged for q in paper.extracted_questions.all()))

    def test_forged_remote_extraction_modes_are_rejected(self):
        self.client.force_login(self.teacher)
        for mode in ("gemini", "cloud", "local_mineru", "__more__"):
            with self.subTest(mode=mode):
                response = self.client.post(reverse("questions:classroom_paper_upload"), {
                    "title": "Rejected provider", "extraction_mode": mode,
                    "pdf_file": SimpleUploadedFile("paper.pdf", classroom_pdf(), content_type="application/pdf"),
                    "target_type": "jee",
                })
                self.assertEqual(response.status_code, 200)
                self.assertIn("extraction_mode", response.context["form"].errors)
        self.assertFalse(QuestionPaperUpload.objects.filter(uploaded_by=self.teacher).exists())

    def test_manual_original_recrop_is_private_and_snapshot_is_independent(self):
        paper = self.upload()
        question = paper.extracted_questions.order_by("question_number").first()
        crop = question.figures.get(source="question_crop", option_key="")
        url = reverse("questions:classroom_figures", args=[question.pk])
        page = self.client.get(url)
        self.assertContains(page, "Original question image")
        response = self.client.post(url, {"action": "crop", "target": "__crop__", "replace_id": crop.pk,
            "page_number": "1", "x0": ".04", "y0": ".04", "x1": ".75", "y1": ".29"})
        self.assertEqual(response.status_code, 302)
        crop.refresh_from_db()
        self.assertEqual(crop.source, "question_crop")
        self.assertEqual(crop.option_key, "")
        with crop.private_image.open("rb") as handle:
            original = handle.read()
        self.assertGreater(len(original), 100)
        session = self.create_test(paper, keys="1:A, 2:A")
        snapshot = session.exam.exam_questions.get(order=1).question
        copied = snapshot.figures.get(source="question_crop", option__isnull=True)
        self.assertNotEqual(copied.private_image.name, crop.private_image.name)
        with copied.private_image.open("rb") as handle:
            self.assertEqual(handle.read(), original)
        self.client.post(url, {"action": "crop", "target": "__crop__", "replace_id": crop.pk,
            "page_number": "1", "x0": ".05", "y0": ".05", "x1": ".50", "y1": ".24"})
        crop.refresh_from_db()
        with crop.private_image.open("rb") as handle:
            self.assertNotEqual(handle.read(), original)
        with copied.private_image.open("rb") as handle:
            self.assertEqual(handle.read(), original)
        attempt = self.join_start(session)
        payload = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).context["questions_data"]
        image = self.client.get(payload[0]["crop_urls"][0])
        self.assertEqual(response_body(image), original)

    def test_final_answer_validation_is_atomic_and_rejects_cross_attempt_choices(self):
        paper = self.upload()
        session = self.create_test(paper, keys="1:A, 2:A")
        first = self.join_start(session, self.student)
        second = self.join_start(session, self.other_student)
        second_response = second.responses.first()
        self.client.force_login(self.student)
        submit_url = reverse("exams:exam_submit", args=[first.attempt_uuid])
        for malformed in ("[1]", "{", '{"is_timeout":"false"}'):
            with self.subTest(malformed=malformed):
                invalid = self.client.post(submit_url, data=malformed, content_type="application/json")
                self.assertEqual(invalid.status_code, 400)
                first.refresh_from_db()
                self.assertEqual(first.status, ExamAttempt.AttemptStatus.IN_PROGRESS)
        invalid = self.client.post(submit_url, data=json.dumps({"last_response": {
            "response_id": second_response.pk, "selected_option_id": None}}), content_type="application/json")
        self.assertEqual(invalid.status_code, 400)
        first.refresh_from_db()
        self.assertEqual(first.status, ExamAttempt.AttemptStatus.IN_PROGRESS)
        response = first.responses.order_by("order").first()
        unrelated_option = first.responses.order_by("order").last().exam_question.question.options.first()
        invalid = self.client.post(submit_url, data=json.dumps({"last_response": {
            "response_id": response.pk, "selected_option_id": unrelated_option.pk}}), content_type="application/json")
        self.assertEqual(invalid.status_code, 400)
        choice = response.exam_question.question.options.get(option_key="A")
        valid = self.client.post(submit_url, data=json.dumps({"last_response": {
            "response_id": response.pk, "selected_option_id": choice.pk}}), content_type="application/json")
        self.assertEqual(valid.json()["status"], "submitted")
        first.refresh_from_db()
        response.refresh_from_db()
        self.assertEqual(response.selected_option_id, choice.pk)
        self.assertEqual(first.score_obtained, Decimal("4.00"))

    def test_malformed_security_payload_does_not_change_the_attempt(self):
        paper = self.upload()
        session = self.create_test(paper, keys="1:A, 2:A", strict_exam_mode="on")
        attempt = self.join_start(session)
        url = reverse("exams:exam_log_event", args=[attempt.attempt_uuid])
        for payload in ("[]", "null", "{\"event_type\":"):
            with self.subTest(payload=payload):
                response = self.client.post(url, data=payload, content_type="application/json")
                self.assertEqual(response.status_code, 400)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.IN_PROGRESS)
        self.assertEqual(attempt.tab_switch_warnings, 0)
        self.assertFalse(attempt.security_logs.exists())

    def test_monitor_finalizes_an_expired_offline_attempt(self):
        paper = self.upload()
        session = self.create_test(paper, keys="1:A, 2:A")
        attempt = self.join_start(session)
        attempt.expires_at = timezone.now() - timedelta(seconds=1)
        attempt.save(update_fields=["expires_at"])
        self.client.force_login(self.teacher)
        monitor = self.client.get(reverse("exams:classroom_monitor", args=[session.pk]))
        self.assertEqual(monitor.status_code, 200)
        self.assertEqual(monitor.json()["counts"]["in_progress"], 0)
        self.assertEqual(monitor.json()["counts"]["timed_out"], 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TIMED_OUT)
