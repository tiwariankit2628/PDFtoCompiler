from django import forms
from django.utils.text import slugify
from .models import Category


class BootstrapFormMixin:
    """Applies Bootstrap 5 classes to form widgets."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.update({"class": "form-check-input"})
            elif isinstance(field.widget, forms.FileInput):
                field.widget.attrs.update({"class": "form-control"})
            else:
                existing = field.widget.attrs.get("class", "")
                field.widget.attrs["class"] = f"form-control {existing}".strip()


class CategoryForm(BootstrapFormMixin, forms.ModelForm):
    """
    Form for creating and editing dynamic examination categories.
    """

    class Meta:
        model = Category
        fields = [
            "name",
            "slug",
            "description",
            "icon",
            "image",
            "display_order",
            "is_active",
            "seo_title",
            "seo_description",
        ]
        widgets = {
            "description": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Enter detailed category description..."}
            ),
            "seo_description": forms.Textarea(
                attrs={"rows": 2, "placeholder": "Meta description for SEO..."}
            ),
            "icon": forms.TextInput(
                attrs={"placeholder": "e.g. bi-trophy, bi-cpu, bi-mortarboard"}
            ),
        }

    def clean_slug(self):
        slug = self.cleaned_data.get("slug")
        if not slug:
            slug = slugify(self.cleaned_data.get("name", ""))
        else:
            slug = slugify(slug)

        qs = Category.objects.filter(slug=slug)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)

        if qs.exists():
            raise forms.ValidationError("A category with this URL slug already exists.")

        return slug


class ExamForm(BootstrapFormMixin, forms.ModelForm):
    """
    Form for creating and editing Computer-Based Tests with extensive
    configuration for duration, scoring, shuffling, scheduling, and access rules.
    """

    class Meta:
        from .models import Exam

        model = Exam
        fields = [
            "title",
            "category",
            "subject",
            "topics",
            "target_exam",
            "description",
            "instructions",
            "duration_minutes",
            "total_marks",
            "passing_percentage",
            "default_positive_marks",
            "default_negative_marks",
            "is_free",
            "shuffle_questions",
            "shuffle_options",
            "max_attempts",
            "show_result_immediately",
            "allow_review_answers",
            "start_time",
            "end_time",
            "status",
        ]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "e.g. JEE Advanced Full Mock Test 1 (Physics & Math)"}),
            "subject": forms.TextInput(attrs={"placeholder": "e.g. Engineering Physics, Quantitative Aptitude"}),
            "topics": forms.TextInput(attrs={"placeholder": "e.g. Mechanics, Electromagnetism, Optics"}),
            "target_exam": forms.TextInput(attrs={"placeholder": "e.g. JEE Advanced 2026, NEET UG, UPSC CSE"}),
            "description": forms.Textarea(attrs={"rows": 3, "placeholder": "Overview and syllabus coverage of this test..."}),
            "instructions": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "Candidate instructions e.g.:\n- Each question carries +4 marks.\n- 1 mark deducted for incorrect answers.\n- Calculator is strictly prohibited.",
                }
            ),
            "start_time": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "end_time": forms.DateTimeInput(attrs={"type": "datetime-local"}),
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

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("start_time")
        end = cleaned_data.get("end_time")

        if start and end and end <= start:
            raise forms.ValidationError("Examination End Window must be later than Start Window.")

        return cleaned_data


class ExamRandomQuestionForm(BootstrapFormMixin, forms.Form):
    """
    Form for automated random question sampling from the Question Bank into an Exam.
    """

    count = forms.IntegerField(
        label="Number of Questions",
        min_value=1,
        max_value=200,
        initial=10,
        widget=forms.NumberInput(attrs={"placeholder": "e.g. 10"}),
    )
    subject = forms.CharField(
        label="Subject Filter (Optional)",
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Physics"}),
    )
    topic = forms.CharField(
        label="Topic Filter (Optional)",
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Mechanics"}),
    )
    difficulty = forms.ChoiceField(
        label="Difficulty Level",
        choices=[("", "Any Difficulty"), ("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")],
        required=False,
        widget=forms.Select(),
    )
    section = forms.CharField(
        label="Target Section Name",
        initial="Section A",
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Section A"}),
    )
    positive_marks = forms.DecimalField(
        label="Positive Marks",
        initial=4.00,
        max_digits=5,
        decimal_places=2,
    )
    negative_marks = forms.DecimalField(
        label="Negative Penalty (-)",
        initial=1.00,
        max_digits=5,
        decimal_places=2,
    )

