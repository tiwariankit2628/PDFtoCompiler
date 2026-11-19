"""Validation for teacher-owned, local-only classroom papers and tests."""
import uuid
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

from .forms import BootstrapFormMixin, QuestionPaperUploadForm
from .models import AnswerStatus, ExtractedQuestion
from .answer_key_validation import confirmed_staged_key
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
        help_text="Choose single-correct for one printed choice, multiple-correct for several choices, or numerical for a number.",
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
        self.order_fields(["question_number", "detected_type", "question_text", "tables_json", "option_A", "option_B", "option_C", "option_D", "option_E", "visible_crop_choices", "correct_option", "typed_key", "multi_scoring_mode", "partial_credit_per_choice", "numeric_tolerance", "confirm_answer_key", "positive_marks", "negative_marks", "explanation", "reviewed"])

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
        help_text="Checked pairs: single 1:1 (option A), multiple 2:[1,3], numerical 3:-2.5. Blank keys stay ungraded.",
    )
    label = forms.CharField(label="Session label (optional)", max_length=120, required=False)
    show_leaderboard_to_students = forms.BooleanField(label="Let participants see the leaderboard", required=False)
    show_results = forms.BooleanField(label="Release answer keys and explanations immediately", required=False,
        help_text="Scores and students' own answers are always visible after submission. Keys require teacher release.")
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
        if sum((q.positive_marks for q in self.questions), Decimal("0")) > Decimal("9999.99"):
            raise ValidationError("Total marks cannot exceed 9999.99.")
        if sum((q.negative_marks for q in self.questions), Decimal("0")) > Decimal("9999.99"):
            raise ValidationError("Total possible penalties cannot exceed 9999.99.")
        if any(q.is_flagged for q in self.questions) and not cleaned.get("acknowledge_review"):
            self.add_error("acknowledge_review", "Review or acknowledge the flagged questions.")
        if self.unassigned_count and not cleaned.get("acknowledge_unassigned"):
            self.add_error("acknowledge_unassigned", "Review or acknowledge the unassigned images.")
        return cleaned


class SeriesCreateForm(ClassroomCreateForm):
    """Admin-only publication from reviewed, local/private source content."""
    position = forms.IntegerField(label="Series test number", min_value=1, max_value=16,
                                  help_text="Test 1 is free; tests 2–16 belong to this track's series pack.")
    acknowledge_images = forms.BooleanField(required=True,
        label="I checked the question text and original images for printed answers or explanations.",
        help_text="Students see original images during the test and their score and own answers immediately afterward. A printed key can be visible before the separate key release.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("creation_token", "label", "show_leaderboard_to_students"):
            self.fields.pop(name)
        self.fields["show_results"].label = "Release answer keys and explanations immediately"
        self.fields["show_results"].help_text = "Otherwise an administrator may release them later; scores and own answers remain immediately visible."
        self.order_fields(["position", "duration_minutes", "answer_key_text", "show_results", "strict_exam_mode",
                           "acknowledge_review", "acknowledge_unassigned", "acknowledge_images", "confirm_review"])

    def clean(self):
        cleaned = super().clean()
        # An assigned series slot is a durable purchased product. Require
        # reviewed keys now rather than selling a permanently ungraded test.
        # A supplied key is deliberately confirmed by the required review box.
        if "answer_key_text" in cleaned:
            supplied = getattr(self, "answer_keys", {})
            missing = [q.question_number for q in self.questions
                       if not (supplied.get(q.question_number) or
                               confirmed_staged_key(q, AnswerStatus.MANUALLY_VERIFIED))]
            if missing:
                raise ValidationError("Confirm a key for each series question first: " + ", ".join(map(str, missing[:20])))
        return cleaned
