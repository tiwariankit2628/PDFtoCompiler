"""Validation for the student-owned, local-only practice workflow."""
from decimal import Decimal

import pymupdf as fitz
from django import forms
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator

from .forms import (
    BootstrapFormMixin,
    clean_local_extraction_mode,
    local_extraction_field,
)
from .answer_key_validation import parse_checked_answer_key
from .models import ExtractedQuestion, QuestionPaperUpload
from .presentation import missing_crop_pages, unavailable_crop_pages
from .structured_forms import add_tables_editor, clean_tables_editor


class PracticeUploadForm(BootstrapFormMixin, forms.Form):
    title = forms.CharField(max_length=255, label="Practice test title")
    subject = forms.CharField(max_length=150, required=False)
    pdf_file = forms.FileField(label="Question paper PDF", widget=forms.ClearableFileInput(attrs={"accept": ".pdf,application/pdf"}))
    image_first = forms.BooleanField(
        required=False, initial=True, label="Image-first (take the test on the original PDF image)",
        help_text="Recommended. Each question shows the original PDF crop with A/B/C/D buttons below — no retyping.",
    )
    extraction_mode = local_extraction_field()

    def clean_extraction_mode(self):
        return clean_local_extraction_mode(self.cleaned_data.get("extraction_mode"))

    def clean_pdf_file(self):
        upload = self.cleaned_data["pdf_file"]
        if not upload.name.lower().endswith(".pdf"):
            raise ValidationError("Choose a PDF file.")
        if upload.size > 25 * 1024 * 1024:
            raise ValidationError("The PDF must be 25 MB or smaller.")
        try:
            data = upload.read()
            with fitz.open(stream=data, filetype="pdf") as document:
                if document.needs_pass or not len(document):
                    raise ValidationError("The PDF must contain pages and must not be password protected.")
                if len(document) > settings.PRACTICE_MAX_PDF_PAGES:
                    raise ValidationError(f"The PDF exceeds the {settings.PRACTICE_MAX_PDF_PAGES}-page limit.")
                self.pdf_page_count = len(document)
        except (fitz.FileDataError, fitz.EmptyFileError, RuntimeError, ValueError):
            raise ValidationError("The PDF is invalid or damaged.") from None
        finally:
            upload.seek(0)
        return upload

    def clean(self):
        cleaned = super().clean()
        # Extraction is local-only here (enforced by clean_extraction_mode).
        cleaned["extraction_mode"] = cleaned.get("extraction_mode") or QuestionPaperUpload.ExtractionMode.LOCAL
        return cleaned


def practice_question_error(question, *, allow_confirmed_crop_choices=False):
    """Return a human-readable error instead of silently dropping a question.

    Reviewers may explicitly confirm labels visible in a whole-question crop.
    Never infer labels merely because a crop exists; the private original must
    be available and each crop-only label must be deliberately selected.
    """
    figures = list(question.figures.all())
    crops = [f for f in figures if f.source == "question_crop" and not f.option_key and f.private_image]
    has_crop = (allow_confirmed_crop_choices and question.paper.image_first and question.is_image_first
                and bool(crops) and not unavailable_crop_pages(crops, private=True)
                and not missing_crop_pages(question, crops))
    if not question.question_text.strip() and not question.private_image and not any(not f.option_key and f.private_image for f in figures):
        return "Add a question statement or question image."
    options = question.options if isinstance(question.options, list) else []
    keys = []
    for opt in options:
        if not isinstance(opt, dict):
            return "An option is invalid; edit this question."
        key = str(opt.get("key", "")).upper().strip()
        if key not in "ABCDE" or len(key) != 1 or key in keys:
            return "Options must have unique labels A–E."
        if (not str(opt.get("text", "")).strip()
                and not any(f.option_key == key and f.private_image for f in figures)
                and not (has_crop and opt.get("visible_in_crop") is True)):
            if allow_confirmed_crop_choices:
                return f"Option {key} needs text, an image or a checked label in the original crop."
            return f"Option {key} needs text or an image."
        keys.append(key)
    if len(keys) < 2:
        return "At least two non-empty options are required."
    if question.correct_option and question.correct_option.upper() not in keys:
        return "The answer key must match an existing option."
    if question.positive_marks < 0 or question.negative_marks < 0:
        return "Marks cannot be negative."
    return ""


class PracticeSettingsForm(BootstrapFormMixin, forms.Form):
    duration_minutes = forms.IntegerField(label="Test duration (minutes)", required=True)
    passing_percentage = forms.DecimalField(
        label="Practice passing target (%)", min_value=0, max_value=100,
        max_digits=5, decimal_places=2, required=False, initial=40,
        help_text="Optional. Pass/fail is shown only when every question has a confirmed key; blank or 0 disables it.",
    )
    answer_key_text = forms.CharField(
        label="Type or paste answer key (optional)", required=False, max_length=30000,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "1: A, 2: B, 3: D"}),
        help_text="Enter only answers you have checked. Questions without a confirmed key stay ungraded.",
    )
    confirm_answer_key = forms.BooleanField(
        required=False, label="I checked the entered answers against the correct paper, set and printed choice order."
    )
    acknowledge_review = forms.BooleanField(
        required=False, label="I understand extracted text, options and figures can be inaccurate and need review."
    )
    acknowledge_unassigned = forms.BooleanField(
        required=False, label="I understand unassigned image candidates will not appear in this test."
    )

    def __init__(self, *args, questions, unassigned_count=0, **kwargs):
        self.questions = list(questions)
        self.unassigned_count = unassigned_count
        super().__init__(*args, **kwargs)
        minimum = settings.PRACTICE_MIN_DURATION_MINUTES
        maximum = settings.PRACTICE_MAX_DURATION_MINUTES
        if not (1 <= minimum <= maximum <= 10080):
            raise ImproperlyConfigured("Practice duration bounds must satisfy 1 <= min <= max <= 10080 minutes.")
        self.fields["duration_minutes"].min_value = minimum
        self.fields["duration_minutes"].max_value = maximum
        self.fields["duration_minutes"].validators.extend([MinValueValidator(minimum), MaxValueValidator(maximum)])
        self.fields["duration_minutes"].widget.attrs.update({"min": minimum, "max": maximum, "step": 1, "inputmode": "numeric"})
        self.fields["duration_minutes"].help_text = f"Required: {minimum}–{maximum} whole minutes."

    def clean_answer_key_text(self):
        text = self.cleaned_data.get("answer_key_text", "").strip()
        self.answer_keys = parse_checked_answer_key(text, self.questions)
        return text

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("answer_key_text") and not cleaned.get("confirm_answer_key"):
            self.add_error("confirm_answer_key", "Confirm that you checked the entered answer key, or leave it blank.")
        if not self.questions:
            raise ValidationError("Include at least one question before creating a test.")
        unsupported = [q.question_number for q in self.questions if q.detected_type != ExtractedQuestion.DetectedType.SINGLE]
        if unsupported:
            raise ValidationError("Only single-correct MCQs can be scored. Review the type or exclude: " + ", ".join(map(str, unsupported[:20])))
        invalid = [q.question_number for q in self.questions if practice_question_error(q, allow_confirmed_crop_choices=True)]
        if invalid:
            raise ValidationError("Edit or exclude invalid questions before creating the test: " + ", ".join(map(str, invalid[:20])) + ("…" if len(invalid) > 20 else ""))
        total = sum((q.positive_marks for q in self.questions), Decimal("0.00"))
        if total > Decimal("9999.99"):
            raise ValidationError("Total marks exceed 9999.99. Reduce per-question marks or exclude questions.")
        total_negative = sum((q.negative_marks for q in self.questions), Decimal("0.00"))
        if total_negative > Decimal("9999.99"):
            raise ValidationError("Total possible penalties exceed 9999.99. Reduce negative marks or exclude questions.")
        if any(q.is_flagged for q in self.questions) and not cleaned.get("acknowledge_review"):
            self.add_error("acknowledge_review", "Please acknowledge that flagged extraction needs review.")
        if self.unassigned_count and not cleaned.get("acknowledge_unassigned"):
            self.add_error("acknowledge_unassigned", "Review or acknowledge unassigned images before continuing.")
        return cleaned


class PracticeQuestionForm(BootstrapFormMixin, forms.Form):
    option_error_message = "At least two options need text, an attached image or checked labels in the original crop."
    detected_type = forms.ChoiceField(
        required=False, choices=ExtractedQuestion.DetectedType.choices,
        label="Question type (check against the PDF)",
        help_text="Scoring supports one correct choice only. Review or exclude other types.",
    )
    question_text = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))
    option_A = forms.CharField(label="Option A", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    option_B = forms.CharField(label="Option B", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    option_C = forms.CharField(label="Option C", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    option_D = forms.CharField(label="Option D", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    option_E = forms.CharField(label="Option E", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    visible_crop_choices = forms.MultipleChoiceField(
        required=False, choices=[(key, key) for key in "ABCDE"], widget=forms.CheckboxSelectMultiple,
        label="Choice labels visible in the original question image",
        help_text="For image-only choices, check only labels you can see in the crop. No text or answer will be guessed.",
    )
    correct_option = forms.ChoiceField(
        required=False, choices=[("", "No answer key — ungraded")] + [(k, k) for k in "ABCDE"],
        label="Correct option (optional)",
    )
    confirm_answer_key = forms.BooleanField(required=False,
        label="I checked this answer against the correct paper, set and printed choice order.",
        help_text="New answers or changes to the question, type, number or choices require confirmation. Unchanged confirmed answers stay confirmed.")
    positive_marks = forms.DecimalField(min_value=0, max_value=Decimal("999.99"), max_digits=5, decimal_places=2)
    negative_marks = forms.DecimalField(min_value=0, max_value=Decimal("999.99"), max_digits=5, decimal_places=2)
    explanation = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    reviewed = forms.BooleanField(required=False, label="I checked this question against the source PDF.")

    def __init__(self, *args, question, **kwargs):
        self.question = question
        if not args and "data" not in kwargs:
            options = {str(o.get("key", "")).upper(): o.get("text", "") for o in question.options if isinstance(o, dict)}
            kwargs["initial"] = {
                "detected_type": question.detected_type,
                "question_text": question.question_text, "correct_option": question.correct_option,
                "positive_marks": question.positive_marks, "negative_marks": question.negative_marks,
                "explanation": question.explanation, "reviewed": not question.is_flagged,
                "visible_crop_choices": [o.get("key") for o in question.options
                                         if isinstance(o, dict) and o.get("visible_in_crop") is True],
                **{f"option_{k}": options.get(k, "") for k in "ABCDE"},
            }
        super().__init__(*args, **kwargs)
        self.fields["visible_crop_choices"].widget.attrs.pop("class", None)
        add_tables_editor(self, question)

    def available_option_keys(self, cleaned, figures):
        keys = {k for k in "ABCDE" if cleaned.get(f"option_{k}", "").strip()
                or any(f.option_key == k and f.private_image for f in figures)}
        selected = set(cleaned.get("visible_crop_choices") or [])
        has_crop = (self.question.paper.image_first and self.question.is_image_first
                    and any(f.source == "question_crop" and not f.option_key and f.private_image for f in figures))
        if selected and not has_crop:
            self.add_error("visible_crop_choices", "Add an original question crop in image-first mode before checking labels.")
        elif has_crop:
            keys.update(selected)
        return sorted(keys)

    def clean(self):
        cleaned = clean_tables_editor(self, super().clean())
        figures = list(self.question.figures.all())
        if not cleaned.get("question_text", "").strip() and not self.question.private_image and not any(not f.option_key and f.private_image for f in figures):
            self.add_error("question_text", "Add a question statement or attach a question image.")
        keys = self.available_option_keys(cleaned, figures)
        if len(keys) < 2:
            raise ValidationError(self.option_error_message)
        if cleaned.get("correct_option") and cleaned["correct_option"] not in keys:
            self.add_error("correct_option", "Select an existing option, or leave the answer key blank.")
        question_type = cleaned.get("detected_type") or self.question.detected_type
        cleaned["detected_type"] = question_type
        if question_type != ExtractedQuestion.DetectedType.SINGLE and cleaned.get("correct_option"):
            self.add_error("correct_option", "A single-choice key cannot grade this question type. Leave it blank or review the type.")
        return cleaned
