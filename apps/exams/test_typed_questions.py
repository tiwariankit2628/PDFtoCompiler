"""End-to-end typed-answer, grading, legacy and privacy regressions."""
import json
import os
import tempfile
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.classroom_results import classroom_result_summary
from apps.exams.models import Category, ClassroomSession, Exam, ExamAttempt, ExamQuestion
from apps.questions.answer_key_validation import parse_checked_answer_key
from apps.questions.models import AnswerStatus, ExtractedQuestion, Question, QuestionOption, QuestionPaperUpload
from apps.questions.practice_analytics import compute_practice_result


OPTIONS = [{"key": key, "text": f"Choice {key}"} for key in "ABCD"]


class TypedKeyParsingTests(SimpleTestCase):
    def q(self, n, kind):
        return SimpleNamespace(question_number=n, detected_type=kind, options=OPTIONS,
            status="extracted", approved_question_id=None)

    def test_numeric_labels_and_actual_numbers_do_not_get_confused(self):
        qs = [self.q(1, "mcq_single"), self.q(2, "mcq_single"), self.q(3, "mcq_single"),
              self.q(4, "mcq_multiple"), self.q(5, "numerical"), self.q(6, "numerical")]
        self.assertEqual(parse_checked_answer_key("1:1, 2:3, 3:4, 4:[1,3], 5:-2.500, 6:(3)", qs),
                         {1: "A", 2: "C", 3: "D", 4: "[A,C]", 5: "-2.5", 6: "3"})
        self.assertEqual(parse_checked_answer_key("5:0", qs), {5: "0"})

    def test_malformed_or_ambiguous_keys_fail_as_a_whole(self):
        qs = [self.q(1, "mcq_single"), self.q(2, "mcq_multiple"), self.q(3, "numerical")]
        for text in ("1:A, 1:B", "1:6", "2:A", "2:[A,A]", "2:[A,1]", "2:[A,E]",
                     "3:1.1234567", "3:NaN", "3:1e3", "3:1234567890123", "1:A, junk"):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_checked_answer_key(text, qs)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class TypedQuestionWorkflowTests(TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=os.path.join(root.name, "media"),
            STATIC_ROOT=os.path.join(root.name, "static"), PRIVATE_PRACTICE_ROOT=os.path.join(root.name, "private")))
        self.teacher = User.objects.create_user("typed_teacher", email="typed-t@example.test", password="test", role=User.Roles.TEACHER)
        self.student = User.objects.create_user("typed_student", email="typed-s@example.test", password="test", role=User.Roles.STUDENT, exam_track="jee")
        self.other = User.objects.create_user("typed_other", email="typed-o@example.test", password="test", role=User.Roles.STUDENT, exam_track="jee")
        self.category = Category.objects.create(name="Typed workflow", slug="typed-workflow")

    def paper(self, mode):
        owner = self.student if mode == "student" else self.teacher
        paper = QuestionPaperUpload.objects.create(title=f"Typed {mode}", category=self.category,
            uploaded_by=owner, subject="Physics", status=QuestionPaperUpload.ProcessingStatus.EXTRACTED,
            is_student_private=mode == "student", is_classroom_private=mode == "classroom", target_type="jee")
        for number, kind, opts in ((1, "mcq_multiple", OPTIONS), (2, "numerical", []),
                                   (3, "mcq_single", OPTIONS), (4, "mcq_multiple", OPTIONS)):
            ExtractedQuestion.objects.create(paper=paper, question_number=number, question_text=f"Question {number}",
                detected_type=kind, options=opts, is_flagged=False, include_in_practice=True,
                positive_marks=4, negative_marks=1, multi_scoring_mode="per_correct" if number == 1 else "exact",
                partial_credit_per_choice=1 if number == 1 else 0, numeric_tolerance=Decimal("0.01") if number == 2 else 0)
        return paper

    def start_student(self, paper):
        self.client.force_login(self.student)
        url = reverse("questions:practice_settings", args=[paper.pk])
        response = self.client.post(url, {"duration_minutes": 25, "answer_key_text": "1:[1,3], 2:-2.5, 3:2",
                                          "confirm_answer_key": "on"})
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        paper.refresh_from_db()
        exam = paper.practice_exam
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[exam.slug])).status_code, 302)
        return exam, exam.attempts.get(student=self.student)

    def save(self, attempt, item, **values):
        return self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
            json.dumps({"response_id": item["response_id"], "action": "navigate", **values}), content_type="application/json")

    def test_student_creation_resume_submission_snapshots_and_bank_copy_on_write(self):
        paper = self.paper("student")
        exam, attempt = self.start_student(paper)
        self.assertEqual(exam.duration_minutes, 25)
        questions = list(exam.questions.order_by("exam_instances__order"))
        self.assertEqual([q.question_type for q in questions], ["mcq_multiple", "numerical", "mcq_single", "mcq_multiple"])
        self.assertTrue(questions[0].typed_key_verified)
        self.assertEqual(list(questions[0].options.filter(is_correct=True).values_list("option_key", flat=True)), ["A", "C"])
        self.assertEqual(questions[1].numeric_key, Decimal("-2.5"))
        self.assertFalse(questions[3].typed_key_verified)
        original_expiry = attempt.expires_at
        page = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertNotContains(page, '"is_correct"')
        self.assertNotContains(page, '"numeric_key"')
        a, n, single, unknown = page.context["questions_data"]
        answer_a = next(o["id"] for o in a["options"] if o["key"] == "A")
        answer_b = next(o["id"] for o in single["options"] if o["key"] == "B")
        self.assertEqual(self.save(attempt, a, selected_option_ids=[answer_a]).status_code, 200)
        self.assertEqual(self.save(attempt, n, numerical_response="-2.49").status_code, 200)
        self.assertEqual(self.save(attempt, single, selected_option_id=answer_b).status_code, 200)
        self.assertEqual(self.save(attempt, unknown, selected_option_ids=[unknown["options"][0]["id"]]).status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.answered_count, 4)
        resumed = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(resumed.context["questions_data"][0]["selected_option_ids"], [answer_a])
        self.assertEqual(Decimal(resumed.context["questions_data"][1]["numerical_response"]), Decimal("-2.49"))
        attempt.refresh_from_db(); self.assertEqual(attempt.expires_at, original_expiry)
        final = {"is_timeout": False, "last_response": {"response_id": n["response_id"],
                 "selected_option_id": None, "numerical_response": "-2.5", "time_spent_delta": 3}}
        self.assertEqual(self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]),
            json.dumps(final), content_type="application/json").status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.score_obtained, Decimal("9"))
        self.assertEqual((attempt.correct_count, attempt.partial_count, attempt.incorrect_count, attempt.unattempted_count), (2, 1, 0, 0))
        self.assertTrue(attempt.has_ungraded_keys)
        rows = list(attempt.responses.order_by("order"))
        self.assertEqual([r.typed_grading_snapshot["outcome"] for r in (rows[0], rows[1], rows[3])],
                         ["partial", "correct", "ungraded"])
        self.assertEqual(rows[0].typed_grading_snapshot["key"], ["A", "C"])
        self.assertEqual(rows[1].typed_grading_snapshot["answer"], "-2.5")
        result = compute_practice_result(attempt)
        self.assertEqual(result["counts"], {"correct": 2, "partial": 1, "incorrect": 0, "unattempted": 0, "ungraded": 1})
        review = self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        self.assertContains(review, "Partial credit")
        self.assertContains(review, "-2.5")
        # A later owner bank edit creates a new version, leaving old attempts intact.
        bank = questions[0]
        options = {opt.option_key: opt.option_text for opt in bank.options.all()}
        edit = {"target_type": "jee", "question_type": "mcq_multiple", "question_text": bank.question_text, "subject": bank.subject,
                "topic": "", "chapter": "", "difficulty": bank.difficulty, "language": bank.language,
                "positive_marks": "4", "negative_marks": "1", "multi_scoring_mode": "exact",
                "partial_credit_per_choice": "0", "numeric_tolerance": "0", "typed_key": "[B,D]",
                "confirm_answer_key": "on", **{f"option_{k}": options.get(k, "") for k in "ABCDE"}}
        response = self.client.post(reverse("questions:practice_bank_edit", args=[bank.pk]), edit)
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        bank.refresh_from_db(); rows[0].refresh_from_db(); attempt.refresh_from_db()
        self.assertTrue(bank.is_bank_archived)
        self.assertEqual(rows[0].typed_grading_snapshot["key"], ["A", "C"])
        self.assertEqual(attempt.score_obtained, Decimal("9"))

    def test_partial_only_practice_history_shows_recorded_accuracy(self):
        _, attempt = self.start_student(self.paper("student"))
        item = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).context["questions_data"][0]
        choice = next(option["id"] for option in item["options"] if option["key"] == "A")
        self.assertEqual(self.save(attempt, item, selected_option_ids=[choice]).status_code, 200)
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), "{}", content_type="application/json")
        attempt.refresh_from_db()
        self.assertEqual((attempt.correct_count, attempt.partial_count, attempt.incorrect_count), (0, 1, 0))
        self.assertEqual(attempt.accuracy_percentage, Decimal("0.00"))
        review = self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        self.assertContains(review, "<td>0.00%</td>", html=True)

    def test_typed_payload_validation_and_owner_access(self):
        exam, attempt = self.start_student(self.paper("student"))
        items = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).context["questions_data"]
        multi, numerical, other = items[0], items[1], items[2]
        wrong_id = other["options"][0]["id"]
        for values in ({"selected_option_ids": [wrong_id]}, {"selected_option_ids": [True]},
                       {"selected_option_ids": [multi["options"][0]["id"]] * 2},
                       {"selected_option_ids": "A"}, {"selected_option_id": wrong_id, "selected_option_ids": []}):
            self.assertEqual(self.save(attempt, multi, **values).status_code, 400, values)
        for raw in ("NaN", "1e3", "1.1234567", "1234567890123", True, {}):
            self.assertEqual(self.save(attempt, numerical, numerical_response=raw).status_code, 400, raw)
        self.assertEqual(attempt.responses.get(pk=multi["response_id"]).selected_option_ids, [])
        self.assertIsNone(attempt.responses.get(pk=numerical["response_id"]).numerical_response)
        self.client.force_login(self.other)
        self.assertEqual(self.save(attempt, numerical, numerical_response="0").status_code, 403)
        self.client.force_login(self.student)
        self.assertEqual(self.save(attempt, numerical, numerical_response="0").status_code, 200)
        self.assertEqual(attempt.responses.get(pk=numerical["response_id"]).palette_status, "answered")
        self.assertEqual(attempt.responses.get(pk=numerical["response_id"]).numerical_response, Decimal("0"))

    def test_unreleased_classroom_own_review_preserves_typed_and_image_only_answers(self):
        from django.core.files.base import ContentFile
        from apps.questions.models import ExtractedFigure
        from apps.questions.test_helpers import response_body
        from .test_series_workflow import printed_answer_png

        paper = self.paper("classroom")
        paper.image_first = True
        paper.save(update_fields=["image_first"])
        multi = paper.extracted_questions.get(question_number=1)
        multi.is_image_first = True
        multi.question_text = ""  # actual question and options appear only in the original crop
        multi.options = [{"key": key, "text": "", "visible_in_crop": True} for key in "ABCD"]
        multi.explanation = "SECRET MULTIPLE EXPLANATION"
        multi.save()
        crop = ExtractedFigure(paper=paper, question=multi, source="question_crop", page_number=1)
        crop.private_image.save("printed-answer.png", ContentFile(printed_answer_png()), save=True)
        numerical = paper.extracted_questions.get(question_number=2)
        numerical.explanation = "SECRET NUMERICAL EXPLANATION"
        numerical.save(update_fields=["explanation"])
        self.client.force_login(self.teacher)
        created = self.client.post(reverse("questions:classroom_paper_review", args=[paper.pk]), {
            "creation_token": str(uuid.uuid4()), "duration_minutes": 37,
            "answer_key_text": "1:[A,C], 2:123.75, 3:B", "confirm_review": "on"})
        self.assertEqual(created.status_code, 302, created.context and created.context["form"].errors)
        session = ClassroomSession.objects.get(teacher=self.teacher, exam__is_classroom_only=True)
        self.assertFalse(session.results_released)
        self.client.force_login(self.student)
        self.client.post(reverse("exams:classroom_join"), {"code": session.code})
        self.client.post(reverse("exams:classroom_take", args=[session.pk]))
        attempt = session.attempts.get(student=self.student)
        expiry = attempt.expires_at
        screen = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertNotContains(screen, "SECRET MULTIPLE EXPLANATION")
        self.assertNotContains(screen, "123.75")
        items = screen.context["questions_data"]
        self.assertTrue(items[0]["image_first"])
        self.assertEqual(items[0]["question_text"], "")
        image_url = items[0]["crop_urls"][0]
        choice_a = next(o["id"] for o in items[0]["options"] if o["key"] == "A")
        choice_b = next(o["id"] for o in items[3]["options"] if o["key"] == "B")
        self.assertEqual(self.save(attempt, items[0], selected_option_ids=[choice_a]).status_code, 200)
        self.assertEqual(self.save(attempt, items[1], numerical_response="5").status_code, 200)
        self.assertEqual(self.save(attempt, items[3], selected_option_ids=[choice_b]).status_code, 200)
        self.assertEqual(self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, expiry)
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), "{}", content_type="application/json")
        attempt.refresh_from_db()
        self.assertEqual(attempt.score_obtained, Decimal("0"))
        self.assertEqual(classroom_result_summary(attempt)["counts"],
                         {"correct": 0, "partial": 1, "incorrect": 1, "unattempted": 1, "ungraded": 1})
        result = self.client.get(reverse("exams:student_result_detail", args=[attempt.attempt_uuid]))
        self.assertContains(result, "Answered, ungraded")
        own = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        self.assertContains(own, "Your answer review")
        self.assertContains(own, "Partial credit")
        self.assertContains(own, "Not graded")
        self.assertContains(own, "printed answers")
        self.assertNotContains(own, "123.75")
        self.assertNotContains(own, "SECRET MULTIPLE EXPLANATION")
        self.assertNotContains(own, "SECRET NUMERICAL EXPLANATION")
        image = self.client.get(image_url)
        self.assertEqual(image.status_code, 200)
        self.assertIn("no-store", image["Cache-Control"])
        self.assertIn(b"PRINTED ANSWER: A", response_body(image))
        self.client.force_login(self.other)
        self.assertIn(self.client.get(image_url).status_code, (403, 404))
        self.assertIn(self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid])).status_code, (403, 404))
        self.client.force_login(self.teacher)
        self.client.post(reverse("exams:classroom_control", args=[session.pk]), {"action": "release_results", "confirm": "yes"})
        self.client.force_login(self.student)
        released = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        self.assertContains(released, "123.75")
        self.assertContains(released, "SECRET MULTIPLE EXPLANATION")
        self.assertContains(released, "SECRET NUMERICAL EXPLANATION")
        attempt.refresh_from_db()
        self.assertEqual(attempt.score_obtained, Decimal("0"))

    def test_teacher_classroom_snapshots_types_join_and_keep_later_edits_separate(self):
        paper = self.paper("classroom")
        self.client.force_login(self.teacher)
        response = self.client.post(reverse("questions:classroom_paper_review", args=[paper.pk]), {
            "creation_token": str(uuid.uuid4()), "duration_minutes": 37, "answer_key_text": "1:[A,C], 2:0, 3:1",
            "confirm_review": "on", "show_results": "on"})
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        session = ClassroomSession.objects.get(teacher=self.teacher, exam__is_classroom_only=True)
        questions = list(session.exam.questions.order_by("exam_instances__order"))
        self.assertEqual([q.question_type for q in questions], ["mcq_multiple", "numerical", "mcq_single", "mcq_multiple"])
        self.assertEqual(questions[1].numeric_key, Decimal("0"))
        self.client.force_login(self.student)
        self.client.post(reverse("exams:classroom_join"), {"code": session.code})
        self.client.post(reverse("exams:classroom_take", args=[session.pk]))
        attempt = session.attempts.get(student=self.student)
        expiry = attempt.expires_at
        items = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).context["questions_data"]
        ids = [o["id"] for o in items[0]["options"] if o["key"] in ("A", "C")]
        self.assertEqual(self.save(attempt, items[0], selected_option_ids=ids).status_code, 200)
        self.assertEqual(self.save(attempt, items[1], numerical_response="0.005").status_code, 200)
        attempt.refresh_from_db(); self.assertEqual(attempt.expires_at, expiry)
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), "{}", content_type="application/json")
        attempt.refresh_from_db()
        self.assertEqual(attempt.score_obtained, Decimal("8"))
        self.assertEqual(classroom_result_summary(attempt)["counts"]["correct"], 2)
        self.assertContains(self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid])), "0.005")
        self.client.force_login(self.other)
        self.assertIn(self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid])).status_code, (403, 404))
        questions[0].options.update(is_correct=False)
        questions[1].numeric_key = 12; questions[1].save(update_fields=["numeric_key"])
        self.client.force_login(self.student)
        review = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        self.assertEqual(review.context["reviewed_questions"][0]["saved_key"], "A, C")
        self.assertEqual(review.context["reviewed_questions"][1]["saved_key"], "0")
        self.assertEqual(attempt.score_obtained, Decimal("8"))

    def test_legacy_numerical_choice_is_preserved_until_explicitly_reanswered(self):
        _, attempt = self.start_student(self.paper("student"))
        numerical = attempt.responses.select_related("exam_question__question").order_by("order")[1]
        legacy_option = QuestionOption.objects.create(
            question=numerical.exam_question.question, option_key="A", option_text="Old choice", is_correct=True)
        numerical.selected_option = legacy_option
        numerical.save(update_fields=["selected_option"])
        session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        item = session.context["questions_data"][1]
        self.assertTrue(item["legacy_selection"])
        self.assertIsNone(item["selected_option_id"])
        # Merely navigating/reviewing does not erase the old answer.
        self.assertEqual(self.save(attempt, item, selected_option_id=None, selected_option_ids=[], numerical_response="").status_code, 200)
        numerical.refresh_from_db()
        self.assertEqual(numerical.selected_option_id, legacy_option.pk)
        self.assertIsNone(numerical.numerical_response)
        # A new valid numerical response explicitly replaces it, including zero.
        self.assertEqual(self.save(attempt, item, selected_option_id=None, selected_option_ids=[], numerical_response="-2.5").status_code, 200)
        numerical.refresh_from_db()
        self.assertIsNone(numerical.selected_option_id)
        self.assertEqual(numerical.numerical_response, Decimal("-2.5"))
        reloaded = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).context["questions_data"][1]
        self.assertFalse(reloaded["legacy_selection"])
        self.assertEqual(Decimal(reloaded["numerical_response"]), Decimal("-2.5"))
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), "{}", content_type="application/json")
        numerical.refresh_from_db()
        self.assertEqual(numerical.typed_grading_snapshot["outcome"], "correct")

    def test_legacy_option_flags_never_confirm_a_typed_key(self):
        paper = self.paper("student")
        first = paper.extracted_questions.get(question_number=1)
        first.correct_option = "A"; first.answer_status = AnswerStatus.STUDENT_CONFIRMED
        first.typed_key_verified = False; first.save()
        self.client.force_login(self.student)
        response = self.client.post(reverse("questions:practice_settings", args=[paper.pk]), {"duration_minutes": 20})
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        paper.refresh_from_db(); bank = paper.practice_exam.questions.order_by("exam_instances__order").first()
        self.assertFalse(bank.typed_key_verified)
        self.assertFalse(bank.key_verified)
        self.assertFalse(bank.options.filter(is_correct=True).exists())
        first.refresh_from_db()
        self.assertEqual(first.correct_option, "A")
        self.assertEqual(first.answer_status, AnswerStatus.STUDENT_CONFIRMED)

    def test_teacher_bulk_import_and_approval_preserve_typed_keys_and_rules(self):
        paper = self.paper("teacher")
        url = reverse("questions:paper_import_answer_key", args=[paper.pk])
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.post(url, {"answer_key_text": "1:[1,3], 2:0, 3:4"}).status_code, 200)
        self.assertFalse(paper.extracted_questions.exclude(correct_option="").exists())
        self.assertEqual(self.client.post(url, {"answer_key_text": "1:[1,3], 2:0, 3:4", "confirm_checked": "on"}).status_code, 302)
        questions = list(paper.extracted_questions.order_by("question_number"))
        self.assertEqual([q.correct_option for q in questions], ["[A,C]", "0", "D", ""])
        self.assertEqual([q.typed_key_verified for q in questions], [True, True, False, False])
        page = self.client.get(reverse("questions:paper_review", args=[paper.pk]))
        for number in (1, 2, 3):
            self.assertContains(page, f'data-number="{number}"')
        self.assertContains(page, "Confirmed answer: [A,C]")
        self.assertContains(page, "Confirmed answer: 0")
        self.assertEqual(self.client.post(reverse("questions:paper_bulk_approve", args=[paper.pk])).status_code, 302)
        for q in questions:
            q.refresh_from_db()
            self.assertIsNotNone(q.approved_question_id)
        multi, numerical, single, unconfirmed = [q.approved_question for q in questions]
        self.assertEqual((multi.question_type, multi.multi_scoring_mode, multi.partial_credit_per_choice),
                         ("mcq_multiple", "per_correct", Decimal("1")))
        self.assertTrue(multi.typed_key_verified)
        self.assertEqual(set(multi.options.filter(is_correct=True).values_list("option_key", flat=True)), {"A", "C"})
        self.assertEqual((numerical.question_type, numerical.numeric_key, numerical.numeric_tolerance),
                         ("numerical", Decimal("0"), Decimal("0.01")))
        self.assertTrue(numerical.typed_key_verified)
        self.assertEqual(single.options.get(is_correct=True).option_key, "D")
        self.assertFalse(unconfirmed.typed_key_verified)
        self.assertContains(self.client.get(reverse("questions:question_preview", args=[numerical.pk])), "Numerical key:")
        self.assertContains(self.client.get(reverse("questions:question_bank")), "Numerical key:")
        # A staging import may not silently overwrite the approved bank object.
        attempt = self.client.post(url, {"answer_key_text": "1:[B,D]", "confirm_checked": "on", "overwrite_existing": "on"})
        self.assertEqual(attempt.status_code, 200)
        self.assertEqual(set(multi.options.filter(is_correct=True).values_list("option_key", flat=True)), {"A", "C"})

    def test_public_typed_authoring_active_edit_and_historical_snapshot(self):
        self.client.force_login(self.teacher)
        data = {"target_type": "jee", "category": self.category.pk, "subject": "Physics", "topic": "", "chapter": "",
                "difficulty": "medium", "language": "en", "question_text": "Choose the valid choices",
                "question_type": "mcq_multiple", "positive_marks": "4", "negative_marks": "1",
                "multi_scoring_mode": "per_correct", "partial_credit_per_choice": "1", "numeric_tolerance": "0",
                "typed_key": "[1,3]", "explanation": "Original checked explanation",
                **{f"option_text_{k}": f"Choice {k}" for k in "ABCD"}}
        create = self.client.post(reverse("questions:question_create"), data)
        self.assertEqual(create.status_code, 302, create.context and create.context["form"].errors)
        q = Question.objects.get(creator=self.teacher, question_text=data["question_text"])
        self.assertFalse(q.typed_key_verified)
        self.assertEqual(q.answer_status, AnswerStatus.NEEDS_REVIEW)
        page = self.client.get(reverse("questions:question_preview", args=[q.pk]))
        self.assertContains(page, "Unconfirmed suggestion — ungraded")
        # An unrelated edit cannot promote a proposed key; a checked edit can.
        self.assertEqual(self.client.post(reverse("questions:question_edit", args=[q.pk]), data).status_code, 302)
        q.refresh_from_db(); self.assertFalse(q.typed_key_verified)
        self.assertEqual(self.client.post(reverse("questions:question_edit", args=[q.pk]), dict(data, confirm_answer_key="on")).status_code, 302)
        q.refresh_from_db(); self.assertTrue(q.typed_key_verified)
        exam = Exam.objects.create(title="Public typed", slug="public-typed", category=self.category, creator=self.teacher,
            status=Exam.ExamStatus.PUBLISHED, is_free=True, duration_minutes=20, shuffle_questions=False, shuffle_options=False, target_type="jee")
        ExamQuestion.objects.create(exam=exam, question=q, order=1, positive_marks=4, negative_marks=1)
        exam.recalculate_marks()
        self.client.force_login(self.student)
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[exam.slug])).status_code, 302)
        attempt = exam.attempts.get(student=self.student)
        item = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).context["questions_data"][0]
        b_id = next(o["id"] for o in item["options"] if o["key"] == "B")
        self.assertEqual(self.save(attempt, item, selected_option_ids=[b_id]).status_code, 200)
        expiry = attempt.expires_at
        # Even if the editor removes a choice while a JSON response references
        # it, keep the option ID so a live attempt is not silently destroyed.
        self.client.force_login(self.teacher)
        changed = dict(data, option_text_B="", confirm_answer_key="on")
        edited = self.client.post(reverse("questions:question_edit", args=[q.pk]), changed, follow=True)
        self.assertEqual(edited.status_code, 200)
        self.assertTrue(q.options.filter(pk=b_id).exists())
        self.assertContains(edited, "Option B was retained")
        self.client.force_login(self.student)
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), "{}", content_type="application/json")
        attempt.refresh_from_db(); response = attempt.responses.get()
        self.assertEqual(attempt.expires_at, expiry)
        self.assertEqual(response.typed_grading_snapshot["outcome"], "incorrect")
        self.assertEqual(response.typed_grading_snapshot["answer"], ["B"])
        self.assertEqual(response.typed_grading_snapshot["key"], ["A", "C"])
        self.assertEqual(response.typed_grading_snapshot["explanation"], "Original checked explanation")
        q.explanation = "Later edited explanation"; q.save(update_fields=["explanation"])
        q.options.update(is_correct=False)
        review = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        self.assertContains(review, "Original checked explanation")
        self.assertNotContains(review, "Later edited explanation")
        self.assertEqual(review.context["reviewed_questions"][0]["saved_key"], "A, C")

    def test_legacy_public_typed_review_does_not_reinterpret_an_old_score(self):
        exam = Exam.objects.create(title="Historical typed", slug="historical-typed", category=self.category,
            creator=self.teacher, status="published", is_free=True, total_marks=4, target_type="jee")
        question = Question.objects.create(category=self.category, creator=self.teacher, subject="Math",
            question_type="mcq_multiple", question_text="Old typed question", explanation="Current explanation", target_type="jee")
        choice = QuestionOption.objects.create(question=question, option_key="A", option_text="Old answer", is_correct=True)
        QuestionOption.objects.create(question=question, option_key="B", option_text="Another choice")
        eq = ExamQuestion.objects.create(exam=exam, question=question, order=1, positive_marks=4, negative_marks=1)
        attempt = ExamAttempt.objects.create(exam=exam, student=self.student, status="submitted", total_marks=4,
            score_obtained=-1, incorrect_count=1)
        attempt.responses.create(exam_question=eq, selected_option=choice, order=1, is_correct=False, marks_awarded=-1)
        self.assertTrue(attempt.has_legacy_key_snapshots)
        self.assertIsNone(attempt.graded_possible_marks)
        self.client.force_login(self.student)
        review = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        item = review.context["reviewed_questions"][0]
        self.assertEqual(item["status"], "incorrect")  # not today's is_correct flag
        self.assertIsNone(item["saved_key"])
        self.assertEqual(item["explanation"], "")
        self.assertNotContains(review, "Current explanation")
        detail = self.client.get(reverse("exams:student_result_detail", args=[attempt.attempt_uuid]))
        self.assertContains(detail, "before grading-time key details were saved")
        import pymupdf
        pdf = self.client.get(reverse("exams:student_result_pdf", args=[attempt.attempt_uuid]))
        with pymupdf.open(stream=pdf.content, filetype="pdf") as document:
            self.assertIn("RECORDED RESULT", " ".join(page.get_text() for page in document))
        # A different missing key must not make the old typed maximum appear
        # known when rendering a partial result.
        single = Question.objects.create(category=self.category, creator=self.teacher, subject="Math",
            question_type="mcq_single", question_text="Unconfirmed PDF", is_pdf_derived=True, target_type="jee")
        eq2 = ExamQuestion.objects.create(exam=exam, question=single, order=2, positive_marks=4, negative_marks=1)
        attempt.responses.create(exam_question=eq2, order=2, key_confirmed_at_grading=False)
        from .pdf_key_results import partial_pdf_result_summary
        summary = partial_pdf_result_summary(attempt)
        self.assertEqual((summary["pending_keys"], summary["unknown_keys"], summary["possible_marks"]), (1, 1, None))
        attempt.refresh_from_db(); self.assertEqual(attempt.score_obtained, Decimal("-1"))

    def test_exam_builder_uses_actual_typed_key_and_overridden_mark_rules(self):
        paper = self.paper("teacher")
        self.client.force_login(self.teacher)
        self.client.post(reverse("questions:paper_import_answer_key", args=[paper.pk]),
                         {"answer_key_text": "1:[A,C], 2:0", "confirm_checked": "on"})
        self.client.post(reverse("questions:paper_bulk_approve", args=[paper.pk]))
        staged = list(paper.extracted_questions.order_by("question_number"))
        for q in staged: q.refresh_from_db()
        exam = Exam.objects.create(title="Readiness", slug="typed-readiness", category=self.category, creator=self.teacher, target_type="jee")
        multi = ExamQuestion.objects.create(exam=exam, question=staged[0].approved_question, order=1, positive_marks=4, negative_marks=1)
        ExamQuestion.objects.create(exam=exam, question=staged[1].approved_question, order=2, positive_marks=4, negative_marks=1)
        for name in ("exams:exam_questions", "exams:exam_preview"):
            page = self.client.get(reverse(name, args=[exam.pk]))
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, "No confirmed, valid key or scoring rule")
            self.assertContains(page, "Numerical key:")
        # A per-choice award above this particular exam's full mark is not a
        # valid scoring rule, even though the bank question itself is checked.
        multi.positive_marks = Decimal("0.5"); multi.save(update_fields=["positive_marks"])
        page = self.client.get(reverse("exams:exam_preview", args=[exam.pk]))
        self.assertContains(page, "No confirmed, valid key or scoring rule", count=1)

    def test_legacy_client_single_fk_is_saved_but_never_becomes_typed_answer(self):
        exam, attempt = self.start_student(self.paper("student"))
        item = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).context["questions_data"][0]
        option_id = item["options"][0]["id"]
        self.assertEqual(self.save(attempt, item, selected_option_id=option_id).status_code, 200)
        response = attempt.responses.get(pk=item["response_id"])
        self.assertEqual(response.selected_option_id, option_id)
        self.assertEqual(response.selected_option_ids, [])
        self.assertIsNone(response.numerical_response)
        # Navigation alone does not erase the older response; it must be
        # explicitly replaced or cleared by the candidate.
        self.assertEqual(self.save(attempt, item, selected_option_ids=[]).status_code, 200)
        response.refresh_from_db(); self.assertEqual(response.selected_option_id, option_id)
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), "{}", content_type="application/json")
        response.refresh_from_db()
        self.assertEqual(response.typed_grading_snapshot["outcome"], "ungraded")
        self.assertIsNone(response.typed_grading_snapshot["answer"])
        self.assertFalse(response.key_confirmed_at_grading)
        self.assertIsNone(response.is_correct)

    def test_multiple_rule_cases_and_legacy_single_response(self):
        self.client.force_login(self.student)
        exam = Exam.objects.create(title="Rules", slug="typed-rules", category=self.category, creator=self.teacher,
            status=Exam.ExamStatus.PUBLISHED, is_free=True, duration_minutes=30, shuffle_questions=False, target_type="jee")
        q = Question.objects.create(category=self.category, creator=self.teacher, subject="Math", question_text="Select",
            question_type="mcq_multiple", typed_key_verified=True, answer_status=AnswerStatus.MANUALLY_VERIFIED,
            multi_scoring_mode="per_correct", partial_credit_per_choice=Decimal("1"), target_type="jee")
        for k in "ABCD":
            QuestionOption.objects.create(question=q, option_key=k, option_text=k, is_correct=k in "AC")
        eq = ExamQuestion.objects.create(exam=exam, question=q, order=1, positive_marks=4, negative_marks=1)
        exam.recalculate_marks()
        for selected, expected, outcome in (("AC", "4", "correct"), ("A", "1", "partial"),
                                            ("AB", "-1", "incorrect"), ("", "0", "unattempted")):
            with self.subTest(selected=selected):
                attempt = ExamAttempt.objects.create(exam=exam, student=self.student, total_marks=4,
                    expires_at=timezone.now() + timedelta(minutes=10))
                response = attempt.responses.create(exam_question=eq, order=1,
                    selected_option_ids=list(q.options.filter(option_key__in=list(selected)).values_list("pk", flat=True)))
                attempt.submit_attempt(); response.refresh_from_db()
                self.assertEqual(response.marks_awarded, Decimal(expected))
                self.assertEqual(response.typed_grading_snapshot["outcome"], outcome)
        q.typed_key_verified = False; q.save(update_fields=["typed_key_verified"])
        old = ExamAttempt.objects.create(exam=exam, student=self.other, total_marks=4, expires_at=timezone.now() + timedelta(minutes=10))
        legacy = old.responses.create(exam_question=eq, selected_option=q.options.get(option_key="A"), order=1)
        old.submit_attempt(); legacy.refresh_from_db()
        self.assertEqual(legacy.typed_grading_snapshot["outcome"], "ungraded")
        self.assertIsNone(legacy.is_correct)
        self.assertFalse(legacy.key_confirmed_at_grading)
        self.assertEqual(old.score_obtained, 0)
