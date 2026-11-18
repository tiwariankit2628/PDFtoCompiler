"""Regression tests for local, free question-type detection.

These cover the heuristic classifier and its end-to-end effect in the extractor
and the approval flow. All fixtures are generated in memory; no user PDF or
third-party content is stored. Detection must never invent answers or content
and must only warn the reviewer.
"""
import tempfile

import pymupdf as fitz
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category
from .extractor import PDFQuestionExtractor
from .models import ExtractedQuestion, Question, QuestionOption, QuestionPaperUpload
from .question_types import classify_question, SINGLE, MULTIPLE, NUMERICAL, SUBJECTIVE
from .views import _bank_question_type


FOUR_OPTIONS = [{"key": k, "text": t} for k, t in zip("ABCD", ["w", "x", "y", "z"])]


class ClassifyQuestionTests(SimpleTestCase):
    def test_multiple_correct_phrases_detected_with_options(self):
        phrases = [
            "Which of the following statements is/are correct?",
            "One or more of the following options may be correct.",
            "More than one option can be true. Select all that apply.",
            "This is a multiple correct question.",
            "निम्न में से एक या अधिक कथन सही हैं।",
        ]
        for text in phrases:
            with self.subTest(text=text):
                detected, reason = classify_question(text, FOUR_OPTIONS)
                self.assertEqual(detected, MULTIPLE)
                self.assertIn("Multiple-correct", reason)

    def test_ambiguous_statements_with_combination_choices_are_one_choice_and_flagged(self):
        stem = "Which of the following statements is/are correct? A. First. B. Second. C. Third. D. Fourth. E. Fifth."
        combinations = [{"key": key, "text": text} for key, text in zip("ABCD", [
            "A, B and E only", "B, C and D only", "A, B and D only", "A, D and E only",
        ])]
        detected, reason = classify_question(stem, combinations)
        self.assertEqual(detected, SINGLE)
        self.assertIn("combinations of statements", reason)
        self.assertIn("confirm", reason)
        # Roman-numeral references and parenthesized statements are not tied to
        # one NEET paper; these remain human-reviewed in other exam layouts.
        roman = [{"key": key, "text": text} for key, text in zip("ABCD", [
            "(I) and (II) only", "(II) and (III) only", "(I) and (III) only", "I, II and III only",
        ])]
        self.assertEqual(classify_question("Which statements is/are correct?", roman)[0], SINGLE)

    def test_explicit_multi_select_wins_even_when_choices_are_combinations(self):
        combinations = [{"key": key, "text": text} for key, text in zip("ABCD", [
            "A and B only", "B and C only", "A and C only", "A, B and C only",
        ])]
        for instruction in ("One or more options are correct.", "More than one option is correct.",
                            "Select all that apply.", "Multiple correct answers.", "एक या अधिक विकल्प"):
            with self.subTest(instruction=instruction):
                stem = f"Which statements is/are correct? {instruction}"
                self.assertEqual(classify_question(stem, combinations)[0], MULTIPLE)

    def test_one_or_more_in_question_content_is_not_a_selection_instruction(self):
        stem = ("Read the following features and identify the organisms. "
                "(I) Possess one or more storage bodies located in the chloroplasts. "
                "(II) Reserve food material resembles amylopectin and glycogen. "
                "Choose the most appropriate answer from the options given below:")
        detected, reason = classify_question(stem, FOUR_OPTIONS)
        self.assertEqual((detected, reason), (SINGLE, ""))
        self.assertEqual(classify_question("A cell contains more than one nucleus. Choose one option.", FOUR_OPTIONS)[0], SINGLE)
        self.assertEqual(classify_question("One or more of the following options may be correct.", FOUR_OPTIONS)[0], MULTIPLE)

    def test_ordinary_multi_select_prose_is_not_reclassified_as_a_combination(self):
        prose = [{"key": key, "text": text} for key, text in zip("ABCD", [
            "The value of A and B is positive", "Only compound C is stable", "B and C react in water", "A is larger than B",
        ])]
        self.assertEqual(classify_question("Which statements is/are correct?", prose)[0], MULTIPLE)
        incomplete = [dict(FOUR_OPTIONS[0]), {"key": "B", "text": "A and B"},
                      {"key": "C", "text": "B and C"}, dict(FOUR_OPTIONS[3])]
        self.assertEqual(classify_question("Which statements is/are correct?", incomplete)[0], MULTIPLE)
        image_only = [{"key": key, "text": ""} for key in "ABCD"]
        self.assertEqual(classify_question("Which statements is/are correct?", image_only)[0], MULTIPLE)

    def test_numerical_phrases_detected_without_options(self):
        phrases = [
            "Find the value of x. Give the answer to the nearest integer.",
            "The numerical value of the current (in A) is",
            "Compute the result and round off to two decimal places.",
            "The answer is a non-negative integer.",
            "The length of the rod is __________ cm.",
            "विस्थापन का निकटतम पूर्णांक मान ज्ञात कीजिए।",
        ]
        for text in phrases:
            with self.subTest(text=text):
                detected, reason = classify_question(text, [])
                self.assertEqual(detected, NUMERICAL)
                self.assertIn("Numerical", reason)

    def test_subjective_verbs_detected_without_options(self):
        phrases = [
            "Explain the working principle of a transformer.",
            "Describe the process of photosynthesis in detail.",
            "Derive an expression for the time period of a pendulum.",
            "Prove that the diagonals of a parallelogram bisect each other.",
            "Write short notes on Newton's laws.",
            "ऊष्मागतिकी के प्रथम नियम की व्याख्या कीजिए।",
        ]
        for text in phrases:
            with self.subTest(text=text):
                detected, reason = classify_question(text, [])
                self.assertEqual(detected, SUBJECTIVE)
                self.assertIn("Subjective", reason)

    def test_ordinary_single_mcq_not_misclassified(self):
        # Words that look like cues ("correct", "describes", "value of") must not
        # trip detection when a proper option set is present.
        for text in [
            "Which of the following is the correct formula for water?",
            "Which option best describes the process of evaporation?",
            "The value of g is approximately 9.8. Which statement is true?",
            "What is the capital of France?",
        ]:
            with self.subTest(text=text):
                detected, reason = classify_question(text, FOUR_OPTIONS)
                self.assertEqual(detected, SINGLE)
                self.assertEqual(reason, "")

    def test_broken_mcq_without_cue_stays_single(self):
        # A genuinely broken MCQ (lost options, no type cue) must remain single
        # so the existing "minimum 2 options" check still applies.
        detected, reason = classify_question("What is the boiling point of water?", [{"key": "A", "text": "only one"}])
        self.assertEqual(detected, SINGLE)
        self.assertEqual(reason, "")

    def test_numerical_cue_with_full_options_stays_mcq(self):
        # "nearest integer" inside a normal 4-option MCQ is still an MCQ.
        detected, _ = classify_question(
            "The value of x rounded to the nearest integer is given by which option?", FOUR_OPTIONS)
        self.assertEqual(detected, SINGLE)


class _ExtractionFixture(TestCase):
    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.category = Category.objects.create(name="Mixed", slug="mixed", is_active=True)
        self.teacher = User.objects.create_user(
            username="types_teacher", password="Password123!", role=User.Roles.TEACHER, is_staff=True
        )

    def paper(self, content, title="Types Paper"):
        return QuestionPaperUpload.objects.create(
            title=title, category=self.category, subject="Science", uploaded_by=self.teacher,
            pdf_file=SimpleUploadedFile("paper.pdf", content, content_type="application/pdf"),
        )


def mixed_types_pdf():
    """A single page mixing single, numerical, multiple-correct and subjective."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    # Q1 ordinary single-correct MCQ
    page.insert_text((40, 80), "Q1 Which gas is the lightest?")
    page.insert_text((60, 100), "(A) Hydrogen")
    page.insert_text((60, 118), "(B) Helium")
    page.insert_text((60, 136), "(C) Oxygen")
    page.insert_text((60, 154), "(D) Nitrogen")
    # Q2 numerical / integer value, no options
    page.insert_text((40, 210), "Q2 A ball is dropped from 20 m. Find the time of fall.")
    page.insert_text((40, 228), "Give your answer rounded off to the nearest integer.")
    # Q3 multiple-correct MCQ with options
    page.insert_text((40, 300), "Q3 Which of the following statements is/are correct?")
    page.insert_text((60, 320), "(A) Water boils at 100 C at sea level")
    page.insert_text((60, 338), "(B) Ice is less dense than water")
    page.insert_text((60, 356), "(C) Sound travels in vacuum")
    page.insert_text((60, 374), "(D) The sky appears blue due to scattering")
    # Q4 subjective / essay, no options
    page.insert_text((40, 440), "Q4 Explain the working principle of a nuclear reactor.")
    data = doc.write()
    doc.close()
    return data


def combination_types_pdf():
    """Synthetic statement combinations followed by a genuine multi-select."""
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 60), "Q159 Which of the following statements is/are correct?")
        for index, label in enumerate("ABCDE"):
            page.insert_text((60, 80 + index * 18), f"{label}. Statement text {index + 1}.")
        for index, text in enumerate(("A, B and E only", "B, C and D only", "A, B and D only", "A, D and E only")):
            page.insert_text((60, 190 + index * 18), f"({index + 1}) {text}")
        page.insert_text((40, 320), "Q160 Which statements is/are correct? One or more options are correct.")
        for index, text in enumerate(("A and B only", "B and C only", "A and C only", "A, B and C only")):
            page.insert_text((60, 340 + index * 18), f"({index + 1}) {text}")
        return doc.write()


class CombinationExtractionTests(_ExtractionFixture):
    def test_explicit_numerical_section_keeps_later_combination_question_unscored(self):
        with fitz.open() as document:
            page = document.new_page(width=600, height=800)
            page.insert_text((40, 40), "SECTION: NUMERICAL ANSWER TYPE")
            page.insert_text((40, 60), "Q1 Find the numerical value. Give your answer as an integer.")
            page.insert_text((40, 180), "Q2 Which statements is/are correct?")
            for index, label in enumerate("ABCD"):
                page.insert_text((60, 200 + index * 18), f"{label}. Statement {index + 1}")
            for index, text in enumerate(("A and B only", "B and C only", "A and C only", "A, B and C only")):
                page.insert_text((60, 285 + index * 18), f"({index + 1}) {text}")
            page.insert_text((40, 380), "SECTION B: SINGLE-CORRECT QUESTIONS")
            page.insert_text((40, 410), "Q3 Which statements is/are correct?")
            for index, text in enumerate(("A and B only", "B and C only", "A and C only", "A, B and C only")):
                page.insert_text((60, 430 + index * 18), f"({index + 1}) {text}")
            paper = self.paper(document.write())
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 3)
        questions = list(paper.extracted_questions.order_by("question_number"))
        self.assertEqual([question.detected_type for question in questions],
                         [ExtractedQuestion.DetectedType.NUMERICAL, ExtractedQuestion.DetectedType.NUMERICAL,
                          ExtractedQuestion.DetectedType.SINGLE])
        self.assertTrue(questions[1].is_flagged)
        self.assertEqual(questions[1].correct_option, "")
        self.assertEqual(len(questions[1].options), 4)  # never drop source choices

    def test_section_header_between_questions_applies_to_the_next_question_not_the_previous(self):
        with fitz.open() as document:
            page = document.new_page(width=600, height=800)
            page.insert_text((40, 60), "Q1 Which gas is the lightest?")
            for index in range(4):
                page.insert_text((60, 80 + index * 18), f"({index + 1}) Gas {index + 1}")
            page.insert_text((40, 185), "SECTION B")
            page.insert_text((40, 205), "Each question has ONE or MORE THAN ONE correct choices.")
            page.insert_text((40, 235), "Q2 Which statements is/are correct?")
            for index, text in enumerate(("A and B only", "B and C only", "A and C only", "A, B and C only")):
                page.insert_text((60, 255 + index * 18), f"({index + 1}) {text}")
            # An unrecognized new section must end the previous scope.
            page.insert_text((40, 360), "SECTION C")
            page.insert_text((40, 390), "Q3 Which statements is/are correct?")
            for index, text in enumerate(("A and B only", "B and C only", "A and C only", "A, B and C only")):
                page.insert_text((60, 410 + index * 18), f"({index + 1}) {text}")
            paper = self.paper(document.write())
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 3)
        self.assertEqual([q.detected_type for q in paper.extracted_questions.order_by("question_number")],
                         [ExtractedQuestion.DetectedType.SINGLE, ExtractedQuestion.DetectedType.MULTIPLE,
                          ExtractedQuestion.DetectedType.SINGLE])

    def test_individual_question_multi_select_instruction_does_not_change_the_next_question(self):
        with fitz.open() as document:
            page = document.new_page(width=600, height=800)
            page.insert_text((40, 60), "Q1 One or more options are correct.")
            for index in range(4):
                page.insert_text((60, 80 + index * 18), f"({index + 1}) Choice {index + 1}")
            page.insert_text((40, 200), "Q2 Which statements is/are correct?")
            for index, text in enumerate(("A and B only", "B and C only", "A and C only", "A, B and C only")):
                page.insert_text((60, 220 + index * 18), f"({index + 1}) {text}")
            paper = self.paper(document.write())
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 2)
        self.assertEqual([q.detected_type for q in paper.extracted_questions.order_by("question_number")],
                         [ExtractedQuestion.DetectedType.MULTIPLE, ExtractedQuestion.DetectedType.SINGLE])

    def test_section_multi_select_instruction_takes_priority_over_combination_heuristic(self):
        with fitz.open() as document:
            page = document.new_page(width=600, height=800)
            page.insert_text((40, 35), "SECTION A: ONE or MORE THAN ONE choices are correct.")
            page.insert_text((40, 60), "Q1 Choose the applicable choices.")
            for index in range(4):
                page.insert_text((60, 80 + index * 18), f"({index + 1}) Choice {index + 1}")
            page.insert_text((40, 210), "Q2 Which statements is/are correct?")
            for index, label in enumerate("ABCD"):
                page.insert_text((60, 230 + index * 18), f"{label}. Statement {index + 1}")
            for index, text in enumerate(("A and B only", "B and C only", "A and C only", "A, B and C only")):
                page.insert_text((60, 315 + index * 18), f"({index + 1}) {text}")
            page.insert_text((40, 425), "SECTION B: ONLY ONE answer is correct.")
            page.insert_text((40, 450), "Q3 Choose the applicable choice.")
            for index in range(4):
                page.insert_text((60, 470 + index * 18), f"({index + 1}) Last choice {index + 1}")
            paper = self.paper(document.write())
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 3)
        self.assertEqual([q.detected_type for q in paper.extracted_questions.order_by("question_number")],
                         [ExtractedQuestion.DetectedType.MULTIPLE, ExtractedQuestion.DetectedType.MULTIPLE,
                          ExtractedQuestion.DetectedType.SINGLE])
        second = paper.extracted_questions.get(question_number=2)
        self.assertIn("section instructions", second.flag_reason)
        self.assertEqual(second.correct_option, "")

    def test_numbered_combination_choices_remain_four_and_need_human_review(self):
        paper = self.paper(combination_types_pdf())
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 2)
        q = paper.extracted_questions.get(question_number=159)
        self.assertEqual(q.detected_type, ExtractedQuestion.DetectedType.SINGLE)
        self.assertEqual([option["key"] for option in q.options], list("ABCD"))
        self.assertEqual([option["text"] for option in q.options], [
            "A, B and E only", "B, C and D only", "A, B and D only", "A, D and E only",
        ])
        self.assertTrue(q.is_flagged)
        self.assertIn("combinations of statements", q.flag_reason)
        self.assertEqual(q.correct_option, "")
        self.assertNotEqual(q.answer_status, "manually_verified")
        explicit_multiple = paper.extracted_questions.get(question_number=160)
        self.assertEqual(explicit_multiple.detected_type, ExtractedQuestion.DetectedType.MULTIPLE)


class MixedTypeExtractionTests(_ExtractionFixture):
    def setUp(self):
        super().setUp()
        self.paper_obj = self.paper(mixed_types_pdf())
        self.assertEqual(PDFQuestionExtractor(self.paper_obj).extract(), 4)

    def _q(self, number):
        return self.paper_obj.extracted_questions.get(question_number=number)

    def test_single_correct_question_unchanged(self):
        q1 = self._q(1)
        self.assertEqual(q1.detected_type, ExtractedQuestion.DetectedType.SINGLE)
        self.assertEqual(len(q1.options), 4)
        self.assertFalse(q1.is_flagged, q1.flag_reason)

    def test_numerical_question_detected_and_flagged_not_broken_mcq(self):
        q2 = self._q(2)
        self.assertEqual(q2.detected_type, ExtractedQuestion.DetectedType.NUMERICAL)
        self.assertTrue(q2.is_flagged)
        self.assertIn("Numerical", q2.flag_reason)
        # It must NOT be mislabelled as a broken MCQ.
        self.assertNotIn("Minimum 2 required", " ".join(q2.extraction_issues))
        # Content is preserved, never invented.
        self.assertIn("time of fall", q2.question_text)
        self.assertEqual(q2.options, [])

    def test_multiple_correct_detected_and_flagged_but_keeps_options(self):
        q3 = self._q(3)
        self.assertEqual(q3.detected_type, ExtractedQuestion.DetectedType.MULTIPLE)
        self.assertTrue(q3.is_flagged)
        self.assertIn("Multiple-correct", q3.flag_reason)
        self.assertEqual(len(q3.options), 4)
        self.assertNotIn("Minimum 2 required", " ".join(q3.extraction_issues))

    def test_subjective_detected_and_flagged_not_broken_mcq(self):
        q4 = self._q(4)
        self.assertEqual(q4.detected_type, ExtractedQuestion.DetectedType.SUBJECTIVE)
        self.assertTrue(q4.is_flagged)
        self.assertIn("Subjective", q4.flag_reason)
        self.assertNotIn("Minimum 2 required", " ".join(q4.extraction_issues))
        self.assertIn("nuclear reactor", q4.question_text)

    def test_review_page_shows_type_badges(self):
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("questions:paper_review", args=[self.paper_obj.pk]))
        self.assertContains(response, "Numerical / Integer value")
        self.assertContains(response, "Multiple-correct MCQ")
        self.assertContains(response, "Subjective / Essay")


class ApprovalTypeMappingTests(_ExtractionFixture):
    def test_bank_question_type_mapping_helper(self):
        cases = {
            ExtractedQuestion.DetectedType.SINGLE: Question.QuestionType.MCQ_SINGLE,
            ExtractedQuestion.DetectedType.MULTIPLE: Question.QuestionType.MCQ_MULTIPLE,
            ExtractedQuestion.DetectedType.NUMERICAL: Question.QuestionType.NUMERICAL,
            # Subjective has no bank equivalent; stays single until converted.
            ExtractedQuestion.DetectedType.SUBJECTIVE: Question.QuestionType.MCQ_SINGLE,
        }
        for detected, expected in cases.items():
            staged = ExtractedQuestion(detected_type=detected)
            self.assertEqual(_bank_question_type(staged), expected)

    def test_multiple_correct_approval_sets_bank_type(self):
        paper = self.paper(mixed_types_pdf())
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 4)
        q3 = paper.extracted_questions.get(question_number=3)
        self.assertEqual(q3.detected_type, ExtractedQuestion.DetectedType.MULTIPLE)
        # Reviewer confirms the item and clears the flag before approval.
        q3.is_flagged = False
        q3.flag_reason = ""
        q3.correct_option = "A"
        q3.save()
        self.client.force_login(self.teacher)
        response = self.client.post(reverse("questions:extracted_approve", args=[q3.pk]))
        self.assertEqual(response.status_code, 302)
        q3.refresh_from_db()
        self.assertIsNotNone(q3.approved_question)
        self.assertEqual(q3.approved_question.question_type, Question.QuestionType.MCQ_MULTIPLE)

    def test_multiple_correct_not_linked_to_single_correct_duplicate(self):
        # An identical-looking single-correct bank item must not absorb a
        # multiple-correct question; their types differ.
        paper = self.paper(mixed_types_pdf())
        self.assertEqual(PDFQuestionExtractor(paper).extract(), 4)
        q3 = paper.extracted_questions.get(question_number=3)
        existing = Question.objects.create(
            category=self.category, subject="Science",
            question_text=q3.question_text, question_type=Question.QuestionType.MCQ_SINGLE,
        )
        for index, opt in enumerate(q3.options):
            QuestionOption.objects.create(
                question=existing, option_key=opt["key"], option_text=opt["text"], order=index)
        q3.is_flagged = False
        q3.flag_reason = ""
        q3.save()
        self.client.force_login(self.teacher)
        self.client.post(reverse("questions:extracted_approve", args=[q3.pk]))
        q3.refresh_from_db()
        self.assertIsNotNone(q3.approved_question)
        self.assertNotEqual(q3.approved_question_id, existing.pk)
        self.assertEqual(q3.approved_question.question_type, Question.QuestionType.MCQ_MULTIPLE)
