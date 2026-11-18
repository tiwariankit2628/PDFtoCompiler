"""Validation for teacher-owned, local-only classroom papers and tests."""
import uuid
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

from .forms import BootstrapFormMixin, QuestionPaperUploadForm
from .models import ExtractedQuestion
from .practice_forms import PracticeQuestionForm, PracticeSettingsForm, practice_question_error


class ClassroomPaperUploadForm(QuestionPaperUploadForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].required = False
        self.fields["category"].help_text = "Optional. A private classroom category is used when left blank."


class ClassroomQuestionForm(PracticeQuestionForm):
    option_error_message = "At least two options need text, an attached image or confirmed crop labels."
    question_number = forms.IntegerField(min_value=1, label="Question number")
    detected_type = forms.ChoiceField(
        choices=ExtractedQuestion.DetectedType.choices,
        label="Question type (check against the PDF)",
        help_text="Classroom scoring currently supports one correct choice only.",
    )

    def __init__(self, *args, question, **kwargs):
        super().__init__(*args, question=question, **kwargs)
        if not self.is_bound:
            self.initial.update(
                question_number=question.question_number, detected_type=question.detected_type,
                visible_crop_choices=[o.get("key") for o in question.options
                                      if isinstance(o, dict) and o.get("visible_in_crop") is True],
            )
        self.fields["visible_crop_choices"].widget.attrs.pop("class", None)
        self.order_fields(["question_number", "detected_type", "question_text", "tables_json", "option_A", "option_B", "option_C", "option_D", "option_E", "visible_crop_choices", "correct_option", "confirm_answer_key", "positive_marks", "negative_marks", "explanation", "reviewed"])

    def clean_question_number(self):
        number = self.cleaned_data["question_number"]
        if self.question.paper.extracted_questions.exclude(pk=self.question.pk).filter(question_number=number).exists():
            raise ValidationError("Another question in this paper already has that number.")
        return number


class ClassroomCreateForm(BootstrapFormMixin, forms.Form):
    creation_token = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    duration_minutes = forms.IntegerField(label="Duration (minutes)", min_value=1, max_value=1440, initial=30,
        help_text="1–1,440 minutes. The timer starts when each student begins and never resets on reconnect.")
    answer_key_text = forms.CharField(
        label="Type or paste answer key (optional)", required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "1: A, 2: B, 3: D"}),
        help_text="Only enter answers you have checked. Blank keys stay ungraded, not wrong.",
    )
    label = forms.CharField(label="Session label (optional)", max_length=120, required=False)
    allow_retakes = forms.BooleanField(label="Allow retakes", required=False,
        help_text="The best completed, fully keyed attempt is ranked; see the report for partial attempts.")
    show_leaderboard_to_students = forms.BooleanField(label="Let participants see the leaderboard", required=False)
    show_results = forms.BooleanField(label="Show students their results and answers immediately after submission", required=False,
        help_text="Leave off if students should not see answers while others are still taking the test.")
    strict_exam_mode = forms.BooleanField(label="Enable the disclosed three-warning page-switch rule", required=False,
        help_text="The fourth reported page switch ends the attempt for review. A reported signal is not proof of cheating.")
    acknowledge_review = forms.BooleanField(required=False, label="I understand flagged questions may contain extraction errors.")
    acknowledge_unassigned = forms.BooleanField(required=False, label="I understand unassigned images will not be included.")
    confirm_review = forms.BooleanField(required=True,
        label="I reviewed the included questions, images, choices and supplied answer keys against the original PDF.")

    def __init__(self, *args, questions, unassigned_count=0, **kwargs):
        self.questions = list(questions)
        self.unassigned_count = unassigned_count
        super().__init__(*args, **kwargs)
        self.fields["duration_minutes"].widget.attrs.update({"min": 1, "max": 1440, "inputmode": "numeric"})

    # The same strict, duplicate-aware parser as private practice, but no student
    # duration settings or student quota apply to a teacher classroom test.
    clean_answer_key_text = PracticeSettingsForm.clean_answer_key_text

    def clean(self):
        cleaned = super().clean()
        if not self.questions:
            raise ValidationError("Include at least one question before creating a test.")
        invalid = [q.question_number for q in self.questions if practice_question_error(q, allow_confirmed_crop_choices=True)]
        if invalid:
            raise ValidationError("Edit or exclude incomplete questions first: " + ", ".join(map(str, invalid[:20])))
        unsupported = [q.question_number for q in self.questions if q.detected_type != ExtractedQuestion.DetectedType.SINGLE]
        if unsupported:
            raise ValidationError("Only single-correct MCQs can be scored. Review the type or exclude: " + ", ".join(map(str, unsupported[:20])))
        if sum((q.positive_marks for q in self.questions), Decimal("0")) > Decimal("9999.99"):
            raise ValidationError("Total marks cannot exceed 9999.99.")
        if sum((q.negative_marks for q in self.questions), Decimal("0")) > Decimal("9999.99"):
            raise ValidationError("Total possible penalties cannot exceed 9999.99.")
        if any(q.is_flagged for q in self.questions) and not cleaned.get("acknowledge_review"):
            self.add_error("acknowledge_review", "Review or acknowledge the flagged questions.")
        if self.unassigned_count and not cleaned.get("acknowledge_unassigned"):
            self.add_error("acknowledge_unassigned", "Review or acknowledge the unassigned images.")
        return cleaned
