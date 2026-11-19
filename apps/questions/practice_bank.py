"""Private, owner-only bank with copy-on-write versions for immutable results."""
import uuid
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from apps.exams.models import Exam, ExamQuestion
from apps.core.exam_targets import ExamTarget, targets_for_track
from apps.payments.services import FeatureUnavailable, QuotaExceeded, consume, lock_practice_user, require_feature
from apps.core.utils import log_audit
from .forms import BootstrapFormMixin
from .answer_key_validation import (answer_keys_set, canonical_answer, edited_answer_status,
                                    format_numeric_value)
from .figure_utils import page_image, validate_box
from .figure_views import _png_upload, _safe_id
from .models import AnswerStatus, ExtractedQuestion, Question, QuestionFigure, QuestionOption
from .presentation import missing_crop_pages, primary_question_crops, unavailable_crop_pages
from .private_storage import private_practice_storage
from .structured_forms import add_tables_editor, clean_tables_editor
from .practice_views import PrivateStudentMixin, _open_private_pdf, _system_category, active_private_attempt


def bank_items(user):
    allowed = targets_for_track(user.exam_track)
    return Question.objects.filter(Q(target_type__in=allowed) | Q(target_type=""),
                                   creator=user, is_private_practice=True, is_bank_archived=False)


def _feature_or_redirect(request):
    try:
        require_feature(request.user, "bank")
    except FeatureUnavailable as exc:
        messages.error(request, str(exc))
        return redirect("payments:plans")
    return None


class BankQuestionForm(BootstrapFormMixin, forms.Form):
    target_type = forms.ChoiceField(choices=[("", "Select an exam")] + list(ExamTarget.choices),
        label="Target exam", help_text="Classify this question after checking its original paper.")
    question_type = forms.ChoiceField(required=False, choices=Question.QuestionType.choices,
        label="Question type (check against the original)")
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
    correct_option = forms.ChoiceField(required=False, choices=[("", "No confirmed key — ungraded")] + [(k, k) for k in "ABCDE"], label="Single-correct answer (optional)")
    typed_key = forms.CharField(required=False, max_length=64, label="Multiple or numerical answer (optional)",
        help_text="Use [A,C] or [1,3] for multiple choices, or an actual number such as -2.5 for numerical.")
    multi_scoring_mode = forms.ChoiceField(required=False, choices=[("exact", "Exact set only"),
        ("per_correct", "Partial credit per correct choice")], label="Multiple-correct scoring",
        help_text="A proper subset earns the per-choice amount, up to full marks; any wrong choice gets the negative marks.")
    partial_credit_per_choice = forms.DecimalField(required=False, min_value=0, max_value=Decimal("999.99"), max_digits=5, decimal_places=2)
    numeric_tolerance = forms.DecimalField(required=False, min_value=0, max_digits=18, decimal_places=6,
        help_text="0 means exact equality; otherwise answers within this amount are accepted.")
    confirm_answer_key = forms.BooleanField(required=False,
        label="I checked this answer against the correct paper, set and printed choice order.",
        help_text="Confirm new answers and changes to the question or choices. Unchanged confirmed answers stay confirmed.")
    positive_marks = forms.DecimalField(min_value=0, max_value=Decimal("999.99"), max_digits=5, decimal_places=2)
    negative_marks = forms.DecimalField(min_value=0, max_value=Decimal("999.99"), max_digits=5, decimal_places=2)
    explanation = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, question, **kwargs):
        self.question = question
        options = list(question.options.all())
        keys = sorted(o.option_key for o in options if o.is_correct)
        if question.question_type == Question.QuestionType.MCQ_MULTIPLE:
            self.previous_key = "[" + ",".join(keys) + "]" if keys else ""
        elif question.question_type == Question.QuestionType.NUMERICAL:
            self.previous_key = format_numeric_value(question.numeric_key) if question.numeric_key is not None else ""
        else:
            self.previous_key = keys[0] if len(keys) == 1 else ""
        self.previous_typed_verified = question.typed_key_verified
        self.previous_status = (AnswerStatus.STUDENT_CONFIRMED if question.key_verified and
            (question.question_type == Question.QuestionType.MCQ_SINGLE or question.typed_key_verified)
            else AnswerStatus.NEEDS_REVIEW)
        self.previous_content = (question.question_type, (question.question_text or "").strip(), question.tables or [],
                                 tuple((o.option_key, (o.option_text or "").strip()) for o in options),
                                 question.multi_scoring_mode, question.partial_credit_per_choice, question.numeric_tolerance)
        has_crop = question.is_image_first and question.figures.filter(
            source="question_crop", option__isnull=True).exclude(private_image="").exists()
        # Only preserve labels that were already saved by the reviewer; never
        # infer new choices merely from the existence of a crop.
        self.crop_only_keys = {o.option_key for o in options if has_crop and not o.option_text.strip()}
        if not args and "data" not in kwargs:
            kwargs["initial"] = {
                **{name: getattr(question, name) for name in ("target_type", "question_type", "question_text", "subject", "topic", "chapter", "difficulty", "language", "positive_marks", "negative_marks", "explanation", "multi_scoring_mode", "partial_credit_per_choice", "numeric_tolerance")},
                "correct_option": self.previous_key if question.question_type == Question.QuestionType.MCQ_SINGLE else "",
                "typed_key": self.previous_key if question.question_type != Question.QuestionType.MCQ_SINGLE else "",
                **{f"option_{o.option_key}": o.option_text for o in options if o.option_key in "ABCDE"},
            }
        super().__init__(*args, **kwargs)
        allowed = targets_for_track(question.creator.exam_track)
        self.fields["target_type"].choices = [("", "Select an exam")] + [(value, label) for value, label in ExamTarget.choices if value in allowed]
        add_tables_editor(self, question)

    def clean(self):
        cleaned = clean_tables_editor(self, super().clean())
        figures = list(self.question.figures.select_related("option"))
        if not cleaned.get("question_text", "").strip() and not self.question.private_image and not any(not f.option_id and f.private_image for f in figures):
            self.add_error("question_text", "A question statement or question figure is required.")
        legacy_image_keys = {option.option_key for option in self.question.options.all() if option.image}
        keys = [k for k in "ABCDE" if cleaned.get(f"option_{k}", "").strip() or k in self.crop_only_keys
                or k in legacy_image_keys
                or any(f.option_id and f.option.option_key == k and f.private_image for f in figures)]
        kind = cleaned.get("question_type") or self.question.question_type
        cleaned["question_type"] = kind
        if kind != Question.QuestionType.NUMERICAL and len(keys) < 2:
            raise forms.ValidationError("At least two options need text or an image.")
        if kind == Question.QuestionType.MCQ_SINGLE:
            if cleaned.get("correct_option") and cleaned["correct_option"] not in keys:
                self.add_error("correct_option", "Choose an existing option or leave the key blank.")
            cleaned["answer_key_value"] = cleaned.get("correct_option", "")
        else:
            if cleaned.get("correct_option"):
                self.add_error("correct_option", "Use the multiple or numerical answer field.")
            try:
                cleaned["answer_key_value"] = canonical_answer(cleaned.get("typed_key", ""), kind,
                                                                [{"key": key} for key in keys])
            except forms.ValidationError as exc:
                self.add_error("typed_key", exc)
        cleaned["multi_scoring_mode"] = cleaned.get("multi_scoring_mode") or self.question.multi_scoring_mode
        for field in ("partial_credit_per_choice", "numeric_tolerance"):
            if cleaned.get(field) is None:
                cleaned[field] = getattr(self.question, field)
        if (kind == Question.QuestionType.MCQ_MULTIPLE and cleaned["multi_scoring_mode"] == "per_correct"
                and (cleaned["partial_credit_per_choice"] <= 0 or
                     cleaned.get("positive_marks") is not None and cleaned["partial_credit_per_choice"] > cleaned["positive_marks"])):
            self.add_error("partial_credit_per_choice", "Set a positive partial mark no greater than the full mark.")
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
                                      private_image=fig.private_image.name, image=fig.image.name, order=fig.order,
                                      page_number=fig.page_number, bounding_box=fig.bounding_box, source=fig.source)
    ExtractedQuestion.objects.filter(approved_question=old, paper__uploaded_by=old.creator,
                                     paper__is_student_private=True).update(approved_question=new)
    return new


def save_bank_form(form, question):
    data = form.cleaned_data
    question = clone_for_edit(question)
    for field in ("target_type", "question_type", "question_text", "subject", "topic", "chapter", "difficulty", "language", "positive_marks", "negative_marks", "explanation", "multi_scoring_mode", "partial_credit_per_choice", "numeric_tolerance"):
        setattr(question, field, data[field])
    question.tables = data["tables_data"]
    existing = {o.option_key: o for o in question.options.all()}
    figure_keys = set(question.figures.filter(option__isnull=False).values_list("option__option_key", flat=True))
    preserved_keys = figure_keys | form.crop_only_keys | {key for key, option in existing.items() if option.image}
    choices = tuple((key, data[f"option_{key}"].strip()) for key in "ABCDE"
                    if data[f"option_{key}"].strip() or key in preserved_keys)
    content = (question.question_type, (question.question_text or "").strip(), question.tables or [], choices,
               question.multi_scoring_mode, question.partial_credit_per_choice, question.numeric_tolerance)
    key = data.get("answer_key_value", "")
    changed = content != form.previous_content
    question.answer_status = edited_answer_status(
        form.previous_key, form.previous_status, key, data["confirm_answer_key"],
        AnswerStatus.STUDENT_CONFIRMED, content_changed=changed,
    )
    typed = question.question_type in (Question.QuestionType.MCQ_MULTIPLE, Question.QuestionType.NUMERICAL)
    question.typed_key_verified = bool(typed and key and question.answer_status == AnswerStatus.STUDENT_CONFIRMED
        and (data["confirm_answer_key"] or form.previous_typed_verified and not changed and form.previous_key == key))
    question.key_verified = bool(question.answer_status == AnswerStatus.STUDENT_CONFIRMED and (not typed or question.typed_key_verified))
    question.numeric_key = Decimal(key) if key and question.question_type == Question.QuestionType.NUMERICAL else None
    labels = (set(answer_keys_set(key)) if question.question_type == Question.QuestionType.MCQ_MULTIPLE
              else {key} if question.question_type == Question.QuestionType.MCQ_SINGLE and key else set())
    question.save()
    for index, key in enumerate("ABCDE"):
        text = data[f"option_{key}"].strip()
        opt = existing.get(key)
        if text or key in preserved_keys:
            if not opt:
                opt = QuestionOption(question=question, option_key=key)
            opt.option_text = text
            opt.is_correct = key in labels
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
        qs = base.select_related("source_paper").prefetch_related("options__figures", "figures", "source_extracted_item")
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
            question.primary_crops = primary_question_crops(question, image_first=True, private=True)
            question.image_notice = ""
            if question.is_image_first:
                if not question.primary_crops:
                    question.image_notice = "Original question image is missing. Repair it before using image-only choices."
                else:
                    unavailable = unavailable_crop_pages(question.primary_crops, private=True)
                    missing = missing_crop_pages(question, question.primary_crops)
                    if unavailable:
                        question.image_notice = "An original image file is unavailable on page " + ", ".join(map(str, unavailable)) + "."
                    if missing:
                        question.image_notice += " A possible continuation has no original image on page " + ", ".join(map(str, missing)) + "."
                    question.image_notice = question.image_notice.strip()
            staged = next((item for item in question.source_extracted_item.all()
                           if item.paper_id == question.source_paper_id), None)
            question.source_crop_warning = staged.crop_warning if staged else ""
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
        if form.cleaned_data.get("answer_key_value") and not updated.key_verified:
            messages.warning(request, "Question saved. Its answer remains unconfirmed and will be ungraded until checked.")
        else:
            messages.success(request, "Question updated. Existing tests and results keep their original version.")
        return redirect("questions:practice_bank_edit", pk=updated.pk)


def _private_bank_source(user, question):
    if not question.source_paper_id:
        return None
    return question.source_extracted_item.filter(
        paper_id=question.source_paper_id, paper__uploaded_by=user, paper__is_student_private=True,
    ).select_related("paper").first()


class BankOriginalImageView(PrivateStudentMixin, View):
    """Repair originals in a new bank version without changing past tests."""
    sensitive_during_attempt = True

    def get(self, request, pk):
        denied = _feature_or_redirect(request)
        if denied:
            return denied
        question = get_object_or_404(bank_items(request.user), pk=pk)
        staged = _private_bank_source(request.user, question)
        return render(request, "questions/practice_bank_images.html", {
            "question": question, "staged": staged,
            "crops": primary_question_crops(question, image_first=True, private=True),
            "page_range": range(1, staged.paper.total_pages + 1) if staged else [],
        })

    def post(self, request, pk):
        denied = _feature_or_redirect(request)
        if denied:
            return denied
        action = request.POST.get("action", "")
        saved_file = None
        try:
            with transaction.atomic():
                lock_practice_user(request.user)
                if active_private_attempt(request.user):
                    messages.error(request, "Finish your active practice test before changing images.")
                    return redirect("questions:practice_list")
                original = get_object_or_404(bank_items(request.user).select_for_update(), pk=pk)
                figures = list(original.figures.all())
                figure_id = _safe_id(request.POST.get("replace_id"))
                source_figure = next((f for f in figures if f.pk == figure_id and
                                      f.source == "question_crop" and f.option_id is None), None)
                if figure_id and not source_figure:
                    raise ValueError("Choose an original question image belonging to this question.")
                if action == "remove":
                    if not source_figure:
                        raise ValueError("Choose an original image to remove.")
                    png, page_number, box = None, None, None
                elif action in ("crop", "upload"):
                    staged = _private_bank_source(request.user, original)
                    try:
                        page_number = int(request.POST.get("page_number", ""))
                        if page_number < 1 or (staged and staged.paper.total_pages and page_number > staged.paper.total_pages):
                            raise ValueError
                    except (TypeError, ValueError, OverflowError):
                        raise ValueError("Choose a valid source page number.") from None
                    if action == "crop":
                        if not staged:
                            raise ValueError("The original PDF is unavailable. Upload a replacement image instead.")
                        try:
                            box = validate_box([request.POST.get(k, "") for k in ("x0", "y0", "x1", "y1")])
                            with _open_private_pdf(staged.paper) as document:
                                if page_number > len(document):
                                    raise ValueError("That source page does not exist.")
                                png = page_image(document[page_number - 1], box, max_side=3200, scale=3)
                        except Http404:
                            raise ValueError("The original PDF could not be opened. Upload a replacement image instead.") from None
                    else:
                        box = []
                        png = _png_upload(request.FILES.get("image"))
                    if not source_figure and any(f.source == "question_crop" and f.option_id is None
                                                 and f.page_number == page_number for f in figures):
                        raise ValueError("This page already has an original image. Replace it instead of adding a duplicate.")
                else:
                    raise ValueError("Choose an image operation.")

                # Clone only after all input has been validated. Existing exams
                # keep their own version, option IDs and image files.
                index = figures.index(source_figure) if source_figure else None
                question = clone_for_edit(original)
                figure = list(question.figures.all())[index] if index is not None else None
                if action == "remove":
                    figure.delete()  # old file may still be referenced by past tests
                else:
                    figure = figure or QuestionFigure(question=question)
                    figure.page_number, figure.bounding_box, figure.source = page_number, box, "question_crop"
                    figure.private_image.save(f"bank-{uuid.uuid4().hex}.png", ContentFile(png), save=True)
                    saved_file = figure.private_image.name
                    question.is_image_first = True
                question.key_verified = False
                question.answer_status = AnswerStatus.NEEDS_REVIEW
                question.save(update_fields=["is_image_first", "key_verified", "answer_status"])
        except ValueError as exc:
            if saved_file:
                private_practice_storage.delete(saved_file)
            messages.error(request, str(exc))
            return redirect("questions:practice_bank_images", pk=pk)
        except Exception:
            if saved_file:
                private_practice_storage.delete(saved_file)
            raise
        log_audit(request.user, "PRIVATE_BANK_IMAGE_EDITED", f"Original image for private bank question {question.pk} changed.", request=request)
        messages.warning(request, "Original image saved for future tests. Check the answer again; existing tests and results were not changed.")
        return redirect("questions:practice_bank_images", pk=question.pk)


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
                questions = list(bank_items(request.user).select_for_update().filter(pk__in=ids)
                                 .prefetch_related("options__figures", "figures"))
                if len(questions) != len(ids):
                    raise Http404()
                by_id = {q.pk: q for q in questions}
                questions = [by_id[pk] for pk in ids]
                if not targets_for_track(request.user.exam_track) or any(not q.target_type for q in questions):
                    messages.error(request, "Choose your exam track and classify every selected question before creating a test.")
                    return redirect("questions:practice_bank")
                for question in questions:
                    if question.question_type not in (Question.QuestionType.MCQ_SINGLE, Question.QuestionType.MCQ_MULTIPLE, Question.QuestionType.NUMERICAL):
                        messages.error(request, "A selected question type is unsupported. Review or exclude it.")
                        return redirect("questions:practice_bank")
                    options = list(question.options.all())
                    crops = primary_question_crops(question, image_first=True, private=True)
                    unavailable = unavailable_crop_pages(crops, private=True)
                    usable_crop = bool(crops and not unavailable and not missing_crop_pages(question, crops))
                    incomplete = (question.question_type != Question.QuestionType.NUMERICAL and
                        (len(options) < 2 or any(not option.option_text.strip()
                        and not any(figure.private_image for figure in option.figures.all()) and not usable_crop
                        for option in options)))
                    if incomplete:
                        messages.error(request, "A selected question has missing choices or an unavailable original image. Edit or repair it before creating the test.")
                        return redirect("questions:practice_bank")
                positive = sum((q.positive_marks for q in questions), Decimal("0"))
                negative = sum((q.negative_marks for q in questions), Decimal("0"))
                if positive > Decimal("9999.99") or negative > Decimal("9999.99"):
                    messages.error(request, "Selected marks exceed the supported total of 9999.99.")
                    return redirect("questions:practice_bank")
                # Bank-based tests do not spend PDF credits; a given test
                # still permits only one attempt under the new policy.
                exam = Exam.objects.create(is_private_practice=True, slug=f"practice-{uuid.uuid4().hex}",
                    title=form.cleaned_data["title"], category=_system_category(), creator=request.user,
                    target_type=request.user.exam_track,
                    subject=questions[0].subject if all(q.subject == questions[0].subject for q in questions) else "Mixed subjects",
                    status=Exam.ExamStatus.PUBLISHED, is_free=True, max_attempts=0,
                    duration_minutes=form.cleaned_data["duration_minutes"], total_marks=positive,
                    passing_percentage=form.cleaned_data["passing_percentage"] or Decimal("0"),
                    shuffle_questions=False, shuffle_options=False, image_first_mode=any(q.is_image_first for q in questions),
                    show_result_immediately=True, allow_review_answers=True,
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
