"""Regression tests for varied locally parsed PDF page layouts.

Fixtures are synthetic; no private exam documents are included in the repo.
"""
import pymupdf as fitz
from django.test import SimpleTestCase

from .pdf_layout import DigitalQuestionLayout


def add_question(page, number, x, y, *, prefix=True, wide=False):
    label = f"Q{number}" if prefix else f"{number}."
    body = (f"This statement continues beyond the centre to QUESTION-{number}-TAIL"
            if wide else f"Choose answer {number}?")
    page.insert_text((x, y), f"{label} {body}", fontsize=11)
    for index in range(4):
        choice = (f"This long option continues beyond the centre to OPTION-{number}-{index + 1}-TAIL"
                  if wide else f"Choice {number}-{index + 1}")
        page.insert_text((x + 10, y + 40 + index * 20), f"({index + 1}) {choice}", fontsize=10)


def mixed_column_fixture(*, prefix=True, reverse=False):
    doc = fitz.open()
    if reverse:
        page = doc.new_page(width=600, height=800)
        add_question(page, 1, 40, 100, prefix=prefix, wide=True)
        add_question(page, 2, 40, 400, prefix=prefix, wide=True)
        first, second = 3, 4
    else:
        first, second = 1, 2
    page = doc.new_page(width=600, height=800)
    for number, x, y in ((first, 40, 90), (second, 40, 380),
                          (first + 2, 340, 90), (second + 2, 340, 380)):
        add_question(page, number, x, y, prefix=prefix)
    if not reverse:
        page = doc.new_page(width=600, height=800)
        add_question(page, 5, 40, 100, prefix=prefix, wide=True)
        add_question(page, 6, 40, 400, prefix=prefix, wide=True)
    return doc


class VariedPageLayoutTests(SimpleTestCase):
    def check_mixed(self, *, prefix=True, reverse=False):
        doc = mixed_column_fixture(prefix=prefix, reverse=reverse)
        self.addCleanup(doc.close)
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([question.number for question in questions], list(range(1, 7)))
        wide_numbers = (1, 2) if reverse else (5, 6)
        for number in wide_numbers:
            question = questions[number - 1]
            self.assertIn(f"QUESTION-{number}-TAIL", question.text)
            self.assertEqual(len(question.options), 4)
            for index, option in enumerate(question.options, 1):
                self.assertIn(f"OPTION-{number}-{index}-TAIL", option["text"])
                self.assertNotIn(f"OPTION-{7 - number}-", option["text"])
        return questions

    def test_explicit_essay_subparts_are_preserved_but_not_graded_as_choices(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 35), "One essay type question and short note is compulsory from each unit.")
        for number, y in ((1, 90), (2, 390)):
            page.insert_text((40, y), f"Q. No. {number}.(a) Explain principle {number}.")
            page.insert_text((55, y + 30), "Marks: 10")
            page.insert_text((55, y + 55), "OR")
            page.insert_text((55, y + 80), f"Discuss another aspect of principle {number}.")
            page.insert_text((55, y + 110), f"(b) Write a short note for {number}.")
            page.insert_text((55, y + 135), "Marks: 6")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([q.number for q in questions], [1, 2])
        for question in questions:
            self.assertFalse(question.options)
            self.assertIn("OR", question.text)
            self.assertIn("short note", question.text)
            self.assertTrue(any("essay" in issue.lower() for issue in question.issues))

    def test_ocr_q_no_label_damage_is_inferred_and_flagged(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 30), "One essay type question and short note is compulsory from each unit.")
        page.insert_text((40, 90), "Q. No. 1.(a) Explain first principle.")
        page.insert_text((50, 120), "Marks: 10")
        page.insert_text((50, 150), "(b) Write a short note.")
        page.insert_text((40, 300), "2. No. 2. (a) Explain second principle.")
        page.insert_text((50, 330), "Marks: 10")
        page.insert_text((50, 360), "(b) Write another short note.")
        textpage = page.get_textpage()
        questions = DigitalQuestionLayout(doc, lambda _: None, {0: (page, textpage)}).extract()
        self.assertEqual([q.number for q in questions], [1, 2])
        self.assertTrue(any("inferred" in issue.lower() for issue in questions[1].issues))
        self.assertFalse(questions[1].options)

    def test_declared_question_count_mismatch_warns_without_inventing_items(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 30), "Total number of questions: 4")
        for number, y in ((1, 80), (2, 280), (3, 480)):
            add_question(page, number, 40, y)
        logs = []
        questions = DigitalQuestionLayout(doc, logs.append).extract()
        self.assertEqual([q.number for q in questions], [1, 2, 3])
        self.assertTrue(any("states 4 questions" in log and "3 were detected" in log for log in logs), logs)

    def test_section_question_count_is_not_mistaken_for_total(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 30), "This section contains 10 questions.")
        for number, y in ((1, 80), (2, 280), (3, 480)):
            add_question(page, number, 40, y)
        logs = []
        DigitalQuestionLayout(doc, logs.append).extract()
        self.assertFalse(any("paper states" in log for log in logs), logs)

    def test_plain_question_sequence_outweighs_stray_q_references(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        for page_index in range(2):
            page = doc.new_page(width=600, height=800)
            for index in range(4):
                number = page_index * 4 + index + 1
                y = 75 + index * 170
                add_question(page, number, 40, y, prefix=False)
            if page_index == 1:
                page.insert_text((235, 55), "Q.5 to Q.8 carry two marks each")
                page.insert_text((200, 145), "Q1: notation within a question")
                page.insert_text((200, 170), "Q2: notation within a question")
        logs = []
        questions = DigitalQuestionLayout(doc, logs.append).extract()
        self.assertEqual([q.number for q in questions], list(range(1, 9)))
        self.assertTrue(any("question count" in log for log in logs))
        self.assertEqual([o["text"] for o in questions[0].options],
                         [f"Choice 1-{n}" for n in range(1, 5)])

    def test_trailing_sequential_non_mcq_questions_are_retained_for_review(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=800)
        for number, y in ((1, 80), (2, 240), (3, 400)):
            add_question(page, number, 40, y, prefix=False)
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "4. Calculate the numerical value of the expression.")
        page.insert_text((40, 180), "5. Determine the integer value of the following expression.")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([q.number for q in questions], [1, 2, 3, 4, 5])
        self.assertTrue(all(any("numbering was inferred" in issue.lower() for issue in q.issues)
                            for q in questions[-2:]))
        self.assertTrue(all(not q.options for q in questions[-2:]))

    def test_repeated_option_labels_near_page_edges_are_not_cut_as_headers(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        for number in range(1, 4):
            page = doc.new_page(width=600, height=800)
            page.insert_text((40, 20), f"Q{number}. Question at the top of the page?")
            for option, y in ((1, 45), (2, 70), (3, 95), (4, 740)):
                page.insert_text((55, y), f"({option})")
                page.insert_text((85, y), f"Choice {number}-{option}")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([q.number for q in questions], [1, 2, 3])
        for number, question in enumerate(questions, 1):
            self.assertIn("Question at the top", question.text)
            self.assertEqual([o["text"] for o in question.options],
                             [f"Choice {number}-{option}" for option in range(1, 5)])

    def test_parallel_translations_are_not_merged_or_counted_twice(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=850)
        for number, y in ((1, 80), (2, 310), (3, 540)):
            page.insert_text((30, y), f"{number}. H$m$m {{{number}}} $` localized content")
            page.insert_text((330, y), f"{number}. Which of the following is item {number}?")
            for option in range(1, 5):
                page.insert_text((45, y + 25 * option), f"({option}) alternate {number}-{option}")
                page.insert_text((345, y + 25 * option), f"({option}) English {number}-{option}")
        logs = []
        layout = DigitalQuestionLayout(doc, logs.append)
        questions = layout.extract()
        self.assertEqual([q.number for q in questions], [1, 2, 3])
        self.assertEqual(layout._columns_on(0), 2)
        for number, question in enumerate(questions, 1):
            self.assertIn(f"item {number}?", question.text)
            self.assertNotIn("localized", question.text)
            self.assertEqual([o["text"] for o in question.options],
                             [f"English {number}-{option}" for option in range(1, 5)])
            source = next(f for f in question.extra_figures if f["source"] == "parallel-source")
            page_rect = page.rect
            box = fitz.Rect(source["box"][0] * page_rect.width, source["box"][1] * page_rect.height,
                            source["box"][2] * page_rect.width, source["box"][3] * page_rect.height)
            alternate = page.get_text("text", clip=box)
            self.assertIn(f"alternate {number}-4", alternate)
            self.assertNotIn(f"alternate {number + 1}-1", alternate)
            self.assertTrue(any("parallel" in issue.lower() for issue in question.issues))
            self.assertEqual(len(question.source_regions), 2)

    def test_parallel_question_continues_across_pages_without_merging_neighbours(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=800)
        for number, y in ((1, 70), (2, 270), (3, 650)):
            for x, localized in ((30, True), (330, False)):
                title = "H$m$m {x} $` localized" if localized else "Which of the following is English"
                page.insert_text((x, y), f"{number}. {title} {number}?")
                count = 2 if number == 3 else 4
                for option in range(1, count + 1):
                    page.insert_text((x + 15, y + 25 * option),
                                     f"({option}) {'alt' if localized else 'English'} {number}-{option}")
        page = doc.new_page(width=600, height=800)
        for x, localized in ((30, True), (330, False)):
            for option, y in ((3, 50), (4, 75)):
                page.insert_text((x + 15, y), f"({option}) {'alt' if localized else 'English'} 3-{option}")
            for number, y in ((4, 180), (5, 380), (6, 580)):
                title = "H$m$m {x} $` localized" if localized else "Which of the following is English"
                page.insert_text((x, y), f"{number}. {title} {number}?")
                for option in range(1, 5):
                    page.insert_text((x + 15, y + 25 * option),
                                     f"({option}) {'alt' if localized else 'English'} {number}-{option}")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([q.number for q in questions], list(range(1, 7)))
        third = questions[2]
        self.assertEqual([o["text"] for o in third.options], [f"English 3-{n}" for n in range(1, 5)])
        sources = [f for f in third.extra_figures if f["source"] == "parallel-source"]
        self.assertEqual([f["page"] for f in sources], [1, 2])
        self.assertEqual(len(third.source_regions), 4)
        self.assertIn("English 4", questions[3].text)
        self.assertNotIn("English 3", questions[3].text)

    def test_missing_parallel_boundary_is_flagged_instead_of_cropped(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=900)
        for number, y in ((1, 70), (2, 270), (3, 470), (4, 670)):
            page.insert_text((330, y), f"{number}. Which of the following is English {number}?")
            for option in range(1, 5):
                page.insert_text((345, y + 25 * option), f"({option}) English {number}-{option}")
            if number != 2:
                page.insert_text((30, y), f"{number}. H$m$m {{x}} $` localized {number}")
                for option in range(1, 5):
                    page.insert_text((45, y + 25 * option), f"({option}) alt {number}-{option}")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([q.number for q in questions], [1, 2, 3, 4])
        self.assertTrue(any("parallel-language boundary" in issue.lower() for issue in questions[1].issues))
        self.assertFalse(any(f["source"] == "parallel-source" for f in questions[1].extra_figures))

    def test_column_major_two_column_choices_use_their_original_keys(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "Q1. Which example is correct?")
        for label, x, y, value in (("a", 55, 130, "Alpha choice"), ("c", 300, 130, "Charlie choice"),
                                    ("b", 55, 165, "Bravo choice"), ("d", 300, 165, "Delta choice")):
            page.insert_text((x, y), f"{label}) {value}")
        page.insert_text((40, 260), "Q2. Next question?")
        for option in range(1, 5):
            page.insert_text((55, 260 + 22 * option), f"({option}) Next {option}")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([q.number for q in questions], [1, 2])
        self.assertEqual([(o["key"], o["text"]) for o in questions[0].options],
                         [("A", "Alpha choice"), ("B", "Bravo choice"),
                          ("C", "Charlie choice"), ("D", "Delta choice")])
        self.assertTrue(any("column" in issue.lower() and "option" in issue.lower()
                            for issue in questions[0].issues))

    def test_multiple_correct_instruction_is_flagged_until_single_choice_section(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), "Q1. ONE or MORE THAN ONE choices are correct.")
        for option in range(1, 5):
            page.insert_text((55, 80 + 22 * option), f"({option}) First {option}")
        add_question(page, 2, 40, 270)
        page.insert_text((40, 470), "Q3. Only one answer is correct.")
        for option in range(1, 5):
            page.insert_text((55, 470 + 22 * option), f"({option}) Last {option}")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([q.number for q in questions], [1, 2, 3])
        self.assertTrue(all(any("multiple correct" in issue.lower() for issue in q.issues)
                            for q in questions[:2]))
        self.assertFalse(any("multiple correct" in issue.lower() for issue in questions[2].issues))

    def test_two_column_page_then_full_width_prefixed_questions(self):
        self.check_mixed()

    def test_full_width_page_then_two_column_prefixed_questions(self):
        self.check_mixed(reverse=True)

    def test_two_column_page_then_full_width_numbered_questions(self):
        self.check_mixed(prefix=False)

    def test_full_width_page_then_two_column_numbered_questions(self):
        self.check_mixed(prefix=False, reverse=True)

    def test_three_equal_columns_preserve_reading_order_and_all_options(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=800)
        for index, x in enumerate((20, 220, 420)):
            add_question(page, 2 * index + 1, x, 90)
            add_question(page, 2 * index + 2, x, 380)
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([question.number for question in questions], list(range(1, 7)))
        for question in questions:
            self.assertEqual([option["text"] for option in question.options],
                             [f"Choice {question.number}-{n}" for n in range(1, 5)])

    def test_three_equal_columns_with_plain_numbered_questions(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=800)
        for index, x in enumerate((20, 220, 420)):
            add_question(page, 2 * index + 1, x, 90, prefix=False)
            add_question(page, 2 * index + 2, x, 380, prefix=False)
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([question.number for question in questions], list(range(1, 7)))
        for question in questions:
            self.assertEqual([option["text"] for option in question.options],
                             [f"Choice {question.number}-{n}" for n in range(1, 5)])

    def test_conflicting_span_on_a_two_column_page_is_flagged(self):
        doc = mixed_column_fixture(); self.addCleanup(doc.close)
        page = doc[0]
        page.insert_text((40, 300), "Long text across both columns: CHECK THE SOURCE CAREFULLY", fontsize=11)
        page.insert_text((40, 320), "A second wide line crosses the column boundary here", fontsize=11)
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertTrue(any("spans a detected column boundary" in issue
                            for question in questions[:4] for issue in question.issues))

    def test_full_width_side_by_side_options_do_not_create_columns(self):
        doc = fitz.open(); self.addCleanup(doc.close)
        page = doc.new_page(width=600, height=800)
        for number, y in ((1, 90), (2, 380)):
            page.insert_text((40, y), f"Q{number} Select the correct statement?")
            for label, x, dy in ((1, 60, 40), (2, 330, 40), (3, 60, 75), (4, 330, 75)):
                page.insert_text((x, y + dy), f"({label}) Choice {number}-{label}")
        questions = DigitalQuestionLayout(doc, lambda _: None).extract()
        self.assertEqual([question.number for question in questions], [1, 2])
        for question in questions:
            self.assertEqual([o["text"] for o in question.options],
                             [f"Choice {question.number}-{n}" for n in range(1, 5)])
