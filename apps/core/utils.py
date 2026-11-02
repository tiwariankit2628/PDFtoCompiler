from .models import AuditLog


def log_audit(user, action, description, request=None):
    """
    Utility function to record audit log events.
    """
    ip = None
    if request:
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            ip = x_forwarded_for.split(",")[0].strip()
        else:
            ip = request.META.get("REMOTE_ADDR")

    return AuditLog.objects.create(
        user=user if user and user.is_authenticated else None,
        action=action,
        description=description,
        ip_address=ip,
    )

