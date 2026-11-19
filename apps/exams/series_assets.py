"""Attempt-scoped access to privately stored test-series question images."""
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.views import View

from apps.core.permissions import ActiveUserRequiredMixin
from apps.questions.models import Question, QuestionFigure
from apps.questions.practice_views import _private_file_response
from .models import ExamAttempt


def _authorized_series_attempt(request, attempt_uuid):
    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("exam"), attempt_uuid=attempt_uuid,
        classroom_session__isnull=True, exam__series_slot__isnull=False,
        exam__is_private_practice=False, exam__is_classroom_only=False,
    )
    user = request.user
    if user.is_admin_role:
        return attempt
    if not user.is_student or attempt.student_id != user.pk:
        raise Http404()
    if attempt.status == ExamAttempt.AttemptStatus.IN_PROGRESS:
        if attempt.is_expired:
            attempt.auto_timeout_if_expired()
            raise PermissionDenied("This attempt has ended; open its result to review images.")
        return attempt
    # Preserve safety for any historic in-progress repeat, even though new
    # series attempts cannot be repeated.
    for active in attempt.exam.attempts.filter(student=user, status=ExamAttempt.AttemptStatus.IN_PROGRESS).select_related("exam"):
        if active.is_expired:
            active.auto_timeout_if_expired()
        else:
            raise PermissionDenied("Finish your active attempt before viewing past images.")
    return attempt


class SeriesAttemptFigureView(ActiveUserRequiredMixin, View):
    def get(self, request, attempt_uuid, pk):
        attempt = _authorized_series_attempt(request, attempt_uuid)
        figure = get_object_or_404(QuestionFigure, pk=pk, question__is_classroom_snapshot=True,
                                   question__is_private_practice=False,
                                   question__exam_instances__candidate_responses__attempt=attempt)
        return _private_file_response(figure.private_image, "image/png")


class SeriesAttemptImageView(ActiveUserRequiredMixin, View):
    def get(self, request, attempt_uuid, pk):
        attempt = _authorized_series_attempt(request, attempt_uuid)
        question = get_object_or_404(Question, pk=pk, is_classroom_snapshot=True, is_private_practice=False,
                                    exam_instances__candidate_responses__attempt=attempt)
        return _private_file_response(question.private_image, "image/png")
