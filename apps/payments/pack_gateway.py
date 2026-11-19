"""Razorpay TEST order verification and exactly-once pack accounting."""
import re
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum

from apps.core.utils import log_audit
from . import gateway
from .gateway import InvalidPayment
from .models import CreditGrant, PackCheckout, PackPayment, PackRefund, Payment
from .packs import PRODUCTS
from .services import lock_practice_user


def create_provider_order(checkout):
    api = gateway.client()
    obj = api.order.create({"amount": checkout.amount_paise, "currency": "INR",
                            "receipt": str(checkout.reference), "payment_capture": 1}, timeout=15)
    if (not isinstance(obj, dict) or not re.fullmatch(r"order_[A-Za-z0-9]+", str(obj.get("id", "")))
            or obj.get("amount") != checkout.amount_paise or obj.get("currency") != checkout.currency):
        raise InvalidPayment("Invalid provider order.")
    checkout.provider_order_id = obj["id"]
    checkout.status = PackCheckout.Status.CREATED
    checkout.save(update_fields=["provider_order_id", "status", "updated_at"])


def _validate_capture_snapshot(checkout, payment_data, *, refund_recovery=False):
    product = PRODUCTS.get(checkout.product)
    if not product or checkout.units != product.units or checkout.amount_paise <= 0 or checkout.currency != "INR":
        raise InvalidPayment("Invalid product snapshot.")
    if (not isinstance(payment_data, dict)
            or not re.fullmatch(r"pay_[A-Za-z0-9]+", str(payment_data.get("id", "")))
            or payment_data.get("status") != "captured"
            or ("captured" in payment_data and payment_data["captured"] is not True)
            or payment_data.get("amount") != checkout.amount_paise
            or payment_data.get("currency") != checkout.currency
            or not checkout.provider_order_id
            or payment_data.get("order_id") != checkout.provider_order_id):
        raise InvalidPayment("Payment is not captured for this order, amount and currency.")
    refunded = payment_data.get("amount_refunded", 0)
    refund_status = payment_data.get("refund_status")
    if (type(refunded) is not int or not 0 <= refunded <= checkout.amount_paise
            or refund_status not in (None, "", "partial", "full")
            or (refund_status == "partial" and not 0 < refunded < checkout.amount_paise)
            or (refund_status == "full" and refunded != checkout.amount_paise)
            or (not refund_recovery and (refunded > 0 or refund_status not in (None, "")))):
        raise InvalidPayment("Payment reports a refund or an inconsistent refund snapshot.")


def activate_capture(checkout_id, payment_data, request=None, *, refund_recovery=False):
    if not settings.ALLOW_TEST_PAYMENT_CREDITS:
        raise InvalidPayment("Test payment credits are disabled outside a sandbox.")
    with transaction.atomic():
        initial = PackCheckout.objects.select_related("user").get(pk=checkout_id)
        lock_practice_user(initial.user)
        checkout = PackCheckout.objects.select_for_update().get(pk=checkout_id)
        # Only a signed refund webhook or a verified API refund observation may
        # bypass this check, and then only to revoke in the same transaction.
        _validate_capture_snapshot(checkout, payment_data, refund_recovery=refund_recovery)
        if checkout.status == PackCheckout.Status.REFUNDED:
            raise InvalidPayment("A refunded order cannot be reactivated.")
        if Payment.objects.filter(provider_payment_id=payment_data["id"]).exists():
            raise InvalidPayment("Payment belongs to another billing record.")
        prior = checkout.payments.exclude(status=Payment.Status.FAILED).first()
        if prior and prior.provider_payment_id != payment_data["id"]:
            raise InvalidPayment("An order can only grant once.")
        try:
            payment, created = PackPayment.objects.get_or_create(provider_payment_id=payment_data["id"],
                defaults={"checkout": checkout, "amount_paise": checkout.amount_paise, "currency": checkout.currency})
        except IntegrityError:
            raise InvalidPayment("Payment is already linked elsewhere.") from None
        if (payment.checkout_id != checkout.pk or payment.amount_paise != checkout.amount_paise
                or payment.currency != checkout.currency):
            raise InvalidPayment("Payment belongs to a different checkout or amount.")
        if not created and payment.status == Payment.Status.FAILED:
            # Razorpay can capture the SAME payment after an earlier failure
            # (late authorization or retry). A signed capture/API fetch still
            # has to pass every order, amount and currency check above.
            payment.status = Payment.Status.CAPTURED
            payment.save(update_fields=["status"])
            created = True
        elif payment.status != Payment.Status.CAPTURED:
            raise InvalidPayment("A refunded payment cannot be replayed.")
        if not created:
            if not CreditGrant.objects.filter(checkout=checkout).exists():
                raise InvalidPayment("Payment requires reconciliation.")
            return checkout
        CreditGrant.objects.create(checkout=checkout, user=checkout.user, product=checkout.product,
                                   units=checkout.units, remaining=checkout.units, is_test=True)
        checkout.status = PackCheckout.Status.PAID
        checkout.save(update_fields=["status", "updated_at"])
        log_audit(checkout.user, "PACK_TEST_PAYMENT_CAPTURED", f"Verified test payment for {checkout.reference}.", request=request)
        return checkout


def verify_checkout(checkout, payment_id, order_id, signature, request=None):
    if (not re.fullmatch(r"pay_[A-Za-z0-9]+", payment_id or "")
            or not re.fullmatch(r"[0-9a-fA-F]{64}", signature or "")
            or order_id != checkout.provider_order_id):
        raise InvalidPayment("Invalid payment confirmation.")
    api = gateway.client()
    api.utility.verify_payment_signature({"razorpay_order_id": checkout.provider_order_id,
        "razorpay_payment_id": payment_id, "razorpay_signature": signature})
    payment = api.payment.fetch(payment_id, timeout=15)
    if not isinstance(payment, dict) or payment.get("id") != payment_id:
        raise InvalidPayment("Fetched payment does not match.")
    refunded = payment.get("amount_refunded", 0)
    if type(refunded) is not int or refunded < 0:
        raise InvalidPayment("Invalid provider refund amount.")
    if refunded > 0:
        # The API fetch is the latest provider state, unlike an old capture
        # webhook. Persist a revoked marker before reporting pending, so a late
        # capture cannot grant access while the refund event is in flight.
        suspend_reported_refund(checkout.pk, payment, request=request)
        raise InvalidPayment("Provider reports a refund; await reconciliation.")
    return activate_capture(checkout.pk, payment, request=request)


def suspend_reported_refund(checkout_id, payment_data, request=None):
    """Freeze a verified API-reported refund without inventing refund records.

    This is only called after checkout signature verification and an API fetch.
    Its capture marker and revocation commit atomically. Individual refund IDs
    are recorded later, from separately signed refund.processed webhooks.
    """
    if not settings.ALLOW_TEST_PAYMENT_CREDITS or payment_data.get("captured") is not True:
        raise InvalidPayment("Provider did not confirm a captured payment.")
    if payment_data.get("status") not in ("captured", "refunded"):
        raise InvalidPayment("Invalid refunded payment state.")
    with transaction.atomic():
        initial = PackCheckout.objects.select_related("user").get(pk=checkout_id)
        lock_practice_user(initial.user)
        checkout = PackCheckout.objects.select_for_update().get(pk=checkout_id)
        normalized = {**payment_data, "status": "captured"}
        _validate_capture_snapshot(checkout, normalized, refund_recovery=True)
        refunded = normalized.get("amount_refunded")
        if type(refunded) is not int or not 0 < refunded <= checkout.amount_paise:
            raise InvalidPayment("Invalid provider refund amount.")
        if Payment.objects.filter(provider_payment_id=normalized["id"]).exists():
            raise InvalidPayment("Payment belongs to another billing record.")
        payment = PackPayment.objects.select_for_update().filter(provider_payment_id=normalized["id"]).first()
        if payment and (payment.checkout_id != checkout.pk or payment.amount_paise != checkout.amount_paise
                        or payment.currency != checkout.currency):
            raise InvalidPayment("Payment belongs to a different checkout or amount.")
        if not payment or payment.status in (Payment.Status.FAILED, Payment.Status.CAPTURED):
            activate_capture(checkout.pk, normalized, request=request, refund_recovery=True)
            payment = PackPayment.objects.select_for_update().get(provider_payment_id=normalized["id"])
        elif payment.status not in (Payment.Status.PARTIAL_REFUND, Payment.Status.REFUNDED):
            raise InvalidPayment("Invalid provider payment state.")
        grant = CreditGrant.objects.select_for_update().filter(checkout=checkout).first()
        if not grant:
            raise InvalidPayment("Payment requires reconciliation.")
        desired = (Payment.Status.REFUNDED if refunded == checkout.amount_paise or payment.status == Payment.Status.REFUNDED
                   else Payment.Status.PARTIAL_REFUND)
        changed = not grant.is_revoked or payment.status != desired
        if not grant.is_revoked:
            grant.is_revoked = True
            grant.save(update_fields=["is_revoked"])
        if payment.status != desired:
            payment.status = desired
            payment.save(update_fields=["status"])
        if desired == Payment.Status.REFUNDED and checkout.status != PackCheckout.Status.REFUNDED:
            checkout.status = PackCheckout.Status.REFUNDED
            checkout.save(update_fields=["status", "updated_at"])
            changed = True
        if changed:
            log_audit(checkout.user, "PACK_TEST_REFUND_OBSERVED",
                      f"Provider reports a refund for {checkout.reference}; refund details await reconciliation.", request=request)


def record_failure(checkout, data, request=None):
    # Use the same account -> checkout lock order as capture and refund. A
    # failed event can race a capture for the SAME provider payment ID.
    with transaction.atomic():
        lock_practice_user(checkout.user)
        checkout = PackCheckout.objects.select_for_update().get(pk=checkout.pk)
        if (not isinstance(data, dict) or not re.fullmatch(r"pay_[A-Za-z0-9]+", str(data.get("id", "")))
                or data.get("status") != "failed" or data.get("order_id") != checkout.provider_order_id
                or data.get("amount") != checkout.amount_paise or data.get("currency") != checkout.currency):
            raise InvalidPayment("Invalid failed payment.")
        payment, created = PackPayment.objects.get_or_create(provider_payment_id=data["id"],
            defaults={"checkout": checkout, "amount_paise": checkout.amount_paise,
                      "currency": checkout.currency, "status": Payment.Status.FAILED})
        if (payment.checkout_id != checkout.pk or payment.amount_paise != checkout.amount_paise
                or payment.currency != checkout.currency):
            raise InvalidPayment("Payment belongs to a different checkout or amount.")
        if created:
            log_audit(checkout.user, "PACK_TEST_PAYMENT_FAILED", f"Test payment failed for {checkout.reference}.", request=request)


def apply_refund(payment_id, data, request=None):
    initial = PackPayment.objects.select_related("checkout__user").filter(provider_payment_id=payment_id).first()
    if not initial:
        raise InvalidPayment("Payment for refund not found.")
    with transaction.atomic():
        lock_practice_user(initial.checkout.user)
        payment = PackPayment.objects.select_for_update().select_related("checkout").get(pk=initial.pk)
        refund_id, amount = data.get("id", ""), data.get("amount")
        if (not re.fullmatch(r"rfnd_[A-Za-z0-9]+", str(refund_id)) or type(amount) is not int or amount <= 0
                or data.get("payment_id") != payment.provider_payment_id
                or data.get("currency") != payment.currency or data.get("status") != "processed"
                or payment.status == Payment.Status.FAILED):
            raise InvalidPayment("Invalid refund.")
        refund, created = PackRefund.objects.get_or_create(provider_refund_id=refund_id,
            defaults={"payment": payment, "amount_paise": amount})
        if refund.payment_id != payment.pk or refund.amount_paise != amount:
            raise InvalidPayment("Refund does not match its original record.")
        if not created:
            return
        total = payment.refunds.aggregate(total=Sum("amount_paise"))["total"] or 0
        if total > payment.amount_paise:
            raise InvalidPayment("Refund exceeds payment amount.")
        # A verified API fetch may already have reported a full refund while
        # its individual webhook events arrive out of order. Never downgrade it.
        payment.status = (Payment.Status.REFUNDED if total == payment.amount_paise or payment.status == Payment.Status.REFUNDED
                          else Payment.Status.PARTIAL_REFUND)
        payment.save(update_fields=["status"])
        # Any refund freezes unused units and series access pending support
        # reconciliation. Previously started attempts and past results survive.
        CreditGrant.objects.filter(checkout=payment.checkout).update(is_revoked=True)
        if total == payment.amount_paise:
            payment.checkout.status = PackCheckout.Status.REFUNDED
            payment.checkout.save(update_fields=["status", "updated_at"])
        log_audit(payment.checkout.user, "PACK_TEST_PAYMENT_REFUNDED", f"Refund recorded for {payment.checkout.reference}.", request=request)


def refunded_capture_replay(checkout, payment_data):
    """Acknowledge an old, valid capture snapshot without reviving a refund."""
    try:
        _validate_capture_snapshot(checkout, payment_data, refund_recovery=True)
    except InvalidPayment:
        return False
    return (PackPayment.objects.filter(checkout=checkout, provider_payment_id=payment_data["id"],
                amount_paise=checkout.amount_paise, currency=checkout.currency,
                status__in=[Payment.Status.PARTIAL_REFUND, Payment.Status.REFUNDED]).exists()
            and CreditGrant.objects.filter(checkout=checkout, is_revoked=True).exists())


@transaction.atomic
def apply_webhook_refund(refund_data, payment_data, request=None):
    """Apply a signed pack refund, including refund-before-capture ordering.

    Return False when this is not a known pack payment/order, allowing the
    retained legacy handler to try it. The caller verifies the webhook HMAC.
    A refund's signed payment snapshot may prove that a payment was captured
    before becoming 'refunded'. In that case create the capture and revoke its
    grant in the SAME transaction, so no unrefunded access becomes visible.
    Never infer a capture for an uncaptured automatic refund.
    """
    if not isinstance(refund_data, dict) or not isinstance(payment_data, dict):
        raise InvalidPayment("Invalid refund payload.")
    payment_id = refund_data.get("payment_id")
    if not isinstance(payment_id, str) or not re.fullmatch(r"pay_[A-Za-z0-9]+", payment_id):
        raise InvalidPayment("Invalid refund payment ID.")
    existing = PackPayment.objects.select_related("checkout").filter(provider_payment_id=payment_id).first()
    checkout = existing.checkout if existing else None
    if not checkout and payment_data.get("id") == payment_id:
        order_id = payment_data.get("order_id")
        if isinstance(order_id, str) and re.fullmatch(r"order_[A-Za-z0-9]+", order_id):
            checkout = PackCheckout.objects.filter(provider_order_id=order_id).first()
    if not checkout:
        return False
    if payment_data and (payment_data.get("id") != payment_id
            or payment_data.get("order_id") != checkout.provider_order_id
            or payment_data.get("amount") != checkout.amount_paise or payment_data.get("currency") != checkout.currency
            or ("captured" in payment_data and payment_data["captured"] is not True)
            or payment_data.get("status") not in ("captured", "refunded")):
        raise InvalidPayment("Refund payment snapshot does not match the checkout.")
    if not existing or existing.status == Payment.Status.FAILED:
        if (payment_data.get("id") != payment_id or payment_data.get("captured") is not True
                or payment_data.get("status") not in ("captured", "refunded")):
            raise InvalidPayment("A refund alone does not establish a captured payment.")
        # This is a signed historical capture, not a new provider payment. All
        # ordinary capture validation still runs, and the subsequent refund is
        # inside the same transaction (including the event marker in the view).
        captured = {**payment_data, "status": "captured"}
        activate_capture(checkout.pk, captured, request=request, refund_recovery=True)
    apply_refund(payment_id, refund_data, request=request)
    return True
