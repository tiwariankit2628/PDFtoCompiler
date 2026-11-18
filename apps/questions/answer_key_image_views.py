"""Owner-scoped, non-persistent answer-key image previews."""
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.cache import patch_cache_control
from django.views import View

from apps.core.permissions import TeacherRequiredMixin
from .answer_key_image import AnswerImageError, preview_answer_images
from .models import ExtractedQuestion, QuestionPaperUpload
from .classroom_views import PrivateClassroomMixin, _editable, owned_paper as owned_classroom_paper
from .practice_views import PrivateStudentMixin, editable, owned_paper as owned_practice_paper


def _preview(request, paper, *, teacher_import=False):
    questions = paper.extracted_questions.all()
    if teacher_import:
        questions = questions.exclude(status=ExtractedQuestion.Status.REJECTED)
    else:
        questions = questions.filter(include_in_practice=True)
    questions = list(questions.order_by("question_number", "id"))
    if not questions:
        return JsonResponse({"error": "Include at least one question before reading an answer key."}, status=400)
    try:
        result = preview_answer_images(request.FILES.getlist("images"), questions,
                                       exclude_approved=teacher_import)
    except AnswerImageError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    response = JsonResponse(result)
    response["X-Content-Type-Options"] = "nosniff"
    return response


class TeacherAnswerImagePreviewView(TeacherRequiredMixin, View):
    """Only the uploader or an admin can preview a normal paper's key."""

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        patch_cache_control(response, private=True, no_store=True)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response

    def post(self, request, pk):
        paper = get_object_or_404(QuestionPaperUpload, pk=pk, is_student_private=False,
                                  is_classroom_private=False)
        if not request.user.is_admin_role and paper.uploaded_by_id != request.user.pk:
            raise PermissionDenied("You cannot import answer keys for this paper.")
        if paper.status in (paper.ProcessingStatus.PENDING, paper.ProcessingStatus.PROCESSING):
            return JsonResponse({"error": "Wait for extraction to finish before reading a key."}, status=409)
        return _preview(request, paper, teacher_import=True)


class PracticeAnswerImagePreviewView(PrivateStudentMixin, View):
    # Source keys are as sensitive as the paper during an active practice test.
    sensitive_during_attempt = True

    def post(self, request, pk):
        paper = owned_practice_paper(request.user, pk)
        editable(paper)
        return _preview(request, paper)


class ClassroomAnswerImagePreviewView(PrivateClassroomMixin, View):
    def post(self, request, pk):
        paper = owned_classroom_paper(request.user, pk)
        _editable(paper)
        return _preview(request, paper)
