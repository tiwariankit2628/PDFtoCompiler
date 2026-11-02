from django.conf import settings
from django.db import models


class QuestionPaperUpload(models.Model):
    """
    Stores and manages original question paper PDFs uploaded by teachers
    and administrators for automatic question compilation and extraction.
    """

    class ProcessingStatus(models.TextChoices):
        PENDING = "pending", "Pending Extraction"
        PROCESSING = "processing", "Processing Extraction"
        EXTRACTED = "extracted", "Extracted & Pending Review"
        COMPLETED = "completed", "Imported to Question Bank"
        FAILED = "failed", "Extraction Failed"

    title = models.CharField("Paper Title", max_length=255)
    category = models.ForeignKey(
        "exams.Category",
        on_delete=models.CASCADE,
        related_name="uploaded_papers",
    )
    subject = models.CharField("Subject / Course", max_length=150, blank=True)
    target_exam = models.CharField("Target Examination", max_length=150, blank=True)
    pdf_file = models.FileField(
        "Original PDF Paper",
        upload_to="question_papers/%Y/%m/",
        help_text="Upload official question paper PDF (maximum 25MB).",
    )
    file_size_bytes = models.PositiveIntegerField("File Size (Bytes)", default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="uploaded_papers",
    )
    status = models.CharField(
        "Extraction Status",
        max_length=25,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.PENDING,
        db_index=True,
    )
    total_pages = models.PositiveIntegerField("Total Pages", default=0)
    questions_found = models.PositiveIntegerField("Questions Extracted", default=0)
    questions_approved = models.PositiveIntegerField("Questions Approved", default=0)
    ocr_used = models.BooleanField("OCR Applied", default=False)
    extraction_log = models.TextField("Extraction Log & Diagnostic Output", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Question Paper Upload"
        verbose_name_plural = "Question Paper Uploads"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"

    @property
    def pending_review_count(self):
        return self.extracted_questions.filter(status=ExtractedQuestion.Status.EXTRACTED).count()

    @property
    def approved_count(self):
        return self.extracted_questions.filter(status=ExtractedQuestion.Status.APPROVED).count()


class Question(models.Model):
    """
    Reusable Question Bank model storing vetted, approved questions.
    """

    class DifficultyLevel(models.TextChoices):
        EASY = "easy", "Easy"
        MEDIUM = "medium", "Medium"
        HARD = "hard", "Hard"

    class Language(models.TextChoices):
        ENGLISH = "en", "English"
        HINDI = "hi", "Hindi"
        BILINGUAL = "bilingual", "Bilingual"

    class QuestionType(models.TextChoices):
        MCQ_SINGLE = "mcq_single", "Multiple Choice (Single Correct)"
        MCQ_MULTIPLE = "mcq_multiple", "Multiple Choice (Multiple Correct)"
        NUMERICAL = "numerical", "Numerical / Integer Value"

    category = models.ForeignKey(
        "exams.Category",
        on_delete=models.CASCADE,
        related_name="questions",
    )
    subject = models.CharField("Subject", max_length=150, db_index=True)
    topic = models.CharField("Topic", max_length=150, blank=True, db_index=True)
    chapter = models.CharField("Chapter", max_length=150, blank=True)
    difficulty = models.CharField(
        "Difficulty Level",
        max_length=20,
        choices=DifficultyLevel.choices,
        default=DifficultyLevel.MEDIUM,
        db_index=True,
    )
    language = models.CharField(
        "Language",
        max_length=20,
        choices=Language.choices,
        default=Language.ENGLISH,
    )
    question_type = models.CharField(
        "Question Type",
        max_length=30,
        choices=QuestionType.choices,
        default=QuestionType.MCQ_SINGLE,
    )
    question_text = models.TextField("Question Text (supports LaTeX & Unicode)")
    image = models.ImageField(
        "Diagram / Equation Image",
        upload_to="question_images/%Y/%m/",
        blank=True,
        null=True,
    )
    explanation = models.TextField("Solution & Step-by-Step Explanation", blank=True)
    positive_marks = models.DecimalField(
        "Positive Marks", max_digits=5, decimal_places=2, default=4.0
    )
    negative_marks = models.DecimalField(
        "Negative Marks", max_digits=5, decimal_places=2, default=1.0
    )
    source_paper = models.ForeignKey(
        QuestionPaperUpload,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bank_questions",
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="authored_questions",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Question Bank Item"
        verbose_name_plural = "Question Bank Items"
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.subject}] {self.question_text[:60]}..."

    @property
    def correct_options_list(self):
        return self.options.filter(is_correct=True)


class QuestionOption(models.Model):
    """
    Multiple-choice option associated with a Question Bank item.
    """

    question = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        related_name="options",
    )
    option_key = models.CharField("Option Key (A, B, C, D)", max_length=10)
    option_text = models.TextField("Option Content")
    image = models.ImageField(
        "Option Diagram / Image",
        upload_to="option_images/%Y/%m/",
        blank=True,
        null=True,
    )
    is_correct = models.BooleanField("Correct Answer", default=False)
    order = models.PositiveIntegerField("Display Order", default=0)

    class Meta:
        verbose_name = "Question Option"
        verbose_name_plural = "Question Options"
        ordering = ["order", "option_key"]

    def __str__(self):
        check = "✓" if self.is_correct else ""
        return f"{self.option_key}: {self.option_text[:40]} {check}"


class ExtractedQuestion(models.Model):
    """
    Staging / Review workspace item generated by PDF Question Compiler.
    Allows manual review, text & equation corrections, option editing,
    and manual answer key entry before pushing into permanent Question Bank.
    """

    class Status(models.TextChoices):
        EXTRACTED = "extracted", "Extracted (Pending Review)"
        REVIEWED = "reviewed", "Reviewed / Edited"
        APPROVED = "approved", "Approved to Question Bank"
        REJECTED = "rejected", "Rejected / Discarded"

    paper = models.ForeignKey(
        QuestionPaperUpload,
        on_delete=models.CASCADE,
        related_name="extracted_questions",
    )
    question_number = models.PositiveIntegerField("Question #", default=1)
    question_text = models.TextField("Question Text")
    # Structured options: list of dicts: [{"key": "A", "text": "Option text"}, ...]
    options = models.JSONField("MCQ Options", default=list)
    correct_option = models.CharField(
        "Answer Key",
        max_length=10,
        blank=True,
        help_text="Key of correct option, e.g. 'A', 'B', 'C', 'D'",
    )
    explanation = models.TextField("Explanation / Notes", blank=True)
    positive_marks = models.DecimalField(
        "Positive Marks", max_digits=5, decimal_places=2, default=4.0
    )
    negative_marks = models.DecimalField(
        "Negative Marks", max_digits=5, decimal_places=2, default=1.0
    )
    image = models.ImageField(
        "Extracted Diagram / Formula",
        upload_to="extracted_images/%Y/%m/",
        blank=True,
        null=True,
    )
    page_number = models.PositiveIntegerField("Source Page", default=1)
    status = models.CharField(
        "Review Status",
        max_length=20,
        choices=Status.choices,
        default=Status.EXTRACTED,
        db_index=True,
    )
    is_flagged = models.BooleanField("Flagged Issue", default=False)
    flag_reason = models.CharField("Flag Reason", max_length=255, blank=True)
    approved_question = models.ForeignKey(
        Question,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_extracted_item",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Extracted Question (Staging)"
        verbose_name_plural = "Extracted Questions (Staging)"
        ordering = ["question_number", "id"]

    def __str__(self):
        return f"Q{self.question_number} (Page {self.page_number}) - {self.status}"
