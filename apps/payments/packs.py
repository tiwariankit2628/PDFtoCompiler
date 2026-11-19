"""Lifetime credit products. All mutations are serialized on the account row.

These products use test-mode payments only. A captured TEST payment is usable
only in an explicitly enabled sandbox, never automatically in production.
"""
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from .models import CreditGrant, CreditUse
from .services import QuotaExceeded, consume, current_entitlement, lock_practice_user


@dataclass(frozen=True)
class Product:
    code: str
    name: str
    amount_paise: int
    units: int
    role: str
    detail: str

    @property
    def price_rupees(self):
        return f"{self.amount_paise // 100}.{self.amount_paise % 100:02d}"


PRODUCTS = {
    "pdf": Product("pdf", "PDF practice pack", 29900, 20, "STUDENT", "20 additional PDF extractions"),
    "classroom": Product("classroom", "Classroom student pack", 9900, 20, "STUDENT", "20 additional classroom test starts"),
    "activation": Product("activation", "Classroom teacher pack", 29900, 20, "TEACHER", "20 additional 24-hour test activations"),
    "series_jee": Product("series_jee", "JEE test series", 29900, 1, "STUDENT", "Unlock tests 2–16 of the JEE series"),
    "series_neet": Product("series_neet", "NEET test series", 29900, 1, "STUDENT", "Unlock tests 2–16 of the NEET series"),
}
FREE_KINDS = frozenset(("pdf", "classroom", "activation"))


def eligible_products(user):
    return [p for p in PRODUCTS.values() if p.role == user.role and
            (not p.code.startswith("series_") or p.code == f"series_{user.exam_track}")]


def usable_grants(user, kind):
    qs = CreditGrant.objects.filter(user=user, product=kind, is_revoked=False)
    # Every grant created by the current gateway is a test grant. Switching a
    # database from sandbox to production cannot turn test money into access.
    if not settings.ALLOW_TEST_PAYMENT_CREDITS:
        qs = qs.filter(is_test=False)
    return qs


def has_series_access(user, track):
    return usable_grants(user, f"series_{track}").filter(remaining__gt=0).exists()


def balances(user):
    kinds = ("pdf", "classroom") if user.is_student else ("activation",) if user.is_teacher else ()
    rows = []
    for kind in kinds:
        free = not CreditUse.objects.filter(user=user, kind=kind, is_free=True, reversed_at__isnull=True).exists()
        paid = usable_grants(user, kind).aggregate(n=Sum("remaining"))["n"] or 0
        rows.append({"kind": kind, "name": PRODUCTS[kind].name, "free": free, "paid": paid})
    return rows


def debit(user, kind, resource_key):
    """Debit one free or paid unit inside the operation's transaction.

    All application callers lock the same user, so two concurrent actions can
    neither use the same free allowance nor overspend a pack. A transaction
    rollback also rolls back the debit. Each source is auditable.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("debit must be called inside transaction.atomic()")
    if kind not in FREE_KINDS or PRODUCTS[kind].role != user.role:
        raise ValueError("Invalid credit kind for this role")
    user = lock_practice_user(user)
    if not user.is_active or user.needs_email_verification or PRODUCTS[kind].role != user.role:
        raise PermissionDenied("An active account with a verified email is required.")
    if not CreditUse.objects.filter(user=user, kind=kind, is_free=True, reversed_at__isnull=True).exists():
        return CreditUse.objects.create(user=user, kind=kind, is_free=True, resource_key=resource_key)
    grant = usable_grants(user, kind).select_for_update().filter(remaining__gt=0).order_by("created_at", "pk").first()
    if grant:
        if not CreditGrant.objects.filter(pk=grant.pk, remaining__gt=0, is_revoked=False).update(remaining=F("remaining") - 1):
            raise QuotaExceeded("The credit balance changed. Please retry.")
        return CreditUse.objects.create(user=user, kind=kind, grant=grant, resource_key=resource_key)
    # Honor already-paid legacy PDF subscription terms until they expire. New
    # monthly plans are no longer sold. A legacy debit remains in its old usage
    # ledger; it is never presented as an unexpiring new pack.
    if kind == "pdf" and current_entitlement(user):
        try:
            with transaction.atomic():
                consume(user, "pdf_uploads")
            return CreditUse.objects.create(user=user, kind=kind, resource_key=resource_key)
        except QuotaExceeded:
            pass
    label = {"pdf": "PDF extraction", "classroom": "classroom test start", "activation": "classroom activation"}[kind]
    raise QuotaExceeded(f"Your free {label} has been used and no credits remain. See Packs & usage.")


def reverse_failed_pdf(resource_key):
    """Return a unit only for a confirmed, failed/unqueued local extraction.

    A previously reversed record remains for audit. Legacy monthly debits are
    not altered because their original period/terms may have changed; support
    can reconcile them manually.
    """
    prefix, separator, paper_id = resource_key.partition(":")
    if prefix != "pdf" or not separator or not paper_id.isdecimal():
        return False
    initial = CreditUse.objects.filter(resource_key=resource_key, kind="pdf").first()
    if not initial:
        return False
    with transaction.atomic():
        user = lock_practice_user(initial.user)
        use = CreditUse.objects.select_for_update().get(pk=initial.pk)
        # Do not refund a successful/ongoing job, a deleted paper, or any job
        # that preserved partial review work. A worker retry or support action
        # can call this again safely after a confirmed failure.
        from apps.questions.models import Question, QuestionPaperUpload
        paper = QuestionPaperUpload.objects.select_for_update().filter(
            pk=int(paper_id), uploaded_by=user, is_student_private=True,
            status=QuestionPaperUpload.ProcessingStatus.FAILED,
        ).first()
        if (not paper or paper.practice_exam_id or paper.extracted_questions.exists() or paper.figures.exists()
                or Question.objects.filter(source_paper=paper).exists()
                or use.reversed_at is not None or (not use.is_free and not use.grant_id)):
            return False
        if use.grant_id:
            CreditGrant.objects.filter(pk=use.grant_id).update(remaining=F("remaining") + 1)
        use.reversed_at = timezone.now()
        use.save(update_fields=["reversed_at"])
        return True
