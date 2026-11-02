"""Server-enforced plan entitlements and transactional calendar-month quotas."""
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from .models import PracticeEntitlement, PracticePlan, PracticeUsage


class QuotaExceeded(Exception):
    pass


class FeatureUnavailable(Exception):
    pass


def current_entitlement(user):
    now = timezone.now()
    return (PracticeEntitlement.objects.select_related("plan", "checkout")
            .filter(user=user, is_revoked=False, starts_at__lte=now, valid_until__gt=now)
            .order_by("-starts_at", "-pk").first())


def current_plan(user):
    entitlement = current_entitlement(user)
    if entitlement:
        return entitlement.plan
    plan = PracticePlan.objects.filter(is_default=True, billing=PracticePlan.Billing.FREE).first()
    if not plan:
        raise ImproperlyConfigured("Configure exactly one default free practice plan.")
    return plan


def month_start():
    return timezone.localdate().replace(day=1)


def usage_summary(user):
    plan = current_plan(user)
    usage = PracticeUsage.objects.filter(user=user, period=month_start()).first()
    counters = []
    for label, field, limit_field in (("PDF uploads", "pdf_uploads", "monthly_pdf_uploads"),
                                       ("Tests created", "test_creations", "monthly_test_creations"),
                                       ("Attempts started", "attempts", "monthly_attempts")):
        used = getattr(usage, field, 0) if usage else 0
        limit = getattr(plan, limit_field)
        counters.append({"label": label, "used": used, "limit": limit,
                         "remaining": max(0, limit - used) if limit is not None else None,
                         "percent": min(100, round(100 * used / limit)) if limit else (100 if limit == 0 else 0)})
    return {"plan": plan, "entitlement": current_entitlement(user), "counters": counters, "period": month_start()}


def require_feature(user, feature):
    allowed = {"bank": "allow_bank", "pdf_reports": "allow_pdf_reports", "cloud_extraction": "allow_cloud_extraction"}
    if feature not in allowed:
        raise ValueError("Unknown plan feature")
    if not getattr(current_plan(user), allowed[feature]):
        raise FeatureUnavailable("This feature is not included in your current practice plan. See Plans & usage to upgrade.")


def lock_practice_user(user):
    """Serialize starts and sensitive reads, including on SQLite development DBs."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("lock_practice_user() must be inside transaction.atomic()")
    users = get_user_model().objects.filter(pk=user.pk)
    if connection.vendor == "sqlite":
        # SQLite ignores SELECT FOR UPDATE. A no-op update obtains its writer
        # lock before checking active attempts; the stored value is unchanged.
        if not users.update(last_login=F("last_login")):
            raise get_user_model().DoesNotExist()
    else:
        users.select_for_update().get()


def consume(user, kind):
    """Call inside the same atomic transaction as the operation being charged.

    A user lock serializes concurrent actions on databases supporting row locks;
    the conditional increment is also an atomic DB-level limit backstop.
    Deleting a paper/test never refunds its already consumed monthly quota.
    """
    fields = {"pdf_uploads": "monthly_pdf_uploads", "test_creations": "monthly_test_creations", "attempts": "monthly_attempts"}
    if kind not in fields:
        raise ValueError("Unknown usage counter")
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("consume() must be inside transaction.atomic()")
    lock_practice_user(user)
    plan = current_plan(user)
    limit = getattr(plan, fields[kind])
    usage, _ = PracticeUsage.objects.get_or_create(user=user, period=month_start())
    qs = PracticeUsage.objects.filter(pk=usage.pk)
    if limit is not None:
        qs = qs.filter(**{f"{kind}__lt": limit})
    if not qs.update(**{kind: F(kind) + 1}):
        raise QuotaExceeded(f"Your monthly {kind.replace('_', ' ')} limit has been reached. See Plans & usage.")


def private_attempt_limit(user, exam):
    plan_limit = current_plan(user).attempts_per_test
    test_limit = exam.max_attempts or None
    values = [x for x in (plan_limit, test_limit) if x is not None]
    return min(values) if values else None
