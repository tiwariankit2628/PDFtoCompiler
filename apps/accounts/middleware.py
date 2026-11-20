"""Gate new unverified accounts while never interrupting a running test."""
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.cache import patch_cache_control


class EmailVerificationMiddleware:
    # Ownership checks remain in the target views. A previously active attempt
    # and its assets must remain usable even after an email address changes.
    ALLOWED = {
        "accounts:verification_pending", "accounts:verification_resend", "accounts:verify_email",
        "accounts:google_start", "accounts:google_callback", "accounts:google_signup",
        "accounts:logout", "accounts:profile",
        "exams:exam_session", "exams:exam_save_answer", "exams:exam_sync_status",
        "exams:exam_log_event", "exams:exam_submit", "exams:exam_completed",
        "exams:classroom_attempt_figure", "exams:classroom_attempt_image",
        "exams:series_attempt_figure", "exams:series_attempt_image",
        "exams:student_result_detail", "exams:student_result_review", "exams:student_result_pdf",
        "questions:practice_bank_image", "questions:practice_bank_figure",
        "questions:practice_result", "questions:practice_report",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = request.user
        if (not user.is_authenticated or not user.needs_email_verification
                or request.resolver_match.view_name in self.ALLOWED):
            return None
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.content_type == "application/json":
            response = JsonResponse({"error": "Verify your email before starting new work."}, status=403)
        else:
            response = redirect("accounts:verification_pending")
        patch_cache_control(response, private=True, no_store=True)
        return response
