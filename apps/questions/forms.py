import json
import pymupdf as fitz
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from apps.exams.models import Category
from .structured_forms import add_tables_editor, clean_tables_editor
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


# --- Local-only extraction selector -----------------------------------------
# The upload screens expose only local PyMuPDF/Tesseract extraction plus a
# disabled "coming later" placeholder. Provider code, configuration, migrations
# and historical records for cloud/MinerU modes are intentionally preserved for
# future use; they are simply not offered or accepted here.
EXTRACTION_PLACEHOLDER = "__more__"
LOCAL_EXTRACTION_CHOICES = [
    (QuestionPaperUpload.ExtractionMode.LOCAL, "Local PyMuPDF / Tesseract"),
    (EXTRACTION_PLACEHOLDER, "More methods — coming later"),
]


class LocalOnlyExtractionSelect(forms.Select):
    """Renders the "coming later" option as visibly disabled and unselectable."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if str(value) == EXTRACTION_PLACEHOLDER:
            option["attrs"]["disabled"] = True
        return option


def local_extraction_field():
    """A ChoiceField offering only local extraction (plus a disabled hint)."""
    return forms.ChoiceField(
        choices=LOCAL_EXTRACTION_CHOICES,
        initial=QuestionPaperUpload.ExtractionMode.LOCAL,
        required=False,
        label="Extraction method",
        widget=LocalOnlyExtractionSelect(),
    )


def clean_local_extraction_mode(value):
    """Server-side guard: omitted/local -> local; anything else is rejected.

    An altered request must never be able to start a disabled method, and an
    unsupported value is reported clearly rather than silently downgraded.
    """
    value = (value or "").strip()
    if value in ("", QuestionPaperUpload.ExtractionMode.LOCAL):
        return QuestionPaperUpload.ExtractionMode.LOCAL
    raise ValidationError(
        "Only local PyMuPDF / Tesseract extraction is available right now. "
        "More methods are coming later."
    )


class QuestionPaperUploadForm(BootstrapFormMixin, forms.ModelForm):
    """
    Form for uploading official PDF question papers with file size,
    MIME type, and PyMuPDF integrity verification.
    """

    MAX_FILE_SIZE_MB = 25
    extraction_mode = local_extraction_field()

    class Meta:
        model = QuestionPaperUpload
        fields = [
            "title",
            "category",
            "subject",
            "target_exam",
            "pdf_file",
            "image_first",
        ]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "e.g. JEE Advanced 2024 Paper 1 (Physics)"}),
            "subject": forms.TextInput(attrs={"placeholder": "e.g. Physics / Mechanics"}),
            "target_exam": forms.TextInput(attrs={"placeholder": "e.g. JEE Advanced, NEET, UPSC"}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        self.fields["image_first"].label = "Image-first (students take the test on the original PDF image)"
        self.fields["image_first"].help_text = (
            "Recommended. Each question is shown as a high-resolution crop of the original PDF "
            "(statement, printed options, formulas, tables and diagrams) with A/B/C/D buttons below."
        )

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
            with fitz.open(stream=file_bytes, filetype="pdf") as doc:
                if len(doc) == 0 or doc.needs_pass:
                    raise ValidationError("The uploaded PDF is empty or password protected.")
                self.pdf_page_count = len(doc)
        except Exception as exc:
            raise ValidationError(f"Invalid or corrupted PDF file: {str(exc)}")

        return uploaded_file

    def clean_extraction_mode(self):
        return clean_local_extraction_mode(self.cleaned_data.get("extraction_mode"))


class ExtractedQuestionEditForm(BootstrapFormMixin, forms.ModelForm):
    """
    Form for manual verification and editing of an extracted question in the review workspace.
    """

    class Meta:
        model = ExtractedQuestion
        fields = [
            "question_number",
            "question_text",
            "detected_type",
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Advisory field: keep the detected value when the reviewer does not
        # change it, so editing other fields never clears the type.
        self.fields["detected_type"].required = False
        add_tables_editor(self, self.instance)

    def clean_detected_type(self):
        value = self.cleaned_data.get("detected_type")
        return value or getattr(self.instance, "detected_type", None) or ExtractedQuestion.DetectedType.SINGLE

    def clean(self):
        return clean_tables_editor(self, super().clean())

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.tables = self.cleaned_data["tables_data"]
        if commit:
            instance.save()
            self.save_m2m()
        return instance

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
        required=False,
    )
    option_text_B = forms.CharField(
        label="Option B",
        widget=forms.TextInput(attrs={"placeholder": "Option B statement / formula"}),
        required=False,
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
    option_text_E = forms.CharField(label="Option E", required=False,
                                    widget=forms.TextInput(attrs={"placeholder": "Option E statement / formula"}))
    correct_option = forms.ChoiceField(
        label="Correct Answer (optional)",
        choices=[("", "No answer key"), ("A", "Option A"), ("B", "Option B"),
                 ("C", "Option C"), ("D", "Option D"), ("E", "Option E")],
        widget=forms.Select(), required=False,
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

        add_tables_editor(self, self.instance)

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
            if "E" in opts:
                self.fields["option_text_E"].initial = opts["E"].option_text

            correct = self.instance.options.filter(is_correct=True).first()
            if correct:
                self.fields["correct_option"].initial = correct.option_key

    def clean(self):
        cleaned_data = clean_tables_editor(self, super().clean())
        opt_a = cleaned_data.get("option_text_A", "").strip()
        opt_b = cleaned_data.get("option_text_B", "").strip()
        opt_c = cleaned_data.get("option_text_C", "").strip()
        opt_d = cleaned_data.get("option_text_D", "").strip()
        opt_e = cleaned_data.get("option_text_E", "").strip()
        correct = cleaned_data.get("correct_option")

        options_map = {"A": opt_a, "B": opt_b, "C": opt_c, "D": opt_d, "E": opt_e}
        image_keys = set()
        if self.instance and self.instance.pk:
            image_keys = {opt.option_key for opt in self.instance.options.all() if opt.image or opt.figures.exists()}
        valid_opts = [k for k, v in options_map.items() if v or k in image_keys]

        if len(valid_opts) < 2:
            raise ValidationError("An MCQ question must provide at least 2 non-empty options.")

        if correct and correct not in valid_opts:
            raise ValidationError(
                f"Selected correct answer ({correct}) cannot be empty. Please provide text for Option {correct}."
            )

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.tables = self.cleaned_data["tables_data"]
        if commit:
            instance.save()
            self.save_m2m()
        return instance
