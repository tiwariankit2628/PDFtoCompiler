"""Track-scoped 16-test series; empty slots are not fabricated tests."""
from django.db.models import Q
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.cache import patch_cache_control
from django.views import View

from apps.core.exam_targets import exam_matches_student, targets_for_track
from apps.core.permissions import AdminRequiredMixin, StudentRequiredMixin
from apps.core.utils import log_audit
from apps.payments.packs import has_series_access
from .models import Exam, ExamAttempt, ExamQuestion, SeriesSlot, TestSeries


def series_content_is_private(exam):
    """Paid series may only contain exclusive snapshots with private assets.

    Ordinary question-bank images can live under publicly served MEDIA_ROOT;
    treating their URLs as gated would give a false impression of protection.
    The series publishing workflow instead copies reviewed questions and images
    into private storage, without mutating the source paper or other exams.
    """
    from apps.questions.models import Question, QuestionFigure, QuestionOption
    questions = Question.objects.filter(exam_instances__exam=exam)
    if not questions.exists() or questions.filter(Q(is_private_practice=True) | Q(is_classroom_snapshot=False)).exists():
        return False
    if questions.exclude(Q(image="") | Q(image__isnull=True)).exists():
        return False
    if QuestionOption.objects.filter(question__in=questions).exclude(Q(image="") | Q(image__isnull=True)).exists():
        return False
    if QuestionFigure.objects.filter(question__in=questions).exclude(Q(image="") | Q(image__isnull=True)).exists():
        return False
    if ExamQuestion.objects.filter(question__in=questions).exclude(exam=exam).exists():
        return False
    # A path in a database row is not enough: do not sell a broken paper if
    # its privately stored question/option figures have disappeared.
    try:
        for question in questions.prefetch_related("figures"):
            if question.private_image and not question.private_image.storage.exists(question.private_image.name):
                return False
            for figure in question.figures.all():
                if not figure.private_image or not figure.private_image.storage.exists(figure.private_image.name):
                    return False
    except (OSError, ValueError):
        return False
    return True


def series_keys_confirmed(exam):
    """A commercial series is not offered until every key is gradeable."""
    from apps.questions.models import AnswerStatus, Question
    from .typed_responses import typed_key_is_gradeable
    items = exam.exam_questions.select_related("question").prefetch_related("question__options")
    for item in items:
        question = item.question
        options = list(question.options.all())
        if question.answer_status != AnswerStatus.MANUALLY_VERIFIED:
            return False
        if question.question_type == Question.QuestionType.MCQ_SINGLE:
            if len([option for option in options if option.is_correct]) != 1:
                return False
        elif question.question_type in (Question.QuestionType.MCQ_MULTIPLE, Question.QuestionType.NUMERICAL):
            if not typed_key_is_gradeable(question, options, positive=item.positive_marks,
                                          negative=item.negative_marks, key_confirmed=True):
                return False
        else:
            return False
    return bool(items)


def slot_ready(slot):
    if not slot.exam_id or not slot.series.is_active:
        return False
    exam = slot.exam
    allowed = targets_for_track(slot.series.track)
    return bool(exam.status == Exam.ExamStatus.PUBLISHED and not exam.is_private_practice
                and not exam.is_classroom_only and exam.target_type in allowed
                and exam.exam_questions.exists()
                and not exam.exam_questions.exclude(question__target_type__in=allowed).exists()
                and series_content_is_private(exam)
                and not exam.classroom_sessions.exists()
                and (not exam.end_time or exam.end_time > timezone.now()))


def series_ready(track):
    series = TestSeries.objects.filter(track=track, is_active=True).first()
    if not series:
        return False
    slots = list(series.slots.select_related("series", "exam").order_by("position"))
    return (len(slots) == 16 and [s.position for s in slots] == list(range(1, 17))
            and all(slot_ready(s) and series_keys_confirmed(s.exam) for s in slots))


def series_start_allowed(user, exam):
    slot = SeriesSlot.objects.select_related("series", "exam").filter(exam=exam).first()
    if not slot:
        return None  # ordinary institutional test
    if not user.is_student or slot.series.track != user.exam_track or not exam_matches_student(user, exam):
        return False
    if not slot_ready(slot):
        return False
    return slot.position == 1 or has_series_access(user, slot.series.track)


class SeriesOperatorView(AdminRequiredMixin, View):
    """Readiness and one-way key release; never fabricate or replace papers."""
    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        patch_cache_control(response, private=True, no_store=True)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response

    def get(self, request):
        groups = []
        for series in TestSeries.objects.order_by("track"):
            rows = []
            for slot in series.slots.select_related("series", "exam").order_by("position"):
                rows.append({"slot": slot, "ready": slot_ready(slot),
                             "keys_confirmed": bool(slot.exam_id and series_keys_confirmed(slot.exam))})
            groups.append({"series": series, "rows": rows, "on_sale": series_ready(series.track)})
        return render(request, "exams/series_operator.html", {"groups": groups})


class SeriesReleaseKeysView(AdminRequiredMixin, View):
    def post(self, request, pk):
        with transaction.atomic():
            slot = get_object_or_404(SeriesSlot.objects.select_related("series"), pk=pk, exam__isnull=False)
            exam = get_object_or_404(Exam.objects.select_for_update(), pk=slot.exam_id)
            if not exam.allow_review_answers:
                exam.allow_review_answers = True
                exam.save(update_fields=["allow_review_answers"])
                log_audit(request.user, "SERIES_KEYS_RELEASED", f"Released keys for {slot.series.track} series slot {slot.position}.", request=request)
        messages.success(request, "Answer keys and explanations are released for this series test. Existing scores are unchanged.")
        response = redirect("exams:series_operator")
        patch_cache_control(response, private=True, no_store=True)
        return response


class SeriesView(StudentRequiredMixin, View):
    def get(self, request):
        series = TestSeries.objects.filter(track=request.user.exam_track, is_active=True).first()
        rows = []
        if series:
            owned = has_series_access(request.user, series.track)
            slots = series.slots.select_related("series", "exam").order_by("position")
            attempts = {a.exam_id: a for a in ExamAttempt.objects.filter(
                student=request.user, exam_id__in=slots.values("exam_id")).order_by("started_at")}
            for slot in slots:
                available = slot_ready(slot) and exam_matches_student(request.user, slot.exam) if slot.exam_id else False
                rows.append({"slot": slot, "available": available, "unlocked": slot.position == 1 or owned,
                             "attempt": attempts.get(slot.exam_id)})
        else:
            owned = False
        return render(request, "exams/series.html", {"series": series, "rows": rows, "owned": owned,
            "ready": bool(series and series_ready(series.track))})
