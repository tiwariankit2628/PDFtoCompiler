import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditLog(models.Model):
    """
    Tracks administrative and operational activities for governance,
    compliance, security, and auditing.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    action = models.CharField("Action Type", max_length=80, db_index=True)
    description = models.TextField("Action Details")
    ip_address = models.GenericIPAddressField("IP Address", null=True, blank=True)
    created_at = models.DateTimeField("Timestamp", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"
        ordering = ["-created_at"]

    def __str__(self):
        username = self.user.username if self.user else "System"
        return f"[{self.created_at:%Y-%m-%d %H:%M}] {username}: {self.action}"


class FounderAccess(models.Model):
    """Independent, explicitly provisioned founder authorization.

    This is deliberately not registered in Django admin or exposed by any
    account form. A role or Django-superuser flag alone never creates a row.
    The encrypted TOTP seed requires a separate environment-held Fernet key.
    """
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="founder_access")
    encrypted_totp_secret = models.TextField(editable=False)
    access_version = models.UUIDField(default=uuid.uuid4, editable=False)
    is_enabled = models.BooleanField(default=False)
    last_accepted_step = models.BigIntegerField(default=-1, editable=False)
    failed_attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    locked_until = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ActivityTrackingState(models.Model):
    """When new sign-in event collection began; no historical backfill."""
    key = models.CharField(max_length=32, primary_key=True)
    started_at = models.DateTimeField(default=timezone.now)


class LoginEvent(models.Model):
    """A successful application sign-in, including sign-in after signup.

    No password, OTP, IP, device identifier, or OAuth token is recorded. Role
    and track are snapshots, so later profile edits do not rewrite history.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                             null=True, blank=True, related_name="recorded_sign_ins")
    role_snapshot = models.CharField(max_length=20, blank=True)
    track_snapshot = models.CharField(max_length=12, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        indexes = [models.Index(fields=["user", "-occurred_at"], name="login_user_occurred_idx")]

