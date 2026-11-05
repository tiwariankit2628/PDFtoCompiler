"""Student-owned, explicitly requested local AI comparisons after submission."""
from datetime import timedelta
import uuid

from django.contrib import messages
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View

from apps.exams.models import ExamAttempt, ExamResponse
from apps.payments.services import QuotaExceeded, consume, lock_practice_user
from .local_practice_ai import LocalEstimateUnavailable, config, estimate_response
from .models import PracticeLocalEstimate
from .practice_analytics import compute_practice_result, confirmed_key
from .practice_views import PrivateStudentMixin, active_private_attempt


def _owned_attempt(user, attempt_uuid):
    attempt = get_object_or_404(ExamAttempt.objects.select_related("exam"), attempt_uuid=attempt_uuid,
                                student=user, exam__creator=user, exam__is_private_practice=True)
    attempt.auto_timeout_if_expired()
    if attempt.status == ExamAttempt.AttemptStatus.IN_PROGRESS:
        raise Http404("The attempt has not been submitted.")
    return attempt


def _json_requested(request):
    return "application/json" in request.headers.get("Accept", "")


def estimate_context(attempt, rows):
    job = PracticeLocalEstimate.objects.filter(attempt=attempt).first()
    items = {item.get("response_id"): item for item in (job.results if job else []) if isinstance(item, dict)}
    for row in rows:
        row["local_estimate"] = items.get(row["response"].pk)
    values = list(items.values())
    return {"local_ai_configured": bool(config()), "local_ai_job": job,
            "local_ai_summary": {"processed": len(values), "total": len(job.candidate_ids) if job else 0,
                                 "matches": sum(item.get("state") == "suggested" and item.get("match") is True for item in values),
                                 "different": sum(item.get("state") == "suggested" and item.get("match") is False for item in values),
                                 "unknown": sum(item.get("state") != "suggested" for item in values)}}


class PracticeEstimateStartView(PrivateStudentMixin, View):
    sensitive_during_attempt = True

    def post(self, request, attempt_uuid):
        attempt = _owned_attempt(request.user, attempt_uuid)
        settings = config()
        if request.POST.get("understand") != "on":
            message = "Acknowledge that this is an unverified AI estimate before starting."
        elif not settings:
            message = "Local AI is not configured. A local-only model must be installed and enabled on the server."
        else:
            rows = compute_practice_result(attempt)["rows"]
            candidate_ids = [row["response"].pk for row in rows if row["status"] == "ungraded"][:settings["limit"]]
            if not candidate_ids:
                message = "There are no answered, ungraded questions to compare."
            else:
                job = PracticeLocalEstimate.objects.filter(attempt=attempt).first()
                if job is None:
                    try:
                        # One plan allowance per comparison, not per question;
                        # resuming is free and deleting does not refund usage.
                        consume(request.user, "ai_suggestions")
                        job = PracticeLocalEstimate.objects.create(attempt=attempt, model_id=settings["model"], candidate_ids=candidate_ids)
                    except QuotaExceeded as exc:
                        message = str(exc)
                if job is not None:
                    if job.model_id != settings["model"]:
                        message = "The configured model changed. Clear the old estimate before starting a new one."
                    else:
                        if _json_requested(request):
                            return JsonResponse({"processed": len(job.results), "total": len(job.candidate_ids),
                                                 "complete": len(job.results) >= len(job.candidate_ids)})
                        messages.info(request, "Estimate saved. Use a JavaScript-enabled browser to process or resume it.")
                        return redirect("questions:practice_result", attempt_uuid=attempt_uuid)
        if _json_requested(request):
            return JsonResponse({"error": message}, status=400)
        messages.error(request, message)
        return redirect("questions:practice_result", attempt_uuid=attempt_uuid)


class PracticeEstimateStepView(PrivateStudentMixin, View):
    # Hold the student lock only for short checks and commits, not while the
    # CPU model is thinking. Check again before returning any answer-bearing
    # data in case another private test started in the meantime.
    sensitive_during_attempt = False

    def post(self, request, attempt_uuid):
        settings = config()
        if not settings:
            return JsonResponse({"error": "Local AI is not configured."}, status=503)
        with transaction.atomic():
            lock_practice_user(request.user)
            if active_private_attempt(request.user):
                return JsonResponse({"error": "Finish your active practice test before viewing AI comparisons."}, status=423)
            attempt = _owned_attempt(request.user, attempt_uuid)
            job = get_object_or_404(PracticeLocalEstimate, attempt=attempt)
            if job.model_id != settings["model"]:
                return JsonResponse({"error": "The local model changed; clear this estimate before restarting."}, status=409)
            processed_ids = {item.get("response_id") for item in job.results if isinstance(item, dict)}
            pending = next((pk for pk in job.candidate_ids if pk not in processed_ids), None)
            if pending is None:
                return JsonResponse({"processed": len(job.results), "total": len(job.candidate_ids), "complete": True})
            # A lost browser connection may leave an in-flight lease. Only
            # expire it after the model timeout plus a safety margin.
            stale = timezone.now() - timedelta(seconds=settings["timeout"] + 45)
            PracticeLocalEstimate.objects.filter(pk=job.pk, request_started_at__lt=stale).update(
                current_response_id=None, request_token=None, request_started_at=None)
            token = uuid.uuid4()
            claimed = PracticeLocalEstimate.objects.filter(pk=job.pk, current_response_id__isnull=True).update(
                current_response_id=pending, request_token=token, request_started_at=timezone.now(), last_error="")
            if not claimed:
                return JsonResponse({"error": "A comparison is already running. Wait, then resume."}, status=409)
        response = (ExamResponse.objects.filter(pk=pending, attempt=attempt)
                    .select_related("exam_question__question", "selected_option")
                    .prefetch_related("exam_question__question__options__figures", "exam_question__question__figures")
                    .first())
        try:
            if response is None or not response.selected_option_id:
                item = {"state": "skipped", "key": "", "reason": "The submitted answer is unavailable."}
            else:
                question = response.exam_question.question
                options = list(question.options.all())
                if (response.exam_question.exam_id != attempt.exam_id
                        or not question.is_private_practice or question.creator_id != request.user.pk
                        or response.selected_option.question_id != question.pk or confirmed_key(question, options)):
                    item = {"state": "skipped", "key": "", "reason": "This response is no longer an ungraded private answer."}
                else:
                    item = estimate_response(response, settings)
                    if item["state"] == "suggested":
                        item["match"] = item["key"] == response.selected_option.option_key
            item["response_id"] = pending
        except LocalEstimateUnavailable as exc:
            PracticeLocalEstimate.objects.filter(pk=job.pk, request_token=token).update(
                current_response_id=None, request_token=None, request_started_at=None, last_error=str(exc)[:255])
            return JsonResponse({"error": str(exc)}, status=503)
        except Exception:
            PracticeLocalEstimate.objects.filter(pk=job.pk, request_token=token).update(
                current_response_id=None, request_token=None, request_started_at=None,
                last_error="The local comparison failed. Please retry.")
            return JsonResponse({"error": "The local comparison failed. Please retry."}, status=503)
        with transaction.atomic():
            lock_practice_user(request.user)
            active = active_private_attempt(request.user)
            current = PracticeLocalEstimate.objects.select_for_update().filter(pk=job.pk, request_token=token).first()
            if not current:
                return JsonResponse({"error": "This request expired; resume the estimate."}, status=409)
            current.results = list(current.results) + [item]
            current.current_response_id = None
            current.request_token = None
            current.request_started_at = None
            current.last_error = ""
            current.save(update_fields=["results", "current_response_id", "request_token", "request_started_at", "last_error", "updated_at"])
            progress = {"processed": len(current.results), "total": len(current.candidate_ids),
                        "complete": len(current.results) >= len(current.candidate_ids)}
            if active:
                return JsonResponse({"error": "Finish your active practice test before viewing AI comparisons."}, status=423)
        return JsonResponse(progress)


class PracticeEstimateDeleteView(PrivateStudentMixin, View):
    sensitive_during_attempt = True

    def post(self, request, attempt_uuid):
        attempt = _owned_attempt(request.user, attempt_uuid)
        PracticeLocalEstimate.objects.filter(attempt=attempt).delete()
        messages.success(request, "The unverified AI comparison was cleared. Your test answers and verified scores are unchanged.")
        return redirect("questions:practice_result", attempt_uuid=attempt_uuid)
