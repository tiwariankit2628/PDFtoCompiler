"""24-hour teacher activation windows, independent of classroom codes."""
from datetime import timedelta
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from apps.payments.packs import debit
from apps.payments.services import lock_practice_user
from .models import ClassroomActivation


def ensure_activation(user, exam):
    """Within a transaction, reuse an available window or debit one new unit."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Activation requires an atomic transaction")
    user = lock_practice_user(user)
    if not user.is_active or not (user.is_teacher or user.is_admin_role) or user.needs_email_verification:
        raise PermissionDenied("An active, verified teacher or administrator is required.")
    now = timezone.now()
    candidates = (ClassroomActivation.objects.select_for_update(of=("self",)).filter(teacher=user, exam=exam,
                  starts_at__lte=now, ends_at__gt=now).select_related("credit_use__grant").order_by("-ends_at"))
    for candidate in candidates:
        if candidate.available:
            return candidate
    activation = ClassroomActivation.objects.create(teacher=user, exam=exam, starts_at=now,
                                                     ends_at=now + timedelta(hours=24))
    if user.is_teacher:
        activation.credit_use = debit(user, "activation", f"activation:{activation.pk}")
        activation.save(update_fields=["credit_use"])
    # Administrators can operate sessions without a teacher pack; no fake debit.
    return activation


def locked_activation_available(session):
    """Serialize a new student start with a refund of the activation's grant."""
    if not session.activation_id:
        return True  # unmigrated/legacy direct records only
    from django.conf import settings
    from apps.payments.models import CreditGrant
    activation = ClassroomActivation.objects.select_related("credit_use").get(pk=session.activation_id)
    now = timezone.now()
    if not activation.starts_at <= now < activation.ends_at:
        return False
    if activation.credit_use_id and activation.credit_use.grant_id:
        grant = CreditGrant.objects.select_for_update().get(pk=activation.credit_use.grant_id)
        return not grant.is_revoked and (not grant.is_test or settings.ALLOW_TEST_PAYMENT_CREDITS)
    return True
