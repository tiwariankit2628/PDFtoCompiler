"""Validation for the student-owned, local-only practice workflow."""
import re
from collections import Counter
from decimal import Decimal

import pymupdf as fitz
from django import forms
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator

from .forms import BootstrapFormMixin
from .models import QuestionPaperUpload
from .structured_forms import add_tables_editor, clean_tables_editor


class PracticeUploadForm(BootstrapFormMixin, forms.Form):
    title = forms.CharField(max_length=255, label="Practice test title")
    subject = forms.CharField(max_length=150, required=False)
    pdf_file = forms.FileField(label="Question paper PDF", widget=forms.ClearableFileInput(attrs={"accept": ".pdf,application/pdf"}))
    extraction_mode = forms.ChoiceField(
        label="Extraction method", choices=QuestionPaperUpload.ExtractionMode.choices,
        initial=QuestionPaperUpload.ExtractionMode.LOCAL, required=False,
    )
    cloud_consent = forms.BooleanField(
        required=False,
        label="I consent to sending this PDF to Mathpix and its pages/OCR to Google Gemini, and accept the associated provider terms and charges.",
    )

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
        mode = cleaned.get("extraction_mode") or QuestionPaperUpload.ExtractionMode.LOCAL
        cleaned["extraction_mode"] = mode
        if mode == QuestionPaperUpload.ExtractionMode.CLOUD:
            if not cleaned.get("cloud_consent"):
                self.add_error("cloud_consent", "Explicit consent is required before sending a PDF to external providers.")
            from .cloud_providers import cloud_is_configured
            if not cloud_is_configured():
                self.add_error("extraction_mode", "Cloud extraction is not configured. Choose local extraction or ask your administrator.")
            if getattr(self, "pdf_page_count", 0) > settings.PDF_CLOUD_MAX_PAGES:
                self.add_error("pdf_file", f"Cloud extraction is limited to {settings.PDF_CLOUD_MAX_PAGES} pages.")
        return cleaned


def practice_question_error(question):
    """Return a human-readable error instead of silently dropping a question."""
    figures = list(question.figures.all())
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
        if not str(opt.get("text", "")).strip() and not any(f.option_key == key and f.private_image for f in figures):
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
        label="Answer key (optional)", required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "1: A, 2: B, 3: D"}),
        help_text="Enter only keys you have personally checked. Questions without a confirmed key stay ungraded.",
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
        if not text:
            self.answer_keys = {}
            return text
        pattern = re.compile(r"(\d+)\s*[:.\-=]?\s*([A-Ea-e1-5])")
        matches = list(pattern.finditer(text))
        remainder = pattern.sub("", text)
        if not matches or remainder.strip(" ,;\n\r\t"):
            raise ValidationError("Use pairs like 1: A, 2: B, separated by commas or new lines.")
        counts = Counter(q.question_number for q in self.questions)
        by_number = {q.question_number: q for q in self.questions}
        keys = {}
        for match in matches:
            number = int(match.group(1))
            key = match.group(2).upper()
            key = {"1": "A", "2": "B", "3": "C", "4": "D", "5": "E"}.get(key, key)
            if number in keys:
                raise ValidationError(f"Question {number} appears more than once in the answer key.")
            if number not in by_number or counts[number] != 1:
                raise ValidationError(f"Question {number} is missing, excluded, or ambiguous. Edit it individually instead.")
            options = by_number[number].options or []
            if key not in [str(o.get("key", "")).upper() for o in options if isinstance(o, dict)]:
                raise ValidationError(f"Question {number} has no option {key}.")
            keys[number] = key
        self.answer_keys = keys
        return text

    def clean(self):
        cleaned = super().clean()
        if not self.questions:
            raise ValidationError("Include at least one question before creating a test.")
        invalid = [q.question_number for q in self.questions if practice_question_error(q)]
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
    question_text = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))
    option_A = forms.CharField(label="Option A", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    option_B = forms.CharField(label="Option B", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    option_C = forms.CharField(label="Option C", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    option_D = forms.CharField(label="Option D", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    option_E = forms.CharField(label="Option E", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    correct_option = forms.ChoiceField(
        required=False, choices=[("", "No answer key — ungraded")] + [(k, k) for k in "ABCDE"],
        label="Correct option (optional)",
    )
    positive_marks = forms.DecimalField(min_value=0, max_value=Decimal("999.99"), max_digits=5, decimal_places=2)
    negative_marks = forms.DecimalField(min_value=0, max_value=Decimal("999.99"), max_digits=5, decimal_places=2)
    explanation = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    reviewed = forms.BooleanField(required=False, label="I checked this question against the source PDF.")

    def __init__(self, *args, question, **kwargs):
        self.question = question
        if not args and "data" not in kwargs:
            options = {str(o.get("key", "")).upper(): o.get("text", "") for o in question.options if isinstance(o, dict)}
            kwargs["initial"] = {
                "question_text": question.question_text, "correct_option": question.correct_option,
                "positive_marks": question.positive_marks, "negative_marks": question.negative_marks,
                "explanation": question.explanation, "reviewed": not question.is_flagged,
                **{f"option_{k}": options.get(k, "") for k in "ABCDE"},
            }
        super().__init__(*args, **kwargs)
        add_tables_editor(self, question)

    def clean(self):
        cleaned = clean_tables_editor(self, super().clean())
        figures = list(self.question.figures.all())
        if not cleaned.get("question_text", "").strip() and not self.question.private_image and not any(not f.option_key and f.private_image for f in figures):
            self.add_error("question_text", "Add a question statement or attach a question image.")
        keys = [k for k in "ABCDE" if cleaned.get(f"option_{k}", "").strip() or any(f.option_key == k and f.private_image for f in figures)]
        if len(keys) < 2:
            raise ValidationError("At least two options need text or an attached image.")
        if cleaned.get("correct_option") and cleaned["correct_option"] not in keys:
            self.add_error("correct_option", "Select an existing option, or leave the answer key blank.")
        return cleaned
