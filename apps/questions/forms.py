import json
import pymupdf as fitz
from django import forms
from django.core.exceptions import ValidationError
from apps.exams.models import Category
from .models import ExtractedQuestion, QuestionPaperUpload


class BootstrapFormMixin:
    """Applies Bootstrap 5 classes to form widgets."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.update({"class": "form-check-input"})
            elif isinstance(field.widget, forms.FileInput):
                field.widget.attrs.update({"class": "form-control"})
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.update({"class": "form-select"})
            else:
                existing = field.widget.attrs.get("class", "")
                field.widget.attrs["class"] = f"form-control {existing}".strip()


class QuestionPaperUploadForm(BootstrapFormMixin, forms.ModelForm):
    """
    Form for uploading official PDF question papers with file size,
    MIME type, and PyMuPDF integrity verification.
    """

    MAX_FILE_SIZE_MB = 25

    class Meta:
        model = QuestionPaperUpload
        fields = [
            "title",
            "category",
            "subject",
            "target_exam",
            "pdf_file",
        ]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "e.g. JEE Advanced 2024 Paper 1 (Physics)"}),
            "subject": forms.TextInput(attrs={"placeholder": "e.g. Physics / Mechanics"}),
            "target_exam": forms.TextInput(attrs={"placeholder": "e.g. JEE Advanced, NEET, UPSC"}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        # If user is a teacher, restrict category choices to their permitted categories
        if user and user.is_teacher and not user.is_admin_role:
            assigned = user.assigned_categories.filter(is_active=True)
            if assigned.exists():
                self.fields["category"].queryset = assigned
            else:
                self.fields["category"].queryset = Category.objects.filter(is_active=True)
        else:
            self.fields["category"].queryset = Category.objects.filter(is_active=True)

    def clean_pdf_file(self):
        uploaded_file = self.cleaned_data.get("pdf_file")
        if not uploaded_file:
            raise ValidationError("Please select a PDF question paper file.")

        # 1. Extension check
        if not uploaded_file.name.lower().endswith(".pdf"):
            raise ValidationError("Invalid file format. Only official .pdf files are accepted.")

        # 2. File size check (Max 25MB)
        if uploaded_file.size > self.MAX_FILE_SIZE_MB * 1024 * 1024:
            raise ValidationError(f"File size exceeds limit of {self.MAX_FILE_SIZE_MB}MB.")

        # 3. File integrity check with PyMuPDF
        try:
            file_bytes = uploaded_file.read()
            uploaded_file.seek(0)
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            if len(doc) == 0:
                raise ValidationError("The uploaded PDF has 0 pages or is corrupted.")
        except Exception as exc:
            raise ValidationError(f"Invalid or corrupted PDF file: {str(exc)}")

        return uploaded_file


class ExtractedQuestionEditForm(BootstrapFormMixin, forms.ModelForm):
    """
    Form for manual verification and editing of an extracted question in the review workspace.
    """

    class Meta:
        model = ExtractedQuestion
        fields = [
            "question_number",
            "question_text",
            "correct_option",
            "positive_marks",
            "negative_marks",
            "explanation",
            "image",
            "is_flagged",
            "flag_reason",
        ]
        widgets = {
            "question_text": forms.Textarea(attrs={"rows": 4, "placeholder": "Enter question text..."}),
            "explanation": forms.Textarea(attrs={"rows": 3, "placeholder": "Solution and explanation..."}),
            "correct_option": forms.TextInput(attrs={"placeholder": "e.g. A, B, C, or D"}),
        }

    def clean_correct_option(self):
        opt = self.cleaned_data.get("correct_option", "").strip().upper()
        return opt


class AnswerKeyImportForm(BootstrapFormMixin, forms.Form):
    """
    Allows bulk import of answer keys e.g.:
    1: A, 2: B, 3: D, 4: C
    or 1-A\n2-B\n3-D
    """

    answer_key_text = forms.CharField(
        label="Paste Answer Key",
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "placeholder": "Paste keys e.g.:\n1: A, 2: B, 3: C, 4: D\nor\n1-A\n2-B\n3-C\n4-D",
            }
        ),
        help_text="Automatically maps answers to extracted questions by question number.",
    )


class QuestionBankForm(BootstrapFormMixin, forms.ModelForm):
    """
    Form for manual authoring and editing of Question Bank items with
    integrated MCQ options and answer key assignment.
    """

    option_text_A = forms.CharField(
        label="Option A",
        widget=forms.TextInput(attrs={"placeholder": "Option A statement / formula"}),
        required=True,
    )
    option_text_B = forms.CharField(
        label="Option B",
        widget=forms.TextInput(attrs={"placeholder": "Option B statement / formula"}),
        required=True,
    )
    option_text_C = forms.CharField(
        label="Option C",
        widget=forms.TextInput(attrs={"placeholder": "Option C statement / formula"}),
        required=False,
    )
    option_text_D = forms.CharField(
        label="Option D",
        widget=forms.TextInput(attrs={"placeholder": "Option D statement / formula"}),
        required=False,
    )
    correct_option = forms.ChoiceField(
        label="Correct Answer",
        choices=[("A", "Option A"), ("B", "Option B"), ("C", "Option C"), ("D", "Option D")],
        widget=forms.Select(),
        required=True,
    )

    class Meta:
        from .models import Question

        model = Question
        fields = [
            "category",
            "subject",
            "topic",
            "chapter",
            "difficulty",
            "language",
            "question_type",
            "question_text",
            "image",
            "positive_marks",
            "negative_marks",
            "explanation",
        ]
        widgets = {
            "question_text": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "Enter question statement. Supports LaTeX math e.g. $E = mc^2$ and Unicode Hindi.",
                }
            ),
            "explanation": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "Detailed step-by-step solution / pedagogical explanation...",
                }
            ),
            "subject": forms.TextInput(attrs={"placeholder": "e.g. Physics, Data Structures, Indian Polity"}),
            "topic": forms.TextInput(attrs={"placeholder": "e.g. Thermodynamics, Trees, Fundamental Rights"}),
            "chapter": forms.TextInput(attrs={"placeholder": "e.g. Chapter 3: Laws of Motion"}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        # Scoping categories by user permissions
        if user and user.is_teacher and not user.is_admin_role:
            assigned = user.assigned_categories.filter(is_active=True)
            if assigned.exists():
                self.fields["category"].queryset = assigned
            else:
                self.fields["category"].queryset = Category.objects.filter(is_active=True)
        else:
            self.fields["category"].queryset = Category.objects.filter(is_active=True)

        # If editing an existing instance, populate options & correct answer
        if self.instance and self.instance.pk:
            opts = {opt.option_key: opt for opt in self.instance.options.all()}
            if "A" in opts:
                self.fields["option_text_A"].initial = opts["A"].option_text
            if "B" in opts:
                self.fields["option_text_B"].initial = opts["B"].option_text
            if "C" in opts:
                self.fields["option_text_C"].initial = opts["C"].option_text
            if "D" in opts:
                self.fields["option_text_D"].initial = opts["D"].option_text

            correct = self.instance.options.filter(is_correct=True).first()
            if correct:
                self.fields["correct_option"].initial = correct.option_key

    def clean(self):
        cleaned_data = super().clean()
        opt_a = cleaned_data.get("option_text_A", "").strip()
        opt_b = cleaned_data.get("option_text_B", "").strip()
        opt_c = cleaned_data.get("option_text_C", "").strip()
        opt_d = cleaned_data.get("option_text_D", "").strip()
        correct = cleaned_data.get("correct_option")

        options_map = {"A": opt_a, "B": opt_b, "C": opt_c, "D": opt_d}
        valid_opts = [k for k, v in options_map.items() if v]

        if len(valid_opts) < 2:
            raise ValidationError("An MCQ question must provide at least 2 non-empty options.")

        if correct not in valid_opts:
            raise ValidationError(
                f"Selected correct answer ({correct}) cannot be empty. Please provide text for Option {correct}."
            )

        return cleaned_data
