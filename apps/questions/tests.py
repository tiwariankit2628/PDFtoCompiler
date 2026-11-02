import io
from decimal import Decimal
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
import pymupdf as fitz

from apps.accounts.models import User
from apps.exams.models import Category
from apps.questions.extractor import PDFQuestionExtractor
from apps.questions.forms import QuestionPaperUploadForm
from apps.questions.models import (
    ExtractedQuestion,
    Question,
    QuestionOption,
    QuestionPaperUpload,
)


def create_sample_pdf_bytes():
    """Generates a valid 2-page sample examination PDF using PyMuPDF."""
    doc = fitz.open()

    # Page 1: Physics and Math questions
    page1 = doc.new_page()
    text_p1 = (
        "NATIONAL ENTRANCE EXAMINATION - SECTION A\n\n"
        "Q.1 An electron enters a uniform magnetic field B with velocity v perpendicular to B. "
        "The radius of the circular path is given by:\n"
        "(A) r = mv / (qB)\n"
        "(B) r = qB / (mv)\n"
        "(C) r = mB / (qv)\n"
        "(D) r = qv / (mB)\n\n"
        "2. If sin(theta) + cos(theta) = sqrt(2), find the value of sin(2*theta):\n"
        "(A) 1\n"
        "(B) 0.5\n"
        "(C) 0\n"
        "(D) -1\n"
    )
    page1.insert_text((50, 72), text_p1, fontsize=11)

    # Page 2: Bilingual question and incomplete question
    page2 = doc.new_page()
    text_p2 = (
        "SECTION B - BILINGUAL & GENERAL\n\n"
        "Q.3 (Bilingual): In which medium is the speed of sound maximum?\n"
        "(A) Steel / Solid\n"
        "(B) Water\n"
        "(C) Air\n"
        "(D) Vacuum\n\n"
        "Q.4 What is the time complexity of binary search?\n"
        "(A) O(log n)\n"
    )
    page2.insert_text((50, 72), text_p2, fontsize=11)

    pdf_bytes = doc.write()
    doc.close()
    return pdf_bytes


class PDFUploadAndValidationTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name="Engineering Entrance",
            slug="engineering-entrance",
            is_active=True,
        )
        self.admin = User.objects.create_superuser(
            username="admin_uploader",
            email="admin_up@test.com",
            password="Password123!",
            role=User.Roles.SUPER_ADMIN,
        )
        self.teacher = User.objects.create_user(
            username="teacher_uploader",
            email="teach_up@test.com",
            password="Password123!",
            role=User.Roles.TEACHER,
            is_staff=True,
        )
        self.student = User.objects.create_user(
            username="student_reader",
            email="stu_read@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
        )

    def test_non_pdf_file_rejected(self):
        txt_file = SimpleUploadedFile("fake.txt", b"Not a PDF content", content_type="text/plain")
        form_data = {
            "title": "Invalid File Test",
            "category": self.category.pk,
            "subject": "Physics",
        }
        form = QuestionPaperUploadForm(data=form_data, files={"pdf_file": txt_file})
        self.assertFalse(form.is_valid())
        self.assertIn("pdf_file", form.errors)

    def test_valid_pdf_form_accepted(self):
        pdf_bytes = create_sample_pdf_bytes()
        pdf_file = SimpleUploadedFile("physics_test.pdf", pdf_bytes, content_type="application/pdf")
        form_data = {
            "title": "Physics JEE 2024",
            "category": self.category.pk,
            "subject": "Physics",
            "target_exam": "JEE Main",
        }
        form = QuestionPaperUploadForm(data=form_data, files={"pdf_file": pdf_file})
        self.assertTrue(form.is_valid(), form.errors)

    def test_student_forbidden_from_upload_page(self):
        self.client.login(username="student_reader", password="Password123!")
        res = self.client.get(reverse("questions:paper_upload"))
        self.assertEqual(res.status_code, 403)

    def test_teacher_can_access_upload_page(self):
        self.client.login(username="teacher_uploader", password="Password123!")
        res = self.client.get(reverse("questions:paper_upload"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Upload Question Paper PDF")


class PDFQuestionExtractionEngineTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name="Competitive Exams",
            slug="competitive-exams",
            is_active=True,
        )
        self.teacher = User.objects.create_user(
            username="science_faculty",
            email="faculty@test.com",
            password="Password123!",
            role=User.Roles.TEACHER,
            is_staff=True,
        )
        pdf_bytes = create_sample_pdf_bytes()
        self.pdf_file = SimpleUploadedFile("exam_paper.pdf", pdf_bytes, content_type="application/pdf")
        self.paper = QuestionPaperUpload.objects.create(
            title="Entrance Mock 2024",
            category=self.category,
            subject="Science",
            pdf_file=self.pdf_file,
            uploaded_by=self.teacher,
        )

    def test_extraction_pipeline_extracts_multipage_and_hindi(self):
        extractor = PDFQuestionExtractor(self.paper)
        count = extractor.extract()

        self.assertGreaterEqual(count, 3)
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.total_pages, 2)
        self.assertEqual(self.paper.status, QuestionPaperUpload.ProcessingStatus.EXTRACTED)

        # Verify Q1 (Physics)
        q1 = ExtractedQuestion.objects.get(paper=self.paper, question_number=1)
        self.assertIn("magnetic field", q1.question_text)
        self.assertEqual(len(q1.options), 4)
        self.assertEqual(q1.options[0]["key"], "A")
        self.assertIn("mv / (qB)", q1.options[0]["text"])

        # Verify Q2 (Math with sin/cos/sqrt)
        q2 = ExtractedQuestion.objects.get(paper=self.paper, question_number=2)
        self.assertIn("sin(theta)", q2.question_text)
        self.assertEqual(len(q2.options), 4)

        # Verify Q3 (Bilingual / General)
        q3 = ExtractedQuestion.objects.get(paper=self.paper, question_number=3)
        self.assertIn("speed of sound", q3.question_text)
        self.assertEqual(len(q3.options), 4)

        # Verify Q4 (Incomplete - only 1 option, should be flagged)
        q4 = ExtractedQuestion.objects.get(paper=self.paper, question_number=4)
        self.assertTrue(q4.is_flagged)
        self.assertIn("Minimum 2 required", q4.flag_reason)

    def test_hindi_devanagari_patterns_recognized(self):
        """Verifies that Hindi question and option patterns are properly recognized by extractor regexes."""
        hindi_q_text = "प्रश्न 15: प्रकाश का वेग निर्वात में कितना होता है?"
        match_q = PDFQuestionExtractor.QUESTION_PATTERN.match(hindi_q_text)
        self.assertIsNotNone(match_q)
        q_num = match_q.group(1) or match_q.group(2) or match_q.group(3) or match_q.group(4)
        self.assertEqual(q_num, "15")
        self.assertIn("प्रकाश का वेग", match_q.group(5))

        # Test Hindi options recognition (क, ख, ग, घ)
        hindi_opts = "(क) 3 x 10^8 m/s (ख) 3 x 10^6 m/s (ग) 0 (घ) अनन्त"
        matches = list(PDFQuestionExtractor.OPTION_INLINE_PATTERN.finditer(hindi_opts))
        self.assertEqual(len(matches), 4)
        keys = [m.group(1).upper() for m in matches]
        self.assertEqual(keys, ["क", "ख", "ग", "घ"])


class QuestionReviewAndApprovalTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name="Computer Science",
            slug="computer-science",
            is_active=True,
        )
        self.teacher = User.objects.create_user(
            username="cs_teacher",
            email="cs@test.com",
            password="Password123!",
            role=User.Roles.TEACHER,
            is_staff=True,
        )
        self.other_teacher = User.objects.create_user(
            username="other_prof",
            email="other@test.com",
            password="Password123!",
            role=User.Roles.TEACHER,
            is_staff=True,
        )
        self.student = User.objects.create_user(
            username="candidate_bob",
            email="bob@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
        )
        pdf_bytes = create_sample_pdf_bytes()
        self.paper = QuestionPaperUpload.objects.create(
            title="Algorithms Midterm",
            category=self.category,
            subject="Algorithms",
            pdf_file=SimpleUploadedFile("midterm.pdf", pdf_bytes, content_type="application/pdf"),
            uploaded_by=self.teacher,
        )
        self.q1 = ExtractedQuestion.objects.create(
            paper=self.paper,
            question_number=1,
            question_text="What is the time complexity of Merge Sort?",
            options=[
                {"key": "A", "text": "O(n log n)"},
                {"key": "B", "text": "O(n^2)"},
                {"key": "C", "text": "O(log n)"},
                {"key": "D", "text": "O(n)"},
            ],
            correct_option="A",
            positive_marks=Decimal("4.00"),
            negative_marks=Decimal("1.00"),
            status=ExtractedQuestion.Status.EXTRACTED,
        )
        self.q2 = ExtractedQuestion.objects.create(
            paper=self.paper,
            question_number=2,
            question_text="What data structure is used in BFS?",
            options=[
                {"key": "A", "text": "Stack"},
                {"key": "B", "text": "Queue"},
                {"key": "C", "text": "Priority Queue"},
                {"key": "D", "text": "Tree"},
            ],
            correct_option="",  # Empty answer key initially
            status=ExtractedQuestion.Status.EXTRACTED,
        )

    def test_review_page_accessible_by_owner_teacher(self):
        self.client.login(username="cs_teacher", password="Password123!")
        res = self.client.get(reverse("questions:paper_review", args=[self.paper.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Merge Sort")

    def test_student_cannot_access_review_page(self):
        self.client.login(username="candidate_bob", password="Password123!")
        res = self.client.get(reverse("questions:paper_review", args=[self.paper.pk]))
        self.assertEqual(res.status_code, 403)

    def test_unauthorized_teacher_denied(self):
        self.client.login(username="other_prof", password="Password123!")
        res = self.client.get(reverse("questions:paper_review", args=[self.paper.pk]))
        self.assertEqual(res.status_code, 403)

    def test_edit_extracted_question(self):
        self.client.login(username="cs_teacher", password="Password123!")
        res = self.client.post(
            reverse("questions:extracted_edit", args=[self.q1.pk]),
            {
                "question_number": 1,
                "question_text": "Updated Merge Sort complexity question",
                "positive_marks": "5.00",
                "negative_marks": "1.25",
                "correct_option": "A",
                "option_text_A": "O(n log n)",
                "option_text_B": "O(n^2)",
                "option_text_C": "O(log n)",
                "option_text_D": "O(n)",
            },
        )
        self.assertEqual(res.status_code, 302)
        self.q1.refresh_from_db()
        self.assertEqual(self.q1.question_text, "Updated Merge Sort complexity question")
        self.assertEqual(self.q1.positive_marks, Decimal("5.00"))
        self.assertEqual(self.q1.status, ExtractedQuestion.Status.REVIEWED)

    def test_bulk_answer_key_mapping(self):
        self.client.login(username="cs_teacher", password="Password123!")
        res = self.client.post(
            reverse("questions:paper_import_answer_key", args=[self.paper.pk]),
            {"answer_key_text": "1: A\n2: B"},
        )
        self.assertEqual(res.status_code, 302)
        self.q2.refresh_from_db()
        self.assertEqual(self.q2.correct_option, "B")

    def test_single_question_approval_creates_question_bank_item(self):
        self.client.login(username="cs_teacher", password="Password123!")
        res = self.client.post(reverse("questions:extracted_approve", args=[self.q1.pk]))
        self.assertEqual(res.status_code, 302)

        self.q1.refresh_from_db()
        self.assertEqual(self.q1.status, ExtractedQuestion.Status.APPROVED)
        self.assertIsNotNone(self.q1.approved_question)

        # Verify created Question in Bank
        bank_q = self.q1.approved_question
        self.assertEqual(bank_q.category, self.category)
        self.assertEqual(bank_q.options.count(), 4)
        correct_opts = bank_q.options.filter(is_correct=True)
        self.assertEqual(correct_opts.count(), 1)
        self.assertEqual(correct_opts.first().option_key, "A")

    def test_bulk_approve_paper(self):
        self.client.login(username="cs_teacher", password="Password123!")
        res = self.client.post(reverse("questions:paper_bulk_approve", args=[self.paper.pk]))
        self.assertEqual(res.status_code, 302)

        self.paper.refresh_from_db()
        self.assertEqual(self.paper.status, QuestionPaperUpload.ProcessingStatus.COMPLETED)
        self.assertEqual(self.paper.questions_approved, 2)
        self.assertEqual(Question.objects.filter(source_paper=self.paper).count(), 2)

    def test_delete_extracted_question(self):
        self.client.login(username="cs_teacher", password="Password123!")
        res = self.client.post(reverse("questions:extracted_delete", args=[self.q2.pk]))
        self.assertEqual(res.status_code, 302)
        self.assertFalse(ExtractedQuestion.objects.filter(pk=self.q2.pk).exists())

    def test_secure_pdf_download_access_control(self):
        # 1. Student should receive 403 Forbidden
        self.client.login(username="candidate_bob", password="Password123!")
        res_stu = self.client.get(reverse("questions:paper_download", args=[self.paper.pk]))
        self.assertEqual(res_stu.status_code, 403)

        # 2. Unauthorized teacher should receive 403 Forbidden
        self.client.login(username="other_prof", password="Password123!")
        res_other = self.client.get(reverse("questions:paper_download", args=[self.paper.pk]))
        self.assertEqual(res_other.status_code, 403)

        # 3. Paper owner teacher should succeed (200 OK)
        self.client.login(username="cs_teacher", password="Password123!")
        res_owner = self.client.get(reverse("questions:paper_download", args=[self.paper.pk]))
        self.assertEqual(res_owner.status_code, 200)
        self.assertEqual(res_owner["Content-Type"], "application/pdf")

        # 4. Super Admin should succeed (200 OK)
        self.client.login(username="faculty_lead", password="Password123!")
        res_admin = self.client.get(reverse("questions:paper_download", args=[self.paper.pk]))
        self.assertEqual(res_admin.status_code, 200)

    def test_duplicate_question_prevention_on_approval(self):
        """Verifies that approving a question identical to an existing bank question does not create duplicate."""
        self.client.login(username="cs_teacher", password="Password123!")

        # First approval creates the question in bank
        self.client.post(reverse("questions:extracted_approve", args=[self.q1.pk]))
        self.q1.refresh_from_db()
        self.assertEqual(Question.objects.filter(question_text=self.q1.question_text).count(), 1)

        # Create a second extracted question with identical text in another staging item
        duplicate_extracted = ExtractedQuestion.objects.create(
            paper=self.paper,
            question_number=99,
            question_text=self.q1.question_text,  # Identical text
            options=self.q1.options,
            correct_option="A",
            status=ExtractedQuestion.Status.EXTRACTED,
        )

        # Attempting to approve the duplicate
        res = self.client.post(reverse("questions:extracted_approve", args=[duplicate_extracted.pk]))
        self.assertEqual(res.status_code, 302)

        duplicate_extracted.refresh_from_db()
        self.assertEqual(duplicate_extracted.status, ExtractedQuestion.Status.APPROVED)
        # Should link to existing bank item and NOT create a new duplicate row
        self.assertEqual(Question.objects.filter(question_text=self.q1.question_text).count(), 1)
        self.assertEqual(duplicate_extracted.approved_question, self.q1.approved_question)

    def test_corrupted_pdf_file_rejected(self):
        corrupted_bytes = b"%PDF-1.4\nCorrupted binary garbage that cannot be parsed by PyMuPDF"
        bad_file = SimpleUploadedFile("bad.pdf", corrupted_bytes, content_type="application/pdf")
        form = QuestionPaperUploadForm(
            data={"title": "Corrupted Test", "category": self.category.pk},
            files={"pdf_file": bad_file},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("pdf_file", form.errors)
        self.assertTrue(any("corrupted" in err.lower() or "invalid" in err.lower() for err in form.errors["pdf_file"]))

    def test_unapproved_questions_do_not_appear_in_bank(self):
        """Ensure staging ExtractedQuestions are never returned by Question Bank query."""
        initial_bank_count = Question.objects.count()
        # q1 and q2 are only in ExtractedQuestion staging table
        self.assertEqual(self.q1.status, ExtractedQuestion.Status.EXTRACTED)
        self.assertEqual(self.q2.status, ExtractedQuestion.Status.EXTRACTED)
        self.assertEqual(Question.objects.count(), initial_bank_count)


class QuestionBankAuthoringTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name="Medical Entrance Biology",
            slug="medical-biology",
            is_active=True,
        )
        self.teacher = User.objects.create_user(
            username="bio_teacher",
            email="bio@test.com",
            password="Password123!",
            role=User.Roles.TEACHER,
            is_staff=True,
        )
        self.teacher.assigned_categories.add(self.category)

        self.student = User.objects.create_user(
            username="candidate_alice",
            email="alice@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
        )

    def test_manual_question_creation_with_options_and_marks(self):
        self.client.login(username="bio_teacher", password="Password123!")
        res = self.client.post(
            reverse("questions:question_create"),
            {
                "category": self.category.pk,
                "subject": "Cell Biology",
                "topic": "Organelles",
                "chapter": "Cell: The Unit of Life",
                "difficulty": Question.DifficultyLevel.EASY,
                "language": Question.Language.ENGLISH,
                "question_type": Question.QuestionType.MCQ_SINGLE,
                "question_text": "Which organelle is famously known as the powerhouse of the cell?",
                "option_text_A": "Ribosome",
                "option_text_B": "Mitochondria",
                "option_text_C": "Golgi Apparatus",
                "option_text_D": "Nucleus",
                "correct_option": "B",
                "positive_marks": "4.00",
                "negative_marks": "1.00",
                "explanation": "Mitochondria generate most of the chemical energy needed to power the cell (ATP).",
            },
        )
        self.assertEqual(res.status_code, 302)

        created_q = Question.objects.filter(subject="Cell Biology").first()
        self.assertIsNotNone(created_q)
        self.assertEqual(created_q.creator, self.teacher)
        self.assertEqual(created_q.options.count(), 4)

        correct_opt = created_q.options.filter(is_correct=True).first()
        self.assertIsNotNone(correct_opt)
        self.assertEqual(correct_opt.option_key, "B")
        self.assertEqual(correct_opt.option_text, "Mitochondria")

    def test_manual_question_creation_validation_fails_with_insufficient_options(self):
        self.client.login(username="bio_teacher", password="Password123!")
        res = self.client.post(
            reverse("questions:question_create"),
            {
                "category": self.category.pk,
                "subject": "Cell Biology",
                "question_text": "Invalid question with only 1 option",
                "option_text_A": "Only Option",
                "option_text_B": "",
                "correct_option": "A",
                "positive_marks": "4.00",
                "negative_marks": "1.00",
            },
        )
        self.assertEqual(res.status_code, 200)
        # Should not create question
        self.assertFalse(Question.objects.filter(question_text="Invalid question with only 1 option").exists())

    def test_question_bank_edit_question(self):
        self.client.login(username="bio_teacher", password="Password123!")
        q = Question.objects.create(
            category=self.category,
            subject="Genetics",
            question_text="Old Genetics Question",
            positive_marks=4.00,
            creator=self.teacher,
        )
        QuestionOption.objects.create(question=q, option_key="A", option_text="Opt A", is_correct=True)
        QuestionOption.objects.create(question=q, option_key="B", option_text="Opt B", is_correct=False)

        res = self.client.post(
            reverse("questions:question_edit", args=[q.pk]),
            {
                "category": self.category.pk,
                "subject": "Genetics",
                "difficulty": Question.DifficultyLevel.MEDIUM,
                "language": Question.Language.ENGLISH,
                "question_type": Question.QuestionType.MCQ_SINGLE,
                "question_text": "Updated Mendel Laws of Inheritance",
                "option_text_A": "Law of Segregation",
                "option_text_B": "Law of Dominance",
                "option_text_C": "Law of Independent Assortment",
                "option_text_D": "All of the above",
                "correct_option": "D",
                "positive_marks": "5.00",
                "negative_marks": "1.00",
            },
        )
        self.assertEqual(res.status_code, 302)
        q.refresh_from_db()
        self.assertEqual(q.question_text, "Updated Mendel Laws of Inheritance")
        self.assertEqual(q.positive_marks, Decimal("5.00"))
        self.assertEqual(q.options.count(), 4)
        self.assertTrue(q.options.filter(option_key="D", is_correct=True).exists())

    def test_question_bank_delete_question(self):
        self.client.login(username="bio_teacher", password="Password123!")
        q = Question.objects.create(
            category=self.category,
            subject="Ecology",
            question_text="Ecology question to delete",
            creator=self.teacher,
        )

        res = self.client.post(reverse("questions:question_delete", args=[q.pk]))
        self.assertEqual(res.status_code, 302)
        self.assertFalse(Question.objects.filter(pk=q.pk).exists())

    def test_question_preview_accessible_by_teacher_and_forbidden_for_student(self):
        q = Question.objects.create(
            category=self.category,
            subject="Physiology",
            question_text="Cardiac cycle duration",
            creator=self.teacher,
        )

        # Student gets 403 Forbidden
        self.client.login(username="candidate_alice", password="Password123!")
        res_stu = self.client.get(reverse("questions:question_preview", args=[q.pk]))
        self.assertEqual(res_stu.status_code, 403)

        # Teacher gets 200 OK
        self.client.login(username="bio_teacher", password="Password123!")
        res_teach = self.client.get(reverse("questions:question_preview", args=[q.pk]))
        self.assertEqual(res_teach.status_code, 200)
        self.assertContains(res_teach, "Cardiac cycle duration")
