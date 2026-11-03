"""Consent-gated answer suggestions and explicit human decisions."""
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.cache import patch_cache_control
from django.views import View

from apps.core.permissions import TeacherRequiredMixin
from apps.core.utils import log_audit
from apps.payments.services import QuotaExceeded, consume, lock_practice_user, usage_summary
from .answer_ai import GeminiAnswerClient, SuggestionError, configured, question_fingerprint, vision_images
from .figure_views import _authorize, _editable
from .models import AIAnswerSuggestion, AnswerStatus, ExtractedQuestion, QuestionPaperUpload
from .practice_views import PrivateStudentMixin, active_private_attempt, editable, owned_question


def _teacher_question(user, pk):
    question = get_object_or_404(ExtractedQuestion.objects.select_related("paper").prefetch_related("figures"), pk=pk)
    _authorize(user, question.paper)
    return question


def _workspace(request, question, private):
    (editable(question.paper) if private else _editable(question))
    suggestions = question.ai_suggestions.all()[:8]
    return render(request, "questions/answer_suggestion.html", {
        "question": question, "suggestions": suggestions, "latest": suggestions[0] if suggestions else None,
        "ai_configured": configured(), "student_practice": private,
        "usage": usage_summary(request.user) if private else None,
        "model_id": settings.AI_ANSWER_MODEL if configured() else "",
    })


def _request_suggestion(request, pk, private):
    target_name = "questions:practice_answer_suggestion" if private else "questions:answer_suggestion"
    if request.POST.get("consent") != "yes":
        messages.error(request, "Explicit consent is required before sending question content to Gemini.")
        return redirect(target_name, pk=pk)
    if not configured():
        messages.error(request, "AI suggestions are disabled or Gemini has not been configured by the administrator.")
        return redirect(target_name, pk=pk)
    vision = request.POST.get("vision") == "yes"
    try:
        with transaction.atomic():
            if private:
                lock_practice_user(request.user)
                active = active_private_attempt(request.user)
                if active:
                    messages.info(request, "Finish your active practice test before requesting an answer suggestion.")
                    return redirect("exams:exam_session", attempt_uuid=active.attempt_uuid)
                question = owned_question(request.user, pk)
                paper = get_object_or_404(QuestionPaperUpload.objects.select_for_update(),
                                          pk=question.paper_id, uploaded_by=request.user, is_student_private=True)
                editable(paper)
            else:
                question = _teacher_question(request.user, pk)
                QuestionPaperUpload.objects.select_for_update().get(pk=question.paper_id)
                _editable(question)
            options = question.options if isinstance(question.options, list) else []
            if len([o for o in options if isinstance(o, dict) and o.get("key")]) < 2:
                messages.error(request, "Add at least two options before requesting a suggestion.")
                return redirect(target_name, pk=pk)
            fingerprint = question_fingerprint(question, vision)
            # A crashed worker/request should not block this question forever.
            question.ai_suggestions.filter(status=AIAnswerSuggestion.Status.PROCESSING,
                                           created_at__lt=timezone.now() - timedelta(minutes=10)).update(
                status=AIAnswerSuggestion.Status.FAILED, error="The previous request did not finish; provider charges may still apply.")
            existing = question.ai_suggestions.filter(input_hash=fingerprint, model_id=settings.AI_ANSWER_MODEL,
                vision_used=vision, status__in=[AIAnswerSuggestion.Status.PROCESSING, AIAnswerSuggestion.Status.SUGGESTED]).first()
            if existing:
                messages.info(request, "A matching request already exists; no duplicate provider request was sent.")
                return redirect(target_name, pk=pk)
            if private:
                consume(request.user, "ai_suggestions")
            suggestion = AIAnswerSuggestion.objects.create(
                question=question, requested_by=request.user, model_id=settings.AI_ANSWER_MODEL,
                vision_used=vision, input_hash=fingerprint,
            )
    except QuotaExceeded as exc:
        messages.error(request, str(exc))
        return redirect("payments:plans")
    except IntegrityError:
        # Concurrent duplicate is prevented by a database uniqueness constraint.
        messages.info(request, "A matching request is already being processed.")
        return redirect(target_name, pk=pk)

    # Never hold a DB transaction/user lock across a paid network request.
    client = GeminiAnswerClient()
    try:
        images = vision_images(question) if vision else []
        result = client.suggest(question, images)
    except SuggestionError as exc:
        AIAnswerSuggestion.objects.filter(pk=suggestion.pk, status=AIAnswerSuggestion.Status.PROCESSING).update(
            status=AIAnswerSuggestion.Status.FAILED, error=str(exc)[:255], provider_calls=client.calls,
            updated_at=timezone.now())
        messages.warning(request, str(exc))
    else:
        AIAnswerSuggestion.objects.filter(pk=suggestion.pk, status=AIAnswerSuggestion.Status.PROCESSING).update(
            status=AIAnswerSuggestion.Status.SUGGESTED, provider_calls=client.calls, updated_at=timezone.now(), **result)
        messages.success(request, "AI suggestion received. It is not a confirmed answer key; review it against the original.")
    log_audit(request.user, "AI_ANSWER_REQUESTED", f"Suggestion request {suggestion.pk}; provider HTTP attempts {client.calls}.", request=request)
    return redirect(target_name, pk=pk)


def _decide(request, pk, private):
    target_name = "questions:practice_answer_suggestion" if private else "questions:answer_suggestion"
    question = owned_question(request.user, pk) if private else _teacher_question(request.user, pk)
    with transaction.atomic():
        paper = get_object_or_404(QuestionPaperUpload.objects.select_for_update(), pk=question.paper_id)
        question = get_object_or_404(ExtractedQuestion.objects.select_for_update().prefetch_related("figures"), pk=question.pk)
        (editable(paper) if private else _editable(question))
        try:
            suggestion_id = int(request.POST.get("suggestion_id", ""))
            if suggestion_id <= 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise Http404() from None
        suggestion = get_object_or_404(AIAnswerSuggestion.objects.select_for_update(),
                                       pk=suggestion_id, question=question,
                                       status=AIAnswerSuggestion.Status.SUGGESTED)
        if private and suggestion.requested_by_id != request.user.pk:
            raise Http404()
        action = request.POST.get("action")
        if action == "reject":
            suggestion.status = AIAnswerSuggestion.Status.REJECTED
            suggestion.save(update_fields=["status", "updated_at"])
            messages.info(request, "Suggestion rejected; the confirmed key was not changed.")
        elif action == "confirm":
            if request.POST.get("confirm") != "yes":
                messages.error(request, "Confirm that you personally checked this answer before saving it.")
                return redirect(target_name, pk=pk)
            if suggestion.input_hash != question_fingerprint(question, suggestion.vision_used):
                messages.error(request, "The question or its images changed after this suggestion. Review it again or request a new suggestion.")
                return redirect(target_name, pk=pk)
            key = request.POST.get("option_key", "").strip().upper()
            options = question.options if isinstance(question.options, list) else []
            keys = {str(o.get("key", "")).strip().upper() for o in options if isinstance(o, dict)}
            explanation = request.POST.get("explanation", "").strip()
            if key not in keys or key not in {"A", "B", "C", "D", "E"} or len(explanation) > 8000:
                messages.error(request, "Choose an existing option and keep the explanation under 8,000 characters.")
                return redirect(target_name, pk=pk)
            question.correct_option = key
            question.answer_status = AnswerStatus.STUDENT_CONFIRMED if private else AnswerStatus.MANUALLY_VERIFIED
            question.explanation = explanation
            question.save(update_fields=["correct_option", "answer_status", "explanation", "updated_at"])
            suggestion.status = AIAnswerSuggestion.Status.ACCEPTED
            suggestion.save(update_fields=["status", "updated_at"])
            messages.success(request, "Answer confirmed. Transcription and image review flags remain unchanged.")
        else:
            messages.error(request, "Choose to confirm or reject the suggestion.")
    return redirect(target_name, pk=pk)


class StudentSuggestionWorkspace(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    def get(self, request, pk):
        return _workspace(request, owned_question(request.user, pk), True)


class StudentSuggestionRequest(PrivateStudentMixin, View):
    # _request_suggestion explicitly locks for the reservation, then releases
    # before network I/O. GET/decision workspaces stay blocked during attempts.
    def post(self, request, pk):
        return _request_suggestion(request, pk, True)


class StudentSuggestionDecision(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    def post(self, request, pk):
        return _decide(request, pk, True)


class TeacherSuggestionWorkspace(TeacherRequiredMixin, View):
    def get(self, request, pk):
        response = _workspace(request, _teacher_question(request.user, pk), False)
        patch_cache_control(response, private=True, no_store=True)
        return response


class TeacherSuggestionRequest(TeacherRequiredMixin, View):
    def post(self, request, pk):
        return _request_suggestion(request, pk, False)


class TeacherSuggestionDecision(TeacherRequiredMixin, View):
    def post(self, request, pk):
        return _decide(request, pk, False)
