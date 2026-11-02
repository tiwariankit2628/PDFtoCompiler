from django.conf import settings
from django.db import models


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

