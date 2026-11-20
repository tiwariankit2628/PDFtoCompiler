"""Minimal, forward-only sign-in tracking; never backfill from last_login."""
import logging

from django.contrib.auth.signals import user_logged_in
from django.db import DatabaseError, transaction
from django.dispatch import receiver

from .models import LoginEvent

logger = logging.getLogger(__name__)


@receiver(user_logged_in, dispatch_uid="core.record_successful_sign_in")
def record_successful_sign_in(sender, request, user, **kwargs):
    if not user or not user.pk:
        return
    try:
        # A missing migration or temporary logging failure must not prevent a
        # student from logging in/resuming a test. The savepoint keeps an outer
        # transaction usable; operators must monitor this warning for gaps.
        with transaction.atomic():
            LoginEvent.objects.create(user_id=user.pk, role_snapshot=user.role,
                                      track_snapshot=user.exam_track or "")
    except DatabaseError:
        logger.error("A successful sign-in could not be recorded; check the activity database and migrations.")
