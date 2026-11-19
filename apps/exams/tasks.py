"""Server-side expiry sweeps for private practice, classrooms and test series."""
from celery import shared_task
from django.utils import timezone

from .models import ExamAttempt


@shared_task
def expire_private_practice_attempts():
    """Run from Celery beat (or cron via the management command), even offline."""
    ids = ExamAttempt.objects.filter(
        is_private_practice=True, status=ExamAttempt.AttemptStatus.IN_PROGRESS,
        expires_at__lte=timezone.now(),
    ).values_list("pk", flat=True).iterator()
    count = 0
    for pk in ids:
        attempt = ExamAttempt.objects.filter(pk=pk).select_related("exam").first()
        if attempt and attempt.auto_timeout_if_expired():
            count += 1
    return count


@shared_task
def expire_series_attempts():
    """Finalize expired series attempts offline without touching ordinary exams."""
    ids = list(ExamAttempt.objects.filter(
        classroom_session__isnull=True, exam__is_private_practice=False,
        exam__series_slot__isnull=False, status=ExamAttempt.AttemptStatus.IN_PROGRESS,
        expires_at__lte=timezone.now(),
    ).values_list("pk", flat=True))
    count = 0
    for pk in ids:
        attempt = ExamAttempt.objects.filter(pk=pk).select_related("exam").first()
        if attempt and attempt.auto_timeout_if_expired():
            count += 1
    return count


@shared_task
def expire_classroom_attempts():
    """Finalize expired classroom attempts even when the student is offline."""
    ids = list(ExamAttempt.objects.filter(
        classroom_session__isnull=False, status=ExamAttempt.AttemptStatus.IN_PROGRESS,
        expires_at__lte=timezone.now(),
    ).values_list("pk", flat=True))
    count = 0
    for pk in ids:
        attempt = ExamAttempt.objects.filter(pk=pk).select_related("exam").first()
        if attempt and attempt.auto_timeout_if_expired():
            count += 1
    return count
