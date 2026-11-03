from django.conf import settings
from django.db import models
from .private_storage import private_practice_storage


class AnswerStatus(models.TextChoices):
    NEEDS_REVIEW = "needs_review", "Needs review / no confirmed key"
    STUDENT_CONFIRMED = "student_confirmed", "Student confirmed"
    MANUALLY_VERIFIED = "manually_verified", "Manually verified"


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

    class ExtractionMode(models.TextChoices):
        LOCAL = "local", "Local PyMuPDF / Tesseract"
        CLOUD = "cloud", "Mathpix + Gemini"

    # Private student material is stored separately from the teacher media tree.
    is_student_private = models.BooleanField(default=False, db_index=True)
    practice_exam = models.OneToOneField(
        "exams.Exam", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="private_source_paper",
    )
    private_pdf_file = models.FileField(
        upload_to="papers/%Y/%m/", storage=private_practice_storage, blank=True
    )
    title = models.CharField("Paper Title", max_length=255)
    extraction_mode = models.CharField(
        max_length=12, choices=ExtractionMode.choices, default=ExtractionMode.LOCAL
    )
    cloud_consent_at = models.DateTimeField(null=True, blank=True)
    mathpix_document_id = models.CharField(max_length=160, blank=True, help_text="Provider reference for status/deletion; not an API credential.")
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

    is_private_practice = models.BooleanField(default=False, db_index=True)
    # Student bank edits use copy-on-write. Archived versions remain attached to
    # existing exams and preserve the text, options and answers of past attempts.
    is_bank_archived = models.BooleanField(default=False, db_index=True)
    # Private keys are explicitly entered/confirmed by the student, never guessed.
    key_verified = models.BooleanField(default=False)
    answer_status = models.CharField(max_length=24, choices=AnswerStatus.choices, default=AnswerStatus.NEEDS_REVIEW)
    private_image = models.ImageField(
        upload_to="question_images/%Y/%m/", storage=private_practice_storage, blank=True
    )
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
    tables = models.JSONField(default=list, blank=True, help_text="Structured tables referenced by [[table:1]], [[table:2]], etc.")
    source_regions = models.JSONField(default=list, blank=True, help_text="Original PDF page and normalized question-region coordinates.")
    primary_image_region = models.JSONField(default=dict, blank=True, help_text="Source page and coordinates for the primary original-PDF image.")
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
    tables = models.JSONField(default=list, blank=True, help_text="Structured tables referenced by [[table:1]], [[table:2]], etc.")
    source_regions = models.JSONField(default=list, blank=True, help_text="Original PDF page and normalized question-region coordinates.")
    primary_image_region = models.JSONField(default=dict, blank=True, help_text="Source page and coordinates for the primary original-PDF image.")
    # Structured options: list of dicts: [{"key": "A", "text": "Option text"}, ...]
    options = models.JSONField("MCQ Options", default=list)
    answer_status = models.CharField(max_length=24, choices=AnswerStatus.choices, default=AnswerStatus.NEEDS_REVIEW)
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
    private_image = models.ImageField(
        upload_to="extracted_images/%Y/%m/", storage=private_practice_storage, blank=True
    )
    include_in_practice = models.BooleanField(default=True)
    page_number = models.PositiveIntegerField("Source Page", default=1)
    source_pages = models.JSONField(default=list, blank=True, help_text="Pages containing this question, including continuations.")
    extraction_issues = models.JSONField(default=list, blank=True, help_text="Original extraction diagnostics for review.")
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


class ExtractedFigure(models.Model):
    """An original-PDF crop in the staging workspace, assigned or awaiting review."""

    paper = models.ForeignKey(QuestionPaperUpload, on_delete=models.CASCADE, related_name="figures")
    question = models.ForeignKey(
        ExtractedQuestion, on_delete=models.SET_NULL, related_name="figures", null=True, blank=True
    )
    option_key = models.CharField(max_length=10, blank=True, default="")
    image = models.ImageField(upload_to="extracted_figures/%Y/%m/", blank=True)
    private_image = models.ImageField(
        upload_to="figures/%Y/%m/", storage=private_practice_storage, blank=True
    )
    page_number = models.PositiveIntegerField(default=1)
    # Source rectangle in normalized page coordinates [x0, y0, x1, y1].
    bounding_box = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=20, default="manual")
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["page_number", "order", "id"]


class QuestionFigure(models.Model):
    """Additional bank image; an optional option links it to that choice."""

    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="figures")
    option = models.ForeignKey(
        QuestionOption, on_delete=models.CASCADE, related_name="figures", null=True, blank=True
    )
    image = models.ImageField(upload_to="question_figures/%Y/%m/", blank=True)
    private_image = models.ImageField(
        upload_to="figures/%Y/%m/", storage=private_practice_storage, blank=True
    )
    order = models.PositiveIntegerField(default=0)
    page_number = models.PositiveIntegerField(default=0)
    bounding_box = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["order", "id"]


class AIAnswerSuggestion(models.Model):
    """Untrusted model output, separate from a human-confirmed grading key."""
    class Status(models.TextChoices):
        PROCESSING = "processing", "Processing"
        SUGGESTED = "suggested", "AI suggested"
        ACCEPTED = "accepted", "Confirmed by reviewer"
        REJECTED = "rejected", "Rejected by reviewer"
        FAILED = "failed", "Failed / no suggestion"

    question = models.ForeignKey(ExtractedQuestion, on_delete=models.CASCADE, related_name="ai_suggestions")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROCESSING, db_index=True)
    option_key = models.CharField(max_length=1, blank=True)
    explanation = models.TextField(blank=True)
    uncertainty = models.TextField(blank=True)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    provider = models.CharField(max_length=30, default="Gemini")
    model_id = models.CharField(max_length=100)
    vision_used = models.BooleanField(default=False)
    input_hash = models.CharField(max_length=64, db_index=True)
    provider_calls = models.PositiveSmallIntegerField(default=0, help_text="Model metadata and generation HTTP attempts; failed calls may still be billable.")
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    estimated_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(
            fields=["question", "input_hash", "model_id", "vision_used"],
            condition=models.Q(status__in=["processing", "suggested"]),
            name="unique_active_ai_question_request",
        )]
