"""Phase 7 regression coverage: results, bank versioning, quotas and privacy."""
import io
import json
import os
import tempfile
from decimal import Decimal

import pymupdf as fitz
from PIL import Image, ImageDraw
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Exam, ExamAttempt
from apps.payments.models import PracticePlan, PracticeUsage
from .models import Question, QuestionPaperUpload
from .practice_forms import practice_question_error
from .test_pdf_layout import make_two_column_pdf


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"], CELERY_TASK_ALWAYS_EAGER=True)
class PracticePhase7Tests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        override = override_settings(MEDIA_ROOT=os.path.join(self.root.name, "media"),
                                     STATIC_ROOT=os.path.join(self.root.name, "static"),
                                     PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private"))
        override.enable()
        self.addCleanup(override.disable)
        os.makedirs(os.path.join(self.root.name, "static"))
        self.student = User.objects.create_user("phase7-owner", email="phase7@example.test", password="x", role=User.Roles.STUDENT)
        self.other = User.objects.create_user("phase7-other", email="other7@example.test", password="x", role=User.Roles.STUDENT)
        self.client.force_login(self.student)
        self.free = PracticePlan.objects.get(is_default=True)

    def upload(self, title="Science", *, image_first=False):
        response = self.client.post(reverse("questions:practice_upload"), {
            "title": title, "subject": "Physics", "image_first": "on" if image_first else "",
            "pdf_file": SimpleUploadedFile("test.pdf", make_two_column_pdf(), content_type="application/pdf")})
        self.assertEqual(response.status_code, 302)
        paper = QuestionPaperUpload.objects.get(title=title, uploaded_by=self.student)
        for question in paper.extracted_questions.filter(include_in_practice=True).prefetch_related("figures"):
            if practice_question_error(question):
                self.client.post(reverse("questions:practice_question_include", args=[question.pk]))
        return paper

    def create_exam(self, paper, keys="", target="40"):
        response = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {
            "duration_minutes": "30", "passing_percentage": target, "answer_key_text": keys, "confirm_answer_key": "on",
            "acknowledge_review": "on", "acknowledge_unassigned": "on"})
        self.assertEqual(response.status_code, 302)
        paper.refresh_from_db()
        self.assertIsNotNone(paper.practice_exam_id)
        return paper.practice_exam

    def start(self, exam):
        r = self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        self.assertEqual(r.status_code, 302)
        return ExamAttempt.objects.get(exam=exam, status="in_progress")

    def submit(self, attempt, response=None, option=None, seconds=12):
        body = {"is_timeout": False}
        if response:
            body["last_response"] = {"response_id": response.pk, "selected_option_id": option.pk if option else None,
                                     "time_spent_delta": seconds, "is_marked_for_review": False}
        return self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]),
                                json.dumps(body), content_type="application/json")

    def bank_edit_data(self, question, **changes):
        options = {o.option_key: o.option_text for o in question.options.all()}
        data = {"question_text": question.question_text, "subject": question.subject,
                "topic": "Mechanics", "chapter": "Motion", "difficulty": "hard", "language": "bilingual",
                "positive_marks": str(question.positive_marks), "negative_marks": str(question.negative_marks),
                "explanation": "Checked explanation", "correct_option": "B",
                **{f"option_{k}": options.get(k, "") for k in "ABCDE"}}
        data.update(changes)
        return data

    def test_result_counts_history_and_atomic_last_answer(self):
        paper = self.upload()
        first = paper.extracted_questions.filter(include_in_practice=True).first()
        exam = self.create_exam(paper, f"{first.question_number}: A")
        attempt = self.start(exam)
        rows = list(attempt.responses.order_by("order"))
        keyed = rows[0]
        unkeyed = rows[1]
        self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]), data=json.dumps({
            "response_id": unkeyed.pk, "selected_option_id": unkeyed.exam_question.question.options.first().pk,
            "action": "save_next", "time_spent_delta": 17}), content_type="application/json")
        correct = keyed.exam_question.question.options.get(option_key="A")
        self.submit(attempt, keyed, correct, 23)
        attempt.refresh_from_db(); keyed.refresh_from_db()
        self.assertEqual(attempt.score_obtained, keyed.exam_question.positive_marks)
        self.assertEqual(keyed.time_spent_seconds, 23)
        self.assertEqual(attempt.correct_count, 1)
        self.assertEqual(attempt.incorrect_count, 0)
        result = self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        self.assertEqual(result.context["counts"]["ungraded"], 1)
        self.assertEqual(result.context["accuracy"], 100)
        self.assertEqual(result.context["outcome"], "Not determined")
        self.assertContains(result, "No answer key — ungraded")
        self.assertContains(result, "subject performance")
        self.assertContains(result, "Question-wise review")
        again = self.start(exam)
        self.assertNotEqual(attempt.pk, again.pk)
        self.assertEqual(again.responses.filter(selected_option__isnull=False).count(), 0)
        self.assertEqual(again.expires_at - again.started_at, attempt.expires_at - attempt.started_at)
        self.submit(again)
        history = self.client.get(reverse("questions:practice_history"))
        self.assertEqual(len(history.context["page_obj"]), 2)
        result = self.client.get(reverse("questions:practice_result", args=[again.attempt_uuid]))
        self.assertEqual(result.context["history"].count(), 2)
        self.assertEqual(PracticeUsage.objects.get(user=self.student).attempts, 2)

    def test_answer_save_rejects_malformed_payloads_and_other_question_options(self):
        exam = self.create_exam(self.upload())
        attempt = self.start(exam)
        rows = list(attempt.responses.order_by("order"))
        url = reverse("exams:exam_save_answer", args=[attempt.attempt_uuid])
        payloads = [[], {"response_id":"not-an-id"}, {"response_id":rows[0].pk, "action":"bogus"},
                    {"response_id":rows[0].pk, "selected_option_id":rows[1].exam_question.question.options.first().pk}]
        for payload in payloads:
            response = self.client.post(url, json.dumps(payload), content_type="application/json")
            self.assertEqual(response.status_code, 400)
        rows[0].refresh_from_db()
        self.assertIsNone(rows[0].selected_option_id)

    def test_unkeyed_history_never_presents_zero_as_a_graded_score(self):
        exam = self.create_exam(self.upload(), keys="", target="")
        attempt = self.start(exam)
        self.submit(attempt)
        history = self.client.get(reverse("questions:practice_history"))
        self.assertContains(history, "Not graded")
        self.assertFalse(history.context["page_obj"][0].has_verified_key)

    def test_pass_fail_only_for_complete_confirmed_key(self):
        paper = self.upload()
        included = list(paper.extracted_questions.filter(include_in_practice=True))
        keys = ", ".join(f"{q.question_number}: A" for q in included)
        exam = self.create_exam(paper, keys, "50")
        attempt = self.start(exam)
        for response in attempt.responses.all():
            response.selected_option = response.exam_question.question.options.get(option_key="A")
            response.save(update_fields=["selected_option"])
        self.submit(attempt)
        result = self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        self.assertEqual(result.context["outcome"], "Passed")
        self.assertTrue(result.context["fully_keyed"])
        self.assertContains(result, "target 50.00%")

    def test_unverified_or_ambiguous_key_and_explanation_are_never_revealed(self):
        paper = self.upload()
        first = paper.extracted_questions.filter(include_in_practice=True).first()
        exam = self.create_exam(paper, f"{first.question_number}: A")
        q = exam.questions.order_by("exam_instances__order").first()
        q.key_verified = False
        q.explanation = "SECRET-UNVERIFIED-ANSWER"
        q.save(update_fields=["key_verified", "explanation"])
        attempt = self.start(exam)
        response = attempt.responses.first()
        self.submit(attempt, response, q.options.get(option_key="A"))
        attempt.refresh_from_db(); response.refresh_from_db()
        self.assertIsNone(response.is_correct)
        page = self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        self.assertNotContains(page, "SECRET-UNVERIFIED-ANSWER")
        self.assertNotContains(page, "Confirmed key</span>")
        report = self.client.get(reverse("questions:practice_report", args=[attempt.attempt_uuid]))
        document = fitz.open(stream=report.content, filetype="pdf")
        self.assertNotIn("SECRET-UNVERIFIED-ANSWER", "".join(p.get_text() for p in document))
        document.close()

    def test_owner_report_has_embedded_devanagari_font_and_figures(self):
        paper = self.upload()
        first = paper.extracted_questions.filter(include_in_practice=True).first()
        first.question_text = "भारत में गति का प्रश्न १२३ / Motion question"
        first.save(update_fields=["question_text"])
        exam = self.create_exam(paper, f"{first.question_number}: A")
        attempt = self.start(exam)
        response = attempt.responses.first()
        self.submit(attempt, response, response.exam_question.question.options.get(option_key="A"))
        pdf = self.client.get(reverse("questions:practice_report", args=[attempt.attempt_uuid]))
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertIn("no-store", pdf["Cache-Control"])
        with fitz.open(stream=pdf.content, filetype="pdf") as doc:
            self.assertGreaterEqual(len(doc), 1)
            fonts = [item[3] for p in doc for item in p.get_fonts()]
            self.assertTrue(any("Devanagari" in font for font in fonts))
            self.assertIn("Motion question", "".join(p.get_text() for p in doc))
            self.assertTrue(any(p.get_images() for p in doc))
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("questions:practice_report", args=[attempt.attempt_uuid])).status_code, 404)

    def test_practice_report_keeps_tall_original_image_within_page(self):
        paper = self.upload("Tall report", image_first=True)
        exam = self.create_exam(paper)
        attempt = self.start(exam)
        response = attempt.responses.first()
        question = response.exam_question.question
        crop = question.figures.filter(source="question_crop").first()
        self.assertIsNotNone(crop)
        # Synthetic tall original with distinct top and bottom bands.
        image = Image.new("RGB", (400, 1200), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 399, 100), fill="red")
        draw.rectangle((0, 1100, 399, 1199), fill="blue")
        buffer = io.BytesIO(); image.save(buffer, "PNG")
        crop.private_image.save("tall-original.png", SimpleUploadedFile("tall-original.png", buffer.getvalue()), save=True)
        self.submit(attempt)
        report = self.client.get(reverse("questions:practice_report", args=[attempt.attempt_uuid]))
        with fitz.open(stream=report.content, filetype="pdf") as document:
            rectangles = []
            for page in document:
                for info in page.get_images(full=True):
                    if info[2:4] == (400, 1200):
                        rectangles.extend((page.rect, rect) for rect in page.get_image_rects(info[0]))
            self.assertTrue(rectangles, "The original question image was omitted from the report")
            for page_rect, rect in rectangles:
                self.assertTrue(page_rect.contains(rect), f"Original image extends outside the page: {rect}")
                self.assertAlmostEqual(rect.width / rect.height, 1 / 3, delta=.02)

    def test_practice_saved_keys_survive_deleted_options_and_later_key_edits(self):
        paper = self.upload()
        first = paper.extracted_questions.filter(include_in_practice=True).first()
        exam = self.create_exam(paper, f"{first.question_number}: A")
        attempt = self.start(exam)
        response = attempt.responses.first()
        q = response.exam_question.question
        self.submit(attempt, response, q.options.get(option_key="A"))
        response.refresh_from_db()
        self.assertTrue(response.key_confirmed_at_grading)
        self.assertEqual((response.selected_option_key_at_grading, response.correct_option_key_at_grading), ("A", "A"))
        q.options.get(option_key="A").delete()
        q.options.filter(option_key="B").update(is_correct=True)
        result = self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        row = result.context["rows"][0]
        self.assertEqual(row["status"], "correct")
        self.assertIsNone(row["correct"])
        self.assertEqual((row["saved_choice"], row["saved_key"]), ("A", "A"))
        self.assertContains(result, "Choice when graded: <strong>A</strong>")
        self.assertContains(result, "Key when graded: <strong>A</strong>")
        report = self.client.get(reverse("questions:practice_report", args=[attempt.attempt_uuid]))
        with fitz.open(stream=report.content, filetype="pdf") as document:
            text = " ".join(page.get_text() for page in document)
        self.assertIn("Key when graded: A", text)

    def test_legacy_practice_result_does_not_guess_original_key_or_regrade(self):
        paper = self.upload()
        first = paper.extracted_questions.filter(include_in_practice=True).first()
        exam = self.create_exam(paper, f"{first.question_number}: A")
        attempt = self.start(exam)
        response = attempt.responses.first()
        q = response.exam_question.question
        self.submit(attempt, response, q.options.get(option_key="A"))
        attempt.refresh_from_db(); old_score = attempt.score_obtained
        attempt.responses.update(key_confirmed_at_grading=None, correct_option_key_at_grading=None,
                                 selected_option_key_at_grading=None)
        q.options.update(is_correct=False)
        q.options.filter(option_key="B").update(is_correct=True)
        q.explanation = "CURRENT EXPLANATION NOT HISTORICAL"
        q.save(update_fields=["explanation"])
        result = self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        row = result.context["rows"][0]
        self.assertEqual(row["status"], "correct")
        self.assertIsNone(row["saved_key"])
        self.assertIsNone(row["correct"])
        self.assertEqual(row["explanation"], "")
        self.assertIsNone(result.context["graded_marks"])
        self.assertContains(result, "did not record its key details")
        self.assertNotContains(result, "CURRENT EXPLANATION NOT HISTORICAL")
        attempt.refresh_from_db(); self.assertEqual(attempt.score_obtained, old_score)
        report = self.client.get(reverse("questions:practice_report", args=[attempt.attempt_uuid]))
        with fitz.open(stream=report.content, filetype="pdf") as document:
            text = " ".join(page.get_text() for page in document)
        self.assertIn("original graded maximum were not recorded", text)
        self.assertNotIn("CURRENT EXPLANATION NOT HISTORICAL", text)

    def test_private_bank_recrop_versions_images_and_requires_key_recheck(self):
        paper = self.upload("Image bank", image_first=True)
        staged = paper.extracted_questions.filter(include_in_practice=True).first()
        self.assertTrue(staged.figures.filter(source="question_crop").exists())
        staged.crop_warning = "This crop may contain a printed answer."
        staged.save(update_fields=["crop_warning"])
        exam = self.create_exam(paper, f"{staged.question_number}: A")
        attempt = self.start(exam)
        response = attempt.responses.first()
        original = response.exam_question.question
        original_image = original.figures.get(source="question_crop")
        original_name = original_image.private_image.name
        self.submit(attempt, response, original.options.get(option_key="A"))
        listing = self.client.get(reverse("questions:practice_bank"))
        self.assertContains(listing, "This crop may contain a printed answer")
        self.assertContains(listing, "Images / recrop")
        url = reverse("questions:practice_bank_images", args=[original.pk])
        self.assertContains(self.client.get(url), "Crop from the original PDF")
        invalid = self.client.post(url, {"action": "crop", "replace_id": original_image.pk,
                                        "page_number": "1", "x0": ".8", "y0": ".8", "x1": ".2", "y1": ".2"})
        self.assertEqual(invalid.status_code, 302)
        original.refresh_from_db(); self.assertFalse(original.is_bank_archived)
        saved = self.client.post(url, {"action": "crop", "replace_id": original_image.pk,
                                      "page_number": "1", "x0": ".05", "y0": ".05", "x1": ".9", "y1": ".5"})
        self.assertEqual(saved.status_code, 302)
        original.refresh_from_db(); self.assertTrue(original.is_bank_archived)
        self.assertEqual(original.figures.get(pk=original_image.pk).private_image.name, original_name)
        revised = Question.objects.get(creator=self.student, source_paper=paper, is_bank_archived=False,
                                       source_extracted_item__pk=staged.pk)
        new_crop = revised.figures.get(source="question_crop")
        self.assertNotEqual(new_crop.private_image.name, original_name)
        self.assertFalse(revised.key_verified)
        self.assertTrue(revised.options.get(option_key="A").is_correct)  # retained as a suggestion
        result = self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        self.assertEqual(result.context["counts"]["correct"], 1)
        self.assertContains(result, "Confirmed key")
        revised_url = reverse("questions:practice_bank_images", args=[revised.pk])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(revised_url).status_code, 404)
        self.assertEqual(self.client.get(reverse("questions:practice_bank_figure", args=[new_crop.pk])).status_code, 404)
        self.client.force_login(self.student)
        edit = self.client.post(reverse("questions:practice_bank_edit", args=[revised.pk]),
                                self.bank_edit_data(revised, correct_option="A", confirm_answer_key="on"))
        self.assertEqual(edit.status_code, 302)
        revised.refresh_from_db(); self.assertTrue(revised.key_verified)
        self.assertEqual(attempt.responses.get(pk=response.pk).selected_option.option_key, "A")

    def test_bank_copy_on_write_and_paper_delete_preserve_other_test(self):
        paper = self.upload()
        first = paper.extracted_questions.filter(include_in_practice=True).first()
        exam = self.create_exam(paper, f"{first.question_number}: A")
        attempt = self.start(exam)
        response = attempt.responses.first()
        original = response.exam_question.question
        original_text = original.question_text
        original_option = original.options.get(option_key="A")
        self.submit(attempt, response, original_option)
        edit = self.client.post(reverse("questions:practice_bank_edit", args=[original.pk]),
                                self.bank_edit_data(original, question_text="Edited future question", option_B="New B", confirm_answer_key="on"))
        self.assertEqual(edit.status_code, 302)
        original.refresh_from_db()
        self.assertTrue(original.is_bank_archived)
        self.assertEqual(original.question_text, original_text)
        new = Question.objects.get(question_text="Edited future question", creator=self.student, is_bank_archived=False)
        self.assertTrue(new.key_verified)
        self.assertTrue(new.options.get(option_key="B").is_correct)
        self.assertTrue(original.options.get(option_key="A").is_correct)
        self.assertEqual(self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid])).context["counts"]["correct"], 1)
        make_test = self.client.post(reverse("questions:practice_bank_test"), {
            "title": "New bank test", "duration_minutes": "25", "passing_percentage": "50", "question_ids": [new.pk]})
        self.assertEqual(make_test.status_code, 302)
        bank_exam = Exam.objects.get(title="New bank test", creator=self.student)
        self.assertEqual(bank_exam.questions.get().pk, new.pk)
        listing = self.client.get(reverse("questions:practice_list"))
        title = listing.content.decode().split("<title>", 1)[1].split("</title>", 1)[0]
        self.assertNotIn("<div", title)
        self.assertContains(listing, "New bank test", count=1)
        image_name = new.private_image.path if new.private_image else None
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("questions:practice_delete", args=[paper.pk]))
        self.assertFalse(Exam.objects.filter(pk=exam.pk).exists())
        self.assertTrue(Exam.objects.filter(pk=bank_exam.pk).exists())
        self.assertTrue(Question.objects.filter(pk=new.pk).exists())
        if image_name:
            self.assertTrue(os.path.isfile(image_name))
        new_attempt = self.start(bank_exam)
        self.assertEqual(new_attempt.responses.first().exam_question.question.question_text, "Edited future question")

    def test_bank_filter_delete_and_student_ownership(self):
        paper = self.upload()
        exam = self.create_exam(paper)
        question = exam.questions.first()
        self.client.post(reverse("questions:practice_bank_edit", args=[question.pk]), self.bank_edit_data(question))
        edited = Question.objects.get(subject="Physics", topic="Mechanics", chapter="Motion", is_bank_archived=False)
        bank = self.client.get(reverse("questions:practice_bank"), {"subject": "Physics", "topic": "Mechanics", "difficulty": "hard"})
        self.assertContains(bank, edited.question_text[:20])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("questions:practice_bank_edit", args=[edited.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("questions:practice_bank_delete", args=[edited.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("questions:practice_bank_test"), {"title":"stolen", "duration_minutes":30, "question_ids":[edited.pk]}).status_code, 404)
        self.client.force_login(self.student)
        self.client.post(reverse("questions:practice_bank_delete", args=[edited.pk]))
        self.assertFalse(Question.objects.filter(pk=edited.pk, is_bank_archived=False).exists())
        self.assertTrue(exam.questions.filter(pk=question.pk).exists())

    def test_active_attempt_blocks_results_source_bank_and_report(self):
        paper = self.upload()
        first = paper.extracted_questions.filter(include_in_practice=True).first()
        exam = self.create_exam(paper, f"{first.question_number}: A")
        old = self.start(exam)
        self.submit(old)
        active = self.start(exam)
        q = exam.questions.first()
        urls = [reverse("questions:practice_result", args=[old.attempt_uuid]),
                reverse("questions:practice_report", args=[old.attempt_uuid]),
                reverse("questions:practice_pdf", args=[paper.pk]),
                reverse("questions:practice_source_page", args=[first.pk, 1]),
                reverse("questions:practice_bank"), reverse("questions:practice_bank_edit", args=[q.pk])]
        for url in urls:
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertRedirects(r, reverse("exams:exam_session", args=[active.attempt_uuid]), fetch_redirect_response=False)
                self.assertIn("no-store", r["Cache-Control"])
        self.submit(active)
        self.assertEqual(self.client.get(urls[0]).status_code, 200)

    def test_plan_limits_and_feature_flags_are_enforced_on_server(self):
        self.free.monthly_pdf_uploads = 1
        self.free.monthly_test_creations = 1
        self.free.monthly_attempts = 1
        self.free.attempts_per_test = 1
        self.free.save()
        paper = self.upload()
        duplicate = self.client.post(reverse("questions:practice_upload"), {
            "title": "Denied", "pdf_file": SimpleUploadedFile("two.pdf", make_two_column_pdf(), content_type="application/pdf")})
        self.assertRedirects(duplicate, reverse("payments:plans"), fetch_redirect_response=False)
        self.assertFalse(QuestionPaperUpload.objects.filter(title="Denied").exists())
        exam = self.create_exam(paper)
        attempt = self.start(exam)
        self.submit(attempt)
        denied = self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        self.assertEqual(denied.status_code, 302)
        self.assertEqual(exam.attempts.count(), 1)
        usage = PracticeUsage.objects.get(user=self.student)
        self.assertEqual((usage.pdf_uploads, usage.test_creations, usage.attempts), (1, 1, 1))
        self.free.allow_bank = False; self.free.allow_pdf_reports = False; self.free.save()
        self.assertRedirects(self.client.get(reverse("questions:practice_bank")), reverse("payments:plans"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("questions:practice_report", args=[attempt.attempt_uuid])), reverse("payments:plans"), fetch_redirect_response=False)

    def test_final_answer_rejects_other_questions_options(self):
        paper = self.upload(); exam = self.create_exam(paper); attempt = self.start(exam)
        rows = list(attempt.responses.all())
        wrong_option = rows[1].exam_question.question.options.first()
        result = self.submit(attempt, rows[0], wrong_option)
        self.assertEqual(result.status_code, 400)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "in_progress")
        self.assertIsNone(rows[0].selected_option_id)
