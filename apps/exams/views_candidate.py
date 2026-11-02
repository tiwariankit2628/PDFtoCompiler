"""
Candidate Computer-Based Testing (CBT) Examination Engine Views.
Handles candidate instructions, exam launch, live timed examination session,
real-time answer autosave, navigation palette synchronization, anti-cheat event logging,
server-authoritative timeout, and atomic final evaluation and submission.
"""
import json
import random
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, ListView

from apps.core.permissions import ActiveUserRequiredMixin
from apps.core.utils import log_audit
from .models import Category, Exam, ExamAttempt, ExamQuestion, ExamResponse, ExamSecurityLog


class ExamCatalogView(ActiveUserRequiredMixin, ListView):
    """
    Searchable, filterable catalog of published CBT examinations available for candidates.
    """

    model = Exam
    template_name = "exams/exam_catalog.html"
    context_object_name = "exams"
    paginate_by = 12

    def get_queryset(self):
        now = timezone.now()
        qs = (
            Exam.objects.filter(status=Exam.ExamStatus.PUBLISHED)
            .filter(Q(start_time__isnull=True) | Q(start_time__lte=now))
            .filter(Q(end_time__isnull=True) | Q(end_time__gte=now))
            .select_related("category")
            .prefetch_related("exam_questions")
        )

        cat_slug = self.request.GET.get("category", "").strip()
        if cat_slug:
            qs = qs.filter(category__slug=cat_slug)

        search_query = self.request.GET.get("q", "").strip()
        if search_query:
            qs = qs.filter(
                Q(title__icontains=search_query)
                | Q(subject__icontains=search_query)
                | Q(topics__icontains=search_query)
                | Q(target_exam__icontains=search_query)
            )

        filter_type = self.request.GET.get("type", "").strip()
        if filter_type == "free":
            qs = qs.filter(is_free=True)
        elif filter_type == "premium":
            qs = qs.filter(is_free=False)

        return qs.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["categories"] = Category.objects.filter(is_active=True)
        context["selected_category"] = self.request.GET.get("category", "")
        context["selected_type"] = self.request.GET.get("type", "")
        context["search_query"] = self.request.GET.get("q", "")
        return context


class ExamInstructionsView(ActiveUserRequiredMixin, DetailView):
    """
    Pre-examination instructions and readiness checkpoint.
    Displays syllabus, duration, marking scheme, rules, and palette explanation.
    Provides 'Start Examination' or 'Resume Examination' CTA.
    """

    model = Exam
    template_name = "exams/exam_instructions.html"
    context_object_name = "exam"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        exam = self.object
        user = self.request.user
        now = timezone.now()

        # Check existing attempts
        user_attempts = ExamAttempt.objects.filter(exam=exam, student=user).order_by("-started_at")
        active_attempt = user_attempts.filter(status=ExamAttempt.AttemptStatus.IN_PROGRESS).first()

        # Handle active attempt timeout check
        if active_attempt and active_attempt.is_expired:
            active_attempt.auto_timeout_if_expired()
            active_attempt = None

        completed_count = user_attempts.filter(
            status__in=[
                ExamAttempt.AttemptStatus.SUBMITTED,
                ExamAttempt.AttemptStatus.EVALUATED,
                ExamAttempt.AttemptStatus.TIMED_OUT,
            ]
        ).count()

        # Eligibility checks
        can_start = True
        block_reason = ""

        if exam.status != Exam.ExamStatus.PUBLISHED:
            can_start = False
            block_reason = "This examination is not currently published."
        elif exam.start_time and now < exam.start_time:
            can_start = False
            block_reason = f"This examination is scheduled to open on {exam.start_time.strftime('%b %d, %Y at %I:%M %p')}."
        elif exam.end_time and now > exam.end_time:
            can_start = False
            block_reason = "This examination has expired and is no longer accepting new attempts."
        elif exam.total_questions_count == 0:
            can_start = False
            block_reason = "This examination does not currently have questions assigned."
        elif exam.max_attempts > 0 and completed_count >= exam.max_attempts and not active_attempt:
            can_start = False
            block_reason = f"You have reached the maximum allowed attempts ({exam.max_attempts}) for this examination."

        # Sections
        sections = list(
            exam.exam_questions.values_list("section", flat=True)
            .distinct()
            .order_by("section")
        )
        if not sections or sections == [""]:
            sections = ["General"]

        context.update(
            {
                "active_attempt": active_attempt,
                "completed_count": completed_count,
                "can_start": can_start,
                "block_reason": block_reason,
                "sections": sections,
                "total_questions": exam.total_questions_count,
            }
        )
        return context


class ExamStartView(ActiveUserRequiredMixin, View):
    """
    Validates candidate eligibility and starts or resumes an examination session.
    Populates candidate-specific ExamResponse records with optional question randomization.
    """

    def post(self, request, slug):
        exam = get_object_or_404(Exam, slug=slug)
        user = request.user
        now = timezone.now()

        # 1. Check for active attempt
        active_attempt = ExamAttempt.objects.filter(
            exam=exam,
            student=user,
            status=ExamAttempt.AttemptStatus.IN_PROGRESS,
        ).first()

        if active_attempt:
            if active_attempt.is_expired:
                active_attempt.auto_timeout_if_expired()
            else:
                messages.info(request, "Resuming your active examination session.")
                return redirect("exams:exam_session", attempt_uuid=active_attempt.attempt_uuid)

        # 2. Status and schedule validations
        if exam.status != Exam.ExamStatus.PUBLISHED:
            messages.error(request, "This examination is not currently active.")
            return redirect("exams:exam_instructions", slug=exam.slug)

        if exam.start_time and now < exam.start_time:
            messages.error(request, "This examination has not started yet.")
            return redirect("exams:exam_instructions", slug=exam.slug)

        if exam.end_time and now > exam.end_time:
            messages.error(request, "This examination window has ended.")
            return redirect("exams:exam_instructions", slug=exam.slug)

        # 3. Attempt limit validation
        completed_count = ExamAttempt.objects.filter(
            exam=exam,
            student=user,
            status__in=[
                ExamAttempt.AttemptStatus.SUBMITTED,
                ExamAttempt.AttemptStatus.EVALUATED,
                ExamAttempt.AttemptStatus.TIMED_OUT,
            ],
        ).count()

        if exam.max_attempts > 0 and completed_count >= exam.max_attempts:
            messages.error(
                request,
                f"You have reached the maximum allowed attempts ({exam.max_attempts}) for this examination.",
            )
            return redirect("exams:exam_instructions", slug=exam.slug)

        # 4. Questions check
        exam_questions = list(
            exam.exam_questions.select_related("question").order_by("order", "id")
        )
        if not exam_questions:
            messages.error(request, "This examination has no questions assigned.")
            return redirect("exams:exam_instructions", slug=exam.slug)

        # 5. Atomic Attempt & Response Creation
        with transaction.atomic():
            started_at = timezone.now()
            expires_at = started_at + timedelta(minutes=exam.duration_minutes)

            attempt = ExamAttempt.objects.create(
                exam=exam,
                student=user,
                status=ExamAttempt.AttemptStatus.IN_PROGRESS,
                started_at=started_at,
                expires_at=expires_at,
                total_marks=exam.total_marks,
            )

            # Question ordering
            ordered_questions = list(exam_questions)
            if exam.shuffle_questions:
                random.shuffle(ordered_questions)

            responses = []
            for idx, eq in enumerate(ordered_questions):
                responses.append(
                    ExamResponse(
                        attempt=attempt,
                        exam_question=eq,
                        order=idx + 1,
                        is_visited=(idx == 0),  # First question marked visited on launch
                    )
                )
            ExamResponse.objects.bulk_create(responses)

            attempt.last_active_question = ordered_questions[0]
            attempt.save(update_fields=["last_active_question"])

            # Track freemium trial usage for students
            if user.is_student and not user.free_test_attempted:
                user.free_test_attempted = True
                user.save(update_fields=["free_test_attempted"])

            log_audit(
                user,
                "EXAM_SESSION_STARTED",
                f"Candidate started examination '{exam.title}' (Attempt #{attempt.id})",
                request=request,
            )

        return redirect("exams:exam_session", attempt_uuid=attempt.attempt_uuid)


class ExamSessionView(ActiveUserRequiredMixin, View):
    """
    Dedicated Candidate Computer-Based Testing (CBT) Interface.
    Full-screen test environment with timer, question renderer, palette,
    and anti-cheat monitoring. Correct answers and explanations are NEVER sent to the client.
    """

    def get(self, request, attempt_uuid):
        attempt = get_object_or_404(
            ExamAttempt.objects.select_related("exam", "student"),
            attempt_uuid=attempt_uuid,
        )

        # Strict security: Only the candidate who started this attempt can access it
        if attempt.student != request.user:
            raise PermissionDenied("You do not have permission to access another candidate's examination session.")

        # If expired or already submitted, redirect to completion page
        if attempt.status != ExamAttempt.AttemptStatus.IN_PROGRESS or attempt.is_expired:
            attempt.auto_timeout_if_expired()
            return redirect("exams:exam_completed", attempt_uuid=attempt.attempt_uuid)

        exam = attempt.exam

        # Fetch candidate responses with attached questions and options
        responses_qs = (
            attempt.responses.select_related(
                "exam_question__question",
                "selected_option",
            )
            .prefetch_related("exam_question__question__options")
            .order_by("order", "id")
        )

        # Prepare sanitized questions payload (SECURITY: never expose is_correct or explanation)
        questions_payload = []
        sections_map = {}

        for resp in responses_qs:
            eq = resp.exam_question
            q = eq.question

            # Prepare options (shuffled if exam requires)
            options_list = list(q.options.all())
            if exam.shuffle_options:
                random.shuffle(options_list)

            sanitized_options = []
            for opt in options_list:
                sanitized_options.append(
                    {
                        "id": opt.id,
                        "key": opt.option_key,
                        "text": opt.option_text,
                        "image_url": opt.image.url if opt.image else None,
                    }
                )

            section_name = eq.section or "General"
            sections_map[section_name] = sections_map.get(section_name, 0) + 1

            questions_payload.append(
                {
                    "response_id": resp.id,
                    "question_id": eq.id,
                    "order": resp.order,
                    "section": section_name,
                    "question_text": q.question_text,
                    "image_url": q.image.url if q.image else None,
                    "positive_marks": float(eq.positive_marks),
                    "negative_marks": float(eq.negative_marks),
                    "selected_option_id": resp.selected_option_id,
                    "is_marked_for_review": resp.is_marked_for_review,
                    "is_visited": resp.is_visited,
                    "palette_status": resp.palette_status,
                    "time_spent": resp.time_spent_seconds,
                    "options": sanitized_options,
                }
            )

        # Find initial question index (last active question or 0)
        initial_index = 0
        if attempt.last_active_question_id:
            for idx, q_data in enumerate(questions_payload):
                if q_data["question_id"] == attempt.last_active_question_id:
                    initial_index = idx
                    break

        context = {
            "attempt": attempt,
            "exam": exam,
            "questions_json": json.dumps(questions_payload),
            "sections": list(sections_map.keys()),
            "initial_index": initial_index,
            "remaining_seconds": attempt.remaining_seconds,
            "total_questions": len(questions_payload),
            "answered_count": attempt.answered_count,
            "not_answered_count": attempt.not_answered_count,
            "marked_review_count": attempt.marked_review_count,
            "not_visited_count": attempt.not_visited_count,
        }
        return render(request, "exams/exam_session.html", context)


class ExamSaveAnswerView(ActiveUserRequiredMixin, View):
    """
    Real-time AJAX endpoint for saving answers, clearing selections,
    marking questions for review, and tracking time spent.
    Enforces server-authoritative timer and prevents changes after expiry or submission.
    """

    def post(self, request, attempt_uuid):
        attempt = get_object_or_404(ExamAttempt, attempt_uuid=attempt_uuid)

        # Strict security check
        if attempt.student != request.user:
            return JsonResponse({"error": "Forbidden: Session ownership mismatch."}, status=403)

        # Server-authoritative timer check
        if attempt.is_expired or attempt.status != ExamAttempt.AttemptStatus.IN_PROGRESS:
            attempt.auto_timeout_if_expired()
            return JsonResponse(
                {
                    "status": "expired",
                    "message": "Your examination session time has expired.",
                    "redirect_url": reverse("exams:exam_completed", kwargs={"attempt_uuid": attempt.attempt_uuid}),
                },
                status=200,
            )

        try:
            data = json.loads(request.body)
        except (ValueError, TypeError):
            return JsonResponse({"error": "Invalid JSON payload."}, status=400)

        response_id = data.get("response_id")
        action = data.get("action", "save_next")  # save_next, clear, mark_review, navigate
        selected_option_id = data.get("selected_option_id")
        is_marked = data.get("is_marked_for_review", False)
        time_spent_delta = int(data.get("time_spent_delta", 0))

        with transaction.atomic():
            resp = (
                ExamResponse.objects.select_for_update()
                .filter(id=response_id, attempt=attempt)
                .first()
            )
            if not resp:
                return JsonResponse({"error": "Response record not found."}, status=404)

            # Mark visited
            resp.is_visited = True

            # Action processing
            if action == "clear":
                resp.selected_option = None
                resp.is_marked_for_review = False
            elif action == "mark_review":
                resp.is_marked_for_review = True
                if selected_option_id:
                    # Validate option belongs to this question
                    from apps.questions.models import QuestionOption
                    valid_opt = QuestionOption.objects.filter(
                        id=selected_option_id,
                        question=resp.exam_question.question,
                    ).first()
                    resp.selected_option = valid_opt
            else:
                # Normal save or navigate
                if selected_option_id:
                    from apps.questions.models import QuestionOption
                    valid_opt = QuestionOption.objects.filter(
                        id=selected_option_id,
                        question=resp.exam_question.question,
                    ).first()
                    resp.selected_option = valid_opt
                elif action == "save_next" and not selected_option_id:
                    # Saved without selecting an option
                    resp.selected_option = None

                resp.is_marked_for_review = bool(is_marked)

            # Accumulate time spent
            if time_spent_delta > 0:
                resp.time_spent_seconds += min(time_spent_delta, 600)

            resp.save()

            # Update candidate's last active question
            attempt.last_active_question = resp.exam_question
            attempt.save(update_fields=["last_active_question"])

        return JsonResponse(
            {
                "status": "success",
                "response_id": resp.id,
                "palette_status": resp.palette_status,
                "selected_option_id": resp.selected_option_id,
                "is_marked_for_review": resp.is_marked_for_review,
                "remaining_seconds": attempt.remaining_seconds,
                "counters": {
                    "answered": attempt.answered_count,
                    "not_answered": attempt.not_answered_count,
                    "marked_review": attempt.marked_review_count,
                    "not_visited": attempt.not_visited_count,
                },
            }
        )


class ExamSyncStatusView(ActiveUserRequiredMixin, View):
    """
    Heartbeat and timer synchronization endpoint.
    Keeps client countdown in sync with authoritative server time.
    Detects timeout independently of client browser activity.
    """

    def get(self, request, attempt_uuid):
        attempt = get_object_or_404(ExamAttempt, attempt_uuid=attempt_uuid)

        if attempt.student != request.user:
            return JsonResponse({"error": "Forbidden"}, status=403)

        if attempt.is_expired or attempt.status != ExamAttempt.AttemptStatus.IN_PROGRESS:
            attempt.auto_timeout_if_expired()
            return JsonResponse(
                {
                    "status": "expired",
                    "redirect_url": reverse("exams:exam_completed", kwargs={"attempt_uuid": attempt.attempt_uuid}),
                }
            )

        return JsonResponse(
            {
                "status": "active",
                "remaining_seconds": attempt.remaining_seconds,
                "server_time": timezone.now().isoformat(),
                "counters": {
                    "answered": attempt.answered_count,
                    "not_answered": attempt.not_answered_count,
                    "marked_review": attempt.marked_review_count,
                    "not_visited": attempt.not_visited_count,
                },
            }
        )


class ExamSecurityLogView(ActiveUserRequiredMixin, View):
    """
    Records anti-cheating signals (fullscreen exit, tab switch, window blur,
    copy-paste attempt, context menu right-click).
    Acts as a deterrent and monitoring audit trail without arbitrary disqualification.
    """

    def post(self, request, attempt_uuid):
        attempt = get_object_or_404(ExamAttempt, attempt_uuid=attempt_uuid)

        if attempt.student != request.user:
            return JsonResponse({"error": "Forbidden"}, status=403)

        if attempt.status != ExamAttempt.AttemptStatus.IN_PROGRESS:
            return JsonResponse({"status": "ignored"}, status=200)

        try:
            data = json.loads(request.body)
        except (ValueError, TypeError):
            return JsonResponse({"error": "Invalid payload"}, status=400)

        event_type = data.get("event_type", "WINDOW_BLUR")
        details = str(data.get("details", ""))[:250]

        valid_events = [choice[0] for choice in ExamSecurityLog.EventType.choices]
        if event_type not in valid_events:
            event_type = ExamSecurityLog.EventType.WINDOW_BLUR

        with transaction.atomic():
            attempt.anti_cheat_warnings += 1
            attempt.save(update_fields=["anti_cheat_warnings"])

            ExamSecurityLog.objects.create(
                attempt=attempt,
                event_type=event_type,
                details=details,
            )

        return JsonResponse(
            {
                "status": "recorded",
                "warning_count": attempt.anti_cheat_warnings,
                "event_type": event_type,
            }
        )


class ExamSubmitView(ActiveUserRequiredMixin, View):
    """
    Submits examination attempt atomically and evaluates candidate score.
    Prevents duplicate submissions via database locking.
    """

    def post(self, request, attempt_uuid):
        attempt = get_object_or_404(ExamAttempt, attempt_uuid=attempt_uuid)

        if attempt.student != request.user:
            raise PermissionDenied("You do not have permission to submit this examination.")

        is_timeout = bool(request.POST.get("is_timeout") == "true")
        if not is_timeout:
            # Check if JSON payload sent
            try:
                data = json.loads(request.body)
                is_timeout = bool(data.get("is_timeout", False))
            except Exception:
                pass

        with transaction.atomic():
            # Lock attempt record
            locked_attempt = ExamAttempt.objects.select_for_update().get(id=attempt.id)
            if locked_attempt.status == ExamAttempt.AttemptStatus.IN_PROGRESS:
                locked_attempt.submit_attempt(is_timeout=is_timeout)

                log_audit(
                    request.user,
                    "EXAM_SESSION_SUBMITTED",
                    f"Candidate submitted exam '{locked_attempt.exam.title}' (Score: {locked_attempt.score_obtained}/{locked_attempt.total_marks})",
                    request=request,
                )

        completed_url = reverse("exams:exam_completed", kwargs={"attempt_uuid": attempt.attempt_uuid})

        if request.headers.get("x-requested-with") == "XMLHttpRequest" or "application/json" in request.headers.get("Content-Type", ""):
            return JsonResponse({"status": "submitted", "redirect_url": completed_url})

        return redirect("exams:exam_completed", attempt_uuid=attempt.attempt_uuid)


class ExamCompletedView(ActiveUserRequiredMixin, DetailView):
    """
    Post-submission summary and verification screen.
    Displays completion status, time taken, question breakdown,
    and immediate score/result if configured by the institution.
    """

    model = ExamAttempt
    template_name = "exams/exam_completed.html"
    context_object_name = "attempt"
    slug_field = "attempt_uuid"
    slug_url_kwarg = "attempt_uuid"

    def get_object(self, queryset=None):
        attempt = super().get_object(queryset)
        if attempt.student != self.request.user:
            raise PermissionDenied("You cannot view another student's examination result.")

        # If accessed while still in progress and expired, auto-submit
        if attempt.status == ExamAttempt.AttemptStatus.IN_PROGRESS and attempt.is_expired:
            attempt.auto_timeout_if_expired()

        return attempt

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        attempt = self.object
        exam = attempt.exam

        context.update(
            {
                "exam": exam,
                "total_questions": attempt.total_questions_count,
                "answered_count": attempt.answered_count,
                "unanswered_count": attempt.total_questions_count - attempt.answered_count,
                "marked_review_count": attempt.marked_review_count,
                "time_taken_minutes": round(attempt.time_taken_seconds / 60, 1),
                "show_results": exam.show_result_immediately,
            }
        )
        return context

