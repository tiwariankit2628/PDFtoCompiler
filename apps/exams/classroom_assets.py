"""Attempt-scoped access to privately stored classroom question images."""
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.views import View

from apps.core.permissions import ActiveUserRequiredMixin
from apps.questions.models import Question, QuestionFigure
from apps.questions.practice_views import _private_file_response
from .models import ExamAttempt


def _authorized_attempt(request, attempt_uuid):
    attempt = get_object_or_404(ExamAttempt.objects.select_related("exam", "classroom_session"),
                                attempt_uuid=attempt_uuid, classroom_session__isnull=False,
                                exam__is_classroom_only=True)
    user = request.user
    if user.is_admin_role or (user.is_teacher and attempt.classroom_session.teacher_id == user.pk):
        return attempt
    if not user.is_student or attempt.student_id != user.pk:
        raise Http404()
    if attempt.status == ExamAttempt.AttemptStatus.IN_PROGRESS:
        if attempt.is_expired:
            attempt.auto_timeout_if_expired()
            raise PermissionDenied("This attempt has ended.")
        return attempt
    # Match result-review policy, and do not expose past answers during a retake.
    if not attempt.classroom_session.student_answers_visible:
        raise PermissionDenied("Answer review is not available for this test.")
    active = attempt.exam.attempts.filter(student=user, status=ExamAttempt.AttemptStatus.IN_PROGRESS).select_related("exam")
    for other in active:
        if other.is_expired:
            other.auto_timeout_if_expired()
        else:
            raise PermissionDenied("Finish your active attempt before viewing past answers.")
    return attempt


class ClassroomAttemptFigureView(ActiveUserRequiredMixin, View):
    def get(self, request, attempt_uuid, pk):
        attempt = _authorized_attempt(request, attempt_uuid)
        figure = get_object_or_404(QuestionFigure, pk=pk, question__is_classroom_snapshot=True,
                                   question__exam_instances__candidate_responses__attempt=attempt)
        return _private_file_response(figure.private_image, "image/png")


class ClassroomAttemptImageView(ActiveUserRequiredMixin, View):
    def get(self, request, attempt_uuid, pk):
        attempt = _authorized_attempt(request, attempt_uuid)
        question = get_object_or_404(Question, pk=pk, is_classroom_snapshot=True,
                                     exam_instances__candidate_responses__attempt=attempt)
        return _private_file_response(question.private_image, "image/png")
