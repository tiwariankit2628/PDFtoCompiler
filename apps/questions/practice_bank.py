"""Private, owner-only bank with copy-on-write versions for immutable results."""
import uuid
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from apps.exams.models import Exam, ExamQuestion
from apps.payments.services import FeatureUnavailable, QuotaExceeded, consume, lock_practice_user, require_feature
from apps.core.utils import log_audit
from .forms import BootstrapFormMixin
from .models import ExtractedQuestion, Question, QuestionFigure, QuestionOption
from .practice_views import PrivateStudentMixin, _system_category, active_private_attempt


def bank_items(user):
    return Question.objects.filter(creator=user, is_private_practice=True, is_bank_archived=False)


def _feature_or_redirect(request):
    try:
        require_feature(request.user, "bank")
    except FeatureUnavailable as exc:
        messages.error(request, str(exc))
        return redirect("payments:plans")
    return None


class BankQuestionForm(BootstrapFormMixin, forms.Form):
    question_text = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))
    subject = forms.CharField(max_length=150)
    topic = forms.CharField(max_length=150, required=False)
    chapter = forms.CharField(max_length=150, required=False)
    difficulty = forms.ChoiceField(choices=Question.DifficultyLevel.choices)
    language = forms.ChoiceField(choices=Question.Language.choices)
    option_A = forms.CharField(required=False, label="Option A", widget=forms.Textarea(attrs={"rows": 2}))
    option_B = forms.CharField(required=False, label="Option B", widget=forms.Textarea(attrs={"rows": 2}))
    option_C = forms.CharField(required=False, label="Option C", widget=forms.Textarea(attrs={"rows": 2}))
    option_D = forms.CharField(required=False, label="Option D", widget=forms.Textarea(attrs={"rows": 2}))
    option_E = forms.CharField(required=False, label="Option E", widget=forms.Textarea(attrs={"rows": 2}))
    correct_option = forms.ChoiceField(required=False, choices=[("", "No confirmed key — ungraded")] + [(k, k) for k in "ABCDE"], label="Student-confirmed key")
    positive_marks = forms.DecimalField(min_value=0, max_value=Decimal("999.99"), max_digits=5, decimal_places=2)
    negative_marks = forms.DecimalField(min_value=0, max_value=Decimal("999.99"), max_digits=5, decimal_places=2)
    explanation = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, question, **kwargs):
        self.question = question
        if not args and "data" not in kwargs:
            options = list(question.options.all())
            keys = [o.option_key for o in options if o.is_correct]
            kwargs["initial"] = {
                **{name: getattr(question, name) for name in ("question_text", "subject", "topic", "chapter", "difficulty", "language", "positive_marks", "negative_marks", "explanation")},
                "correct_option": keys[0] if question.key_verified and len(keys) == 1 else "",
                **{f"option_{o.option_key}": o.option_text for o in options if o.option_key in "ABCDE"},
            }
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        figures = list(self.question.figures.select_related("option"))
        if not cleaned.get("question_text", "").strip() and not self.question.private_image and not any(not f.option_id and f.private_image for f in figures):
            self.add_error("question_text", "A question statement or question figure is required.")
        keys = [k for k in "ABCDE" if cleaned.get(f"option_{k}", "").strip()
                or any(f.option_id and f.option.option_key == k and f.private_image for f in figures)]
        if len(keys) < 2:
            raise forms.ValidationError("At least two options need text or an image.")
        if cleaned.get("correct_option") and cleaned["correct_option"] not in keys:
            self.add_error("correct_option", "Choose an existing option or leave the key blank.")
        return cleaned


def clone_for_edit(question):
    """Never modify a question/option referenced by an existing exam/response."""
    if not question.exam_instances.exists():
        return question
    old = question
    values = {field.attname: getattr(old, field.attname) for field in Question._meta.concrete_fields
              if not field.primary_key and field.name not in ("created_at", "updated_at")}
    old.is_bank_archived = True
    old.save(update_fields=["is_bank_archived"])
    values["is_bank_archived"] = False
    new = Question.objects.create(**values)
    option_map = {}
    for opt in old.options.all():
        option_map[opt.pk] = QuestionOption.objects.create(question=new, option_key=opt.option_key,
            option_text=opt.option_text, is_correct=opt.is_correct, order=opt.order, image=opt.image.name)
    for fig in old.figures.all():
        QuestionFigure.objects.create(question=new, option=option_map.get(fig.option_id),
                                      private_image=fig.private_image.name, image=fig.image.name, order=fig.order)
    ExtractedQuestion.objects.filter(approved_question=old, paper__uploaded_by=old.creator,
                                     paper__is_student_private=True).update(approved_question=new)
    return new


def save_bank_form(form, question):
    data = form.cleaned_data
    question = clone_for_edit(question)
    for field in ("question_text", "subject", "topic", "chapter", "difficulty", "language", "positive_marks", "negative_marks", "explanation"):
        setattr(question, field, data[field])
    question.key_verified = bool(data["correct_option"])
    question.save()
    existing = {o.option_key: o for o in question.options.all()}
    figure_keys = set(question.figures.filter(option__isnull=False).values_list("option__option_key", flat=True))
    for index, key in enumerate("ABCDE"):
        text = data[f"option_{key}"].strip()
        opt = existing.get(key)
        if text or key in figure_keys:
            if not opt:
                opt = QuestionOption(question=question, option_key=key)
            opt.option_text = text
            opt.is_correct = key == data["correct_option"]
            opt.order = index
            opt.save()
        elif opt:
            opt.delete()
    return question


class BankListView(PrivateStudentMixin, View):
    sensitive_during_attempt = True

    def get(self, request):
        denied = _feature_or_redirect(request)
        if denied:
            return denied
        base = bank_items(request.user)
        qs = base.prefetch_related("options")
        search = request.GET.get("q", "").strip()[:200]
        if search:
            qs = qs.filter(Q(question_text__icontains=search) | Q(subject__icontains=search) |
                           Q(topic__icontains=search) | Q(chapter__icontains=search))
        filters = {}
        for field in ("subject", "topic", "chapter", "difficulty"):
            value = request.GET.get(field, "").strip()[:150]
            filters[field] = value
            if value:
                qs = qs.filter(**{field: value})
        from .practice_analytics import confirmed_key
        page_obj = Paginator(qs.order_by("-created_at", "-pk"), 100).get_page(request.GET.get("page"))
        for question in page_obj:
            question.confirmed_key = confirmed_key(question, list(question.options.all()))
        return render(request, "questions/practice_bank.html", {
            "page_obj": page_obj,
            "search": search, "filters": filters,
            "subjects": base.exclude(subject="").order_by("subject").values_list("subject", flat=True).distinct(),
            "topics": base.exclude(topic="").order_by("topic").values_list("topic", flat=True).distinct(),
            "chapters": base.exclude(chapter="").order_by("chapter").values_list("chapter", flat=True).distinct(),
            "difficulties": Question.DifficultyLevel.choices,
            "min_duration": settings.PRACTICE_MIN_DURATION_MINUTES, "max_duration": settings.PRACTICE_MAX_DURATION_MINUTES,
        })


class BankEditView(PrivateStudentMixin, View):
    sensitive_during_attempt = True

    def get(self, request, pk):
        denied = _feature_or_redirect(request)
        if denied:
            return denied
        question = get_object_or_404(bank_items(request.user), pk=pk)
        return render(request, "questions/practice_bank_edit.html", {"question": question, "form": BankQuestionForm(question=question)})

    def post(self, request, pk):
        denied = _feature_or_redirect(request)
        if denied:
            return denied
        with transaction.atomic():
            lock_practice_user(request.user)
            if active_private_attempt(request.user):
                messages.error(request, "Finish your active practice test before changing the bank.")
                return redirect("questions:practice_list")
            question = get_object_or_404(bank_items(request.user).select_for_update(), pk=pk)
            form = BankQuestionForm(request.POST, question=question)
            if not form.is_valid():
                return render(request, "questions/practice_bank_edit.html", {"question": question, "form": form})
            updated = save_bank_form(form, question)
        log_audit(request.user, "PRIVATE_BANK_EDITED", f"Private bank question {updated.pk} updated.", request=request)
        messages.success(request, "Question updated. Existing tests and results keep their original version.")
        return redirect("questions:practice_bank_edit", pk=updated.pk)


class BankDeleteView(PrivateStudentMixin, View):
    sensitive_during_attempt = True

    def post(self, request, pk):
        denied = _feature_or_redirect(request)
        if denied:
            return denied
        with transaction.atomic():
            lock_practice_user(request.user)
            if active_private_attempt(request.user):
                return redirect("questions:practice_list")
            question = get_object_or_404(bank_items(request.user).select_for_update(), pk=pk)
            if question.exam_instances.exists():
                question.is_bank_archived = True
                question.save(update_fields=["is_bank_archived"])
            else:
                question.delete()
        log_audit(request.user, "PRIVATE_BANK_REMOVED", f"Private bank question {pk} removed.", request=request)
        messages.success(request, "Removed from your bank. Existing tests and results are preserved.")
        return redirect("questions:practice_bank")


class BankTestForm(BootstrapFormMixin, forms.Form):
    title = forms.CharField(max_length=255)
    duration_minutes = forms.IntegerField(min_value=settings.PRACTICE_MIN_DURATION_MINUTES, max_value=settings.PRACTICE_MAX_DURATION_MINUTES)
    passing_percentage = forms.DecimalField(min_value=0, max_value=100, max_digits=5, decimal_places=2, required=False)


class BankTestCreateView(PrivateStudentMixin, View):
    sensitive_during_attempt = True

    def post(self, request):
        denied = _feature_or_redirect(request)
        if denied:
            return denied
        form = BankTestForm(request.POST)
        raw_ids = request.POST.getlist("question_ids")
        if not form.is_valid() or not 1 <= len(raw_ids) <= 200:
            messages.error(request, "Select 1–200 questions and enter a valid title, duration and optional target.")
            return redirect("questions:practice_bank")
        try:
            ids = [int(value) for value in raw_ids]
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Invalid question selection.")
        if len(ids) != len(set(ids)):
            return HttpResponseBadRequest("Duplicate question selection.")
        try:
            with transaction.atomic():
                lock_practice_user(request.user)
                if active_private_attempt(request.user):
                    messages.error(request, "Finish your active practice test first.")
                    return redirect("questions:practice_list")
                questions = list(bank_items(request.user).select_for_update().filter(pk__in=ids).prefetch_related("options"))
                if len(questions) != len(ids):
                    raise Http404()
                by_id = {q.pk: q for q in questions}
                questions = [by_id[pk] for pk in ids]
                positive = sum((q.positive_marks for q in questions), Decimal("0"))
                negative = sum((q.negative_marks for q in questions), Decimal("0"))
                if positive > Decimal("9999.99") or negative > Decimal("9999.99"):
                    messages.error(request, "Selected marks exceed the supported total of 9999.99.")
                    return redirect("questions:practice_bank")
                consume(request.user, "test_creations")
                exam = Exam.objects.create(is_private_practice=True, slug=f"practice-{uuid.uuid4().hex}",
                    title=form.cleaned_data["title"], category=_system_category(), creator=request.user,
                    subject=questions[0].subject if all(q.subject == questions[0].subject for q in questions) else "Mixed subjects",
                    status=Exam.ExamStatus.PUBLISHED, is_free=True, max_attempts=0,
                    duration_minutes=form.cleaned_data["duration_minutes"], total_marks=positive,
                    passing_percentage=form.cleaned_data["passing_percentage"] or Decimal("0"),
                    shuffle_questions=False, shuffle_options=False, show_result_immediately=True, allow_review_answers=True,
                    description="Private test from your question bank. Only confirmed keys are graded.")
                ExamQuestion.objects.bulk_create([
                    ExamQuestion(exam=exam, question=q, order=i, section=q.subject or "General",
                                 positive_marks=q.positive_marks, negative_marks=q.negative_marks)
                    for i, q in enumerate(questions, 1)
                ])
        except QuotaExceeded as exc:
            messages.error(request, str(exc))
            return redirect("payments:plans")
        log_audit(request.user, "PRIVATE_TEST_CREATED", f"Private bank test {exam.pk} created.", request=request)
        messages.success(request, "Your private bank test is ready.")
        return redirect("exams:exam_instructions", slug=exam.slug)
