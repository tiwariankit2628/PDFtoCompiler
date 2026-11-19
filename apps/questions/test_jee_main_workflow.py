"""JEE Main-style PDF extraction, numerical entry and verified scoring."""
import json
import os
import tempfile
import uuid
from decimal import Decimal
from unittest.mock import patch

import pymupdf as fitz
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category, ClassroomSession, Exam, ExamAttempt
from apps.questions.models import QuestionPaperUpload


def jee_style_pdf():
    """Synthetic paper: MCQs, numerical section across pages, then MCQs again."""
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 50), "SECTION A: SINGLE-CORRECT QUESTIONS")
        page.insert_text((40, 90), "Q1 Which value equals four?")
        for index, text in enumerate(("Four", "Five", "Six", "Seven")):
            page.insert_text((60, 110 + index * 18), f"({index + 1}) {text}")
        page.insert_text((40, 245), "SECTION B: NUMERICAL VALUE QUESTIONS")
        page.insert_text((40, 265), "The answer to each question is a numerical value.")
        page.insert_text((40, 310), "Q2 Calculate the result of 2 plus 3.")
        page.insert_text((40, 400), "Q3 A displacement is negative two point five units.")
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 65), "Q4 Calculate the result when a quantity cancels itself.")
        page.insert_text((40, 160), "SECTION C: SINGLE-CORRECT QUESTIONS")
        page.insert_text((40, 195), "Q5 Which item is printed second?")
        for index, text in enumerate(("First", "Second", "Third", "Fourth")):
            page.insert_text((60, 215 + index * 18), f"({index + 1}) {text}")
        return doc.write()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"], CELERY_TASK_ALWAYS_EAGER=True)
class JEEMainPDFWorkflowTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=os.path.join(self.root.name, "media"),
                                           PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private")))
        self.student = User.objects.create_user("jee_pdf_student", email="jee-pdf-s@example.test", role="STUDENT", exam_track="jee")
        self.neet = User.objects.create_user("neet_pdf_student", email="neet-pdf-s@example.test", role="STUDENT", exam_track="neet")
        self.teacher = User.objects.create_user("jee_pdf_teacher", email="jee-pdf-t@example.test", role="TEACHER")
        self.pdf = jee_style_pdf()

    def upload(self, mode):
        owner = self.student if mode == "student" else self.teacher
        view = "questions:practice_upload" if mode == "student" else "questions:classroom_paper_upload"
        self.client.force_login(owner)
        with patch("requests.sessions.Session.request", side_effect=AssertionError("PDF must remain local")):
            response = self.client.post(reverse(view), {
                "title": f"JEE Main {mode} PDF", "subject": "Physics", "target_type": "jee_main", "image_first": "on",
                "pdf_file": SimpleUploadedFile("jee-main.pdf", self.pdf, content_type="application/pdf"),
            })
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        paper = QuestionPaperUpload.objects.get(uploaded_by=owner, title=f"JEE Main {mode} PDF")
        extracted = list(paper.extracted_questions.order_by("question_number"))
        self.assertEqual([q.question_number for q in extracted], [1, 2, 3, 4, 5])
        self.assertEqual([q.detected_type for q in extracted],
                         ["mcq_single", "numerical", "numerical", "numerical", "mcq_single"])
        self.assertEqual([len(q.options) for q in extracted], [4, 0, 0, 0, 4])
        self.assertEqual(extracted[0].options[-1]["text"], "Seven")
        self.assertEqual(extracted[3].question_text, "Calculate the result when a quantity cancels itself.")
        self.assertIn("section heading", " ".join(extracted[3].extraction_issues))
        crop = extracted[3].figures.get(source="question_crop")
        self.assertTrue(crop.private_image and crop.private_image.storage.exists(crop.private_image.name))
        # Source regions remain unchanged even though the next heading is no
        # longer accidentally included in the preceding extracted question.
        with fitz.open(stream=self.pdf, filetype="pdf") as original:
            x0, y0, x1, y1 = extracted[3].source_regions[0]["box"]
            page = original[1]
            box = fitz.Rect(x0 * page.rect.width, y0 * page.rect.height,
                            x1 * page.rect.width, y1 * page.rect.height)
            self.assertIn("SECTION C", page.get_text(clip=box))
        self.assertTrue(all(q.is_flagged for q in extracted[1:4]))  # reviewer must inspect the original
        self.assertTrue(all(not q.correct_option for q in extracted))  # extraction never invents keys
        return paper

    def take_and_score(self, exam, start_url):
        self.client.force_login(self.student)
        self.assertEqual(self.client.post(start_url).status_code, 302)
        attempt = exam.attempts.get(student=self.student)
        expiry = attempt.expires_at
        session_url = reverse("exams:exam_session", args=[attempt.attempt_uuid])
        page = self.client.get(session_url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, '"numeric_key"')
        questions = page.context["questions_data"]
        self.assertEqual([q["question_type"] for q in questions],
                         ["mcq_single", "numerical", "numerical", "numerical", "mcq_single"])
        url = reverse("exams:exam_save_answer", args=[attempt.attempt_uuid])

        def save(item, **values):
            return self.client.post(url, json.dumps({"response_id": item["response_id"], "action": "navigate", **values}),
                                    content_type="application/json")

        option_a = next(option["id"] for option in questions[0]["options"] if option["key"] == "A")
        self.assertEqual(save(questions[0], selected_option_id=option_a).status_code, 200)
        self.assertEqual(save(questions[1], numerical_response="5").status_code, 200)
        self.assertEqual(save(questions[2], numerical_response="-2.6").status_code, 200)  # wrong numerical answer
        self.assertEqual(save(questions[3], numerical_response="0").status_code, 200)  # zero is an answer
        self.assertEqual(save(questions[1], numerical_response="NaN").status_code, 400)
        refreshed = self.client.get(session_url).context["questions_data"]
        self.assertEqual(Decimal(refreshed[1]["numerical_response"]), Decimal("5"))
        self.assertEqual(Decimal(refreshed[3]["numerical_response"]), Decimal("0"))
        attempt.refresh_from_db(); self.assertEqual(attempt.expires_at, expiry)
        self.assertEqual(self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]),
                                         "{}", content_type="application/json").status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual((attempt.correct_count, attempt.incorrect_count, attempt.unattempted_count), (3, 1, 1))
        self.assertEqual(attempt.score_obtained, Decimal("11"))
        numerical = list(attempt.responses.order_by("order"))[1:4]
        self.assertEqual([r.typed_grading_snapshot["outcome"] for r in numerical], ["correct", "incorrect", "correct"])
        return attempt

    def test_student_pdf_to_numerical_test_requires_checked_key_and_scores(self):
        paper = self.upload("student")
        settings_url = reverse("questions:practice_settings", args=[paper.pk])
        values = {"duration_minutes": 20, "answer_key_text": "1:A, 2:5, 3:-2.5, 4:0, 5:B",
                  "acknowledge_review": "on", "acknowledge_unassigned": "on"}
        response = self.client.post(settings_url, values)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(paper.practice_exam_id)
        values["confirm_answer_key"] = "on"
        response = self.client.post(settings_url, values)
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        paper.refresh_from_db(); exam = paper.practice_exam
        self.assertEqual(exam.target_type, "jee_main")
        self.assertEqual(set(exam.questions.values_list("target_type", flat=True)), {"jee_main"})
        attempt = self.take_and_score(exam, reverse("exams:exam_start", args=[exam.slug]))
        self.assertContains(self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid])), "-2.6")

    def test_teacher_public_pdf_review_to_numerical_exam(self):
        category = Category.objects.create(name="JEE public fixture", slug="jee-public-fixture")
        self.teacher.assigned_categories.add(category)
        self.client.force_login(self.teacher)
        with patch("requests.sessions.Session.request", side_effect=AssertionError("PDF must remain local")):
            uploaded = self.client.post(reverse("questions:paper_upload"), {
                "title": "Institutional JEE PDF", "category": category.pk, "subject": "Physics",
                "target_type": "jee_main", "pdf_file": SimpleUploadedFile("jee.pdf", self.pdf, content_type="application/pdf"),
            })
        self.assertEqual(uploaded.status_code, 302, uploaded.context and uploaded.context["form"].errors)
        paper = QuestionPaperUpload.objects.get(title="Institutional JEE PDF")
        numerical = paper.extracted_questions.get(question_number=2)
        self.assertEqual((paper.target_type, numerical.detected_type, numerical.correct_option), ("jee_main", "numerical", ""))
        key_url = reverse("questions:paper_import_answer_key", args=[paper.pk])
        unconfirmed = self.client.post(key_url, {"answer_key_text": "2:5"})
        self.assertEqual(unconfirmed.status_code, 200)
        numerical.refresh_from_db(); self.assertEqual(numerical.correct_option, "")
        self.assertEqual(self.client.post(key_url, {"answer_key_text": "2:5", "confirm_checked": "on"}).status_code, 302)
        # Simulate a teacher checking the original and explicitly clearing the
        # extraction warning; approval never fabricates an answer.
        self.assertTrue(numerical.is_flagged)
        self.client.post(reverse("questions:extracted_toggle_flag", args=[numerical.pk]))
        self.client.post(reverse("questions:extracted_approve", args=[numerical.pk]))
        numerical.refresh_from_db(); bank = numerical.approved_question
        self.assertIsNotNone(bank)
        self.assertEqual((bank.target_type, bank.question_type, bank.numeric_key, bank.typed_key_verified),
                         ("jee_main", "numerical", Decimal("5"), True))
        created = self.client.post(reverse("exams:exam_create"), {
            "title": "Institutional JEE numerical", "category": category.pk, "target_type": "jee_main",
            "duration_minutes": 30, "total_marks": 4, "passing_percentage": 0,
            "default_positive_marks": 4, "default_negative_marks": 1, "max_attempts": 1, "status": "draft", "is_free": "on",
        })
        self.assertEqual(created.status_code, 302, created.context and created.context["form"].errors)
        exam = Exam.objects.get(title="Institutional JEE numerical")
        self.client.post(reverse("exams:exam_add_questions_bulk", args=[exam.pk]), {"selected_questions": [bank.pk]})
        self.client.post(reverse("exams:exam_toggle_publish", args=[exam.pk]))
        exam.refresh_from_db(); self.assertEqual(exam.status, "published")
        self.client.force_login(self.neet)
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[exam.slug])).status_code, 404)
        self.client.force_login(self.student)
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[exam.slug])).status_code, 302)
        attempt = exam.attempts.get(student=self.student)
        item = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).context["questions_data"][0]
        self.assertEqual(item["question_type"], "numerical")
        saved = self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
            json.dumps({"response_id": item["response_id"], "numerical_response": "5", "action": "navigate"}),
            content_type="application/json")
        self.assertEqual(saved.status_code, 200)
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), "{}", content_type="application/json")
        attempt.refresh_from_db(); self.assertEqual(attempt.score_obtained, Decimal("4"))

    def test_teacher_classroom_pdf_to_numerical_test_and_track_scoping(self):
        paper = self.upload("teacher")
        response = self.client.post(reverse("questions:classroom_paper_review", args=[paper.pk]), {
            "creation_token": str(uuid.uuid4()), "duration_minutes": 25,
            "answer_key_text": "1:A, 2:5, 3:-2.5, 4:0, 5:B", "confirm_review": "on",
            "acknowledge_review": "on", "acknowledge_unassigned": "on", "show_results": "on",
        })
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        session = ClassroomSession.objects.get(teacher=self.teacher)
        self.assertEqual(session.exam.target_type, "jee_main")
        self.assertEqual(set(session.exam.questions.values_list("target_type", flat=True)), {"jee_main"})
        self.client.force_login(self.neet)
        self.client.post(reverse("exams:classroom_join"), {"code": session.code})
        self.assertFalse(session.participants.filter(student=self.neet).exists())
        self.client.force_login(self.student)
        self.assertEqual(self.client.post(reverse("exams:classroom_join"), {"code": session.code}).status_code, 302)
        attempt = self.take_and_score(session.exam, reverse("exams:classroom_take", args=[session.pk]))
        self.assertEqual(attempt.classroom_session_id, session.pk)
