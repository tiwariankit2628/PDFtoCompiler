"""Razorpay test-mode gateway and idempotent, verified entitlement activation."""
import re
from datetime import datetime, timedelta, timezone as dt_timezone

import razorpay
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.utils import log_audit
from .models import Checkout, Payment, PracticeEntitlement, PracticePlan, Refund
from .services import lock_practice_user


class GatewayUnavailable(Exception):
    pass


class InvalidPayment(Exception):
    pass


def gateway_ready(require_webhook=False):
    key = settings.RAZORPAY_KEY_ID or ""
    secret = settings.RAZORPAY_KEY_SECRET or ""
    webhook = settings.RAZORPAY_WEBHOOK_SECRET or ""
    # Never accept live keys, defaults, or obvious example values.
    invalid = (not settings.ALLOW_TEST_PAYMENT_CREDITS
               or not re.fullmatch(r"rzp_test_[A-Za-z0-9]+", key)
               or not secret.strip() or any(x in secret.lower() for x in ("placeholder", "changeme", "example")))
    if require_webhook:
        invalid = invalid or not webhook.strip() or any(x in webhook.lower() for x in ("placeholder", "changeme", "example"))
    return not invalid


def client():
    if not gateway_ready():
        raise GatewayUnavailable("Razorpay test mode is not configured.")
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def create_provider_checkout(checkout):
    api = client()
    if checkout.billing == PracticePlan.Billing.ONE_TIME:
        obj = api.order.create({"amount": checkout.amount_paise, "currency": checkout.currency,
                                "receipt": str(checkout.reference), "payment_capture": 1}, timeout=15)
        if (not isinstance(obj, dict) or not str(obj.get("id", "")).startswith("order_")
                or obj.get("amount") != checkout.amount_paise or obj.get("currency") != checkout.currency):
            raise InvalidPayment("Invalid provider order.")
        checkout.provider_order_id = obj["id"]
    elif checkout.billing == PracticePlan.Billing.RECURRING:
        provider_plan = api.plan.fetch(checkout.provider_plan_id, timeout=15)
        item = provider_plan.get("item", {}) if isinstance(provider_plan, dict) else {}
        if (not isinstance(provider_plan, dict) or provider_plan.get("id") != checkout.provider_plan_id
                or item.get("amount") != checkout.amount_paise or item.get("currency") != checkout.currency
                or provider_plan.get("period") != checkout.billing_period
                or provider_plan.get("interval") != checkout.billing_interval):
            raise InvalidPayment("The configured Razorpay plan does not match this price.")
        obj = api.subscription.create({"plan_id": checkout.provider_plan_id,
                                       "total_count": checkout.recurring_cycles,
                                       "customer_notify": 0,
                                       "notes": {"checkout_reference": str(checkout.reference)}}, timeout=15)
        if (not isinstance(obj, dict) or not str(obj.get("id", "")).startswith("sub_")
                or obj.get("plan_id") != checkout.provider_plan_id):
            raise InvalidPayment("Invalid provider subscription.")
        checkout.provider_subscription_id = obj["id"]
    else:
        raise InvalidPayment("Free plans do not need checkout.")
    checkout.status = Checkout.Status.CREATED
    checkout.save(update_fields=["provider_order_id", "provider_subscription_id", "status", "updated_at"])
    return checkout


def _validate_payment(checkout, payment, *, refund_recovery=False):
    if not isinstance(payment, dict):
        raise InvalidPayment("Payment data unavailable.")
    if (not re.fullmatch(r"pay_[A-Za-z0-9]+", str(payment.get("id", ""))) or payment.get("status") != "captured"
            or ("captured" in payment and payment["captured"] is not True)
            or payment.get("amount") != checkout.amount_paise or payment.get("currency") != checkout.currency):
        raise InvalidPayment("Payment is not captured for the expected amount and currency.")
    refunded, refund_status = payment.get("amount_refunded", 0), payment.get("refund_status")
    if (type(refunded) is not int or not 0 <= refunded <= checkout.amount_paise
            or refund_status not in (None, "", "partial", "full")
            or (refund_status == "partial" and not 0 < refunded < checkout.amount_paise)
            or (refund_status == "full" and refunded != checkout.amount_paise)
            or (not refund_recovery and (refunded > 0 or refund_status not in (None, "")))):
        raise InvalidPayment("Payment reports a refund or an inconsistent refund snapshot.")


def _subscription_period(checkout, subscription):
    if (not isinstance(subscription, dict) or subscription.get("id") != checkout.provider_subscription_id
            or subscription.get("plan_id") != checkout.provider_plan_id):
        raise InvalidPayment("Subscription does not match the checkout.")
    try:
        start = datetime.fromtimestamp(int(subscription["current_start"]), tz=dt_timezone.utc)
        end = datetime.fromtimestamp(int(subscription["current_end"]), tz=dt_timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise InvalidPayment("Paid subscription period unavailable.") from None
    if end <= start or end - start > timedelta(days=400):
        raise InvalidPayment("Invalid paid subscription period.")
    return start, end


def activate_capture(checkout_id, payment_data, subscription_data=None, request=None, *, refund_recovery=False):
    """Only called after checkout signature + API fetch, or a verified webhook."""
    with transaction.atomic():
        initial = Checkout.objects.select_related("user").get(pk=checkout_id)
        lock_practice_user(initial.user)
        checkout = Checkout.objects.select_for_update(of=("self",)).select_related("plan", "user").get(pk=checkout_id)
        _validate_payment(checkout, payment_data, refund_recovery=refund_recovery)
        if checkout.billing == PracticePlan.Billing.ONE_TIME:
            if not checkout.provider_order_id or not checkout.provider_order_id.startswith("order_"):
                raise InvalidPayment("Provider order has not been linked.")
            if payment_data.get("order_id") != checkout.provider_order_id:
                raise InvalidPayment("Payment belongs to a different order.")
            if checkout.status == Checkout.Status.REFUNDED:
                raise InvalidPayment("Refunded access cannot be reactivated by replay.")
            prior = checkout.payments.exclude(status=Payment.Status.FAILED).first()
            if prior and prior.provider_payment_id != payment_data["id"]:
                raise InvalidPayment("An order can only be activated once.")
            start = timezone.now()
            end = start + timedelta(days=checkout.duration_days)
        else:
            if not checkout.provider_subscription_id or not checkout.provider_subscription_id.startswith("sub_"):
                raise InvalidPayment("Provider subscription has not been linked.")
            start, end = _subscription_period(checkout, subscription_data)
            alleged_sub = payment_data.get("subscription_id")
            if alleged_sub and alleged_sub != checkout.provider_subscription_id:
                raise InvalidPayment("Payment belongs to a different subscription.")
        try:
            payment, created = Payment.objects.get_or_create(
                provider_payment_id=payment_data["id"], defaults={"checkout": checkout,
                    "amount_paise": checkout.amount_paise, "currency": checkout.currency,
                    "period_start": start, "period_end": end})
        except IntegrityError:
            raise InvalidPayment("Payment already belongs to another checkout.") from None
        if (payment.checkout_id != checkout.pk or payment.amount_paise != checkout.amount_paise
                or payment.currency != checkout.currency):
            raise InvalidPayment("Payment already belongs to another checkout or amount.")
        if not created and payment.status == Payment.Status.FAILED:
            # A provider failure can be followed by a late capture of the same
            # payment ID. Never create access until this verified capture.
            payment.status = Payment.Status.CAPTURED
            payment.captured_at = timezone.now()
            payment.period_start, payment.period_end = start, end
            payment.save(update_fields=["status", "captured_at", "period_start", "period_end"])
            created = True
        elif not created:
            if payment.status != Payment.Status.CAPTURED:
                raise InvalidPayment("Refunded payment cannot be replayed.")
            return checkout  # duplicate confirmation must never extend access
        entitlement = PracticeEntitlement.objects.select_for_update().filter(checkout=checkout).first()
        if entitlement:
            if checkout.billing != PracticePlan.Billing.RECURRING:
                raise InvalidPayment("Order was already activated.")
            if end > entitlement.valid_until:
                entitlement.valid_until = end
                entitlement.is_revoked = False
                entitlement.save(update_fields=["valid_until", "is_revoked", "updated_at"])
        else:
            PracticeEntitlement.objects.create(user=checkout.user, plan=checkout.plan, checkout=checkout,
                                                starts_at=start, valid_until=end)
        if checkout.status != Checkout.Status.CANCELLED:
            checkout.status = Checkout.Status.PAID
            checkout.save(update_fields=["status", "updated_at"])
        log_audit(checkout.user, "PRACTICE_PAYMENT_CAPTURED", f"Verified payment for checkout {checkout.reference}.", request=request)
        return checkout


def verify_checkout(checkout, payment_id, provider_id, signature, request=None):
    if not re.fullmatch(r"pay_[A-Za-z0-9]+", payment_id or "") or not re.fullmatch(r"[0-9a-fA-F]{64}", signature or ""):
        raise InvalidPayment("Invalid payment confirmation.")
    api = client()
    if checkout.billing == PracticePlan.Billing.ONE_TIME:
        if provider_id != checkout.provider_order_id:
            raise InvalidPayment("Order does not match checkout.")
        api.utility.verify_payment_signature({"razorpay_order_id": checkout.provider_order_id,
            "razorpay_payment_id": payment_id, "razorpay_signature": signature})
        payment = api.payment.fetch(payment_id, timeout=15)
        if not isinstance(payment, dict) or payment.get("id") != payment_id:
            raise InvalidPayment("Fetched payment ID does not match the signed confirmation.")
        refunded = payment.get("amount_refunded", 0)
        if type(refunded) is not int or refunded < 0:
            raise InvalidPayment("Invalid provider refund amount.")
        if refunded > 0:
            record_reported_refund(checkout.pk, payment, request=request)
            raise InvalidPayment("Provider reports a refund; await reconciliation.")
        return activate_capture(checkout.pk, payment, request=request)
    if provider_id != checkout.provider_subscription_id:
        raise InvalidPayment("Subscription does not match checkout.")
    api.utility.verify_subscription_payment_signature({"razorpay_subscription_id": checkout.provider_subscription_id,
        "razorpay_payment_id": payment_id, "razorpay_signature": signature})
    payment = api.payment.fetch(payment_id, timeout=15)
    if not isinstance(payment, dict) or payment.get("id") != payment_id:
        raise InvalidPayment("Fetched payment ID does not match the signed confirmation.")
    subscription = api.subscription.fetch(checkout.provider_subscription_id, timeout=15)
    refunded = payment.get("amount_refunded", 0)
    if type(refunded) is not int or refunded < 0:
        raise InvalidPayment("Invalid provider refund amount.")
    if refunded > 0:
        record_reported_subscription_refund(checkout.pk, payment, subscription, request=request)
        raise InvalidPayment("Provider reports a refund; await reconciliation.")
    return activate_capture(checkout.pk, payment, subscription, request=request)


def record_reported_refund(checkout_id, payment_data, request=None):
    """Record an API-observed one-time legacy refund, without inventing IDs.

    Legacy partial refunds historically retain access; a full refund revokes it.
    Recurring paid periods cannot safely be reconstructed from an API refund.
    """
    if not settings.ALLOW_TEST_PAYMENT_CREDITS or payment_data.get("captured") is not True:
        raise InvalidPayment("Provider did not confirm a captured payment.")
    if payment_data.get("status") not in ("captured", "refunded"):
        raise InvalidPayment("Invalid refunded payment state.")
    with transaction.atomic():
        initial = Checkout.objects.select_related("user").get(pk=checkout_id)
        lock_practice_user(initial.user)
        checkout = Checkout.objects.select_for_update().get(pk=checkout_id)
        normalized = {**payment_data, "status": "captured"}
        _validate_payment(checkout, normalized, refund_recovery=True)
        refunded = normalized.get("amount_refunded")
        if (checkout.billing != PracticePlan.Billing.ONE_TIME or not checkout.provider_order_id
                or normalized.get("order_id") != checkout.provider_order_id
                or type(refunded) is not int or not 0 < refunded <= checkout.amount_paise):
            raise InvalidPayment("Refunded payment requires reconciliation.")
        payment = Payment.objects.select_for_update().filter(provider_payment_id=normalized["id"]).first()
        if payment and (payment.checkout_id != checkout.pk or payment.amount_paise != checkout.amount_paise
                        or payment.currency != checkout.currency):
            raise InvalidPayment("Payment belongs to another checkout or amount.")
        if not payment or payment.status in (Payment.Status.FAILED, Payment.Status.CAPTURED):
            activate_capture(checkout.pk, normalized, request=request, refund_recovery=True)
            payment = Payment.objects.select_for_update().get(provider_payment_id=normalized["id"])
        elif payment.status not in (Payment.Status.PARTIAL_REFUND, Payment.Status.REFUNDED):
            raise InvalidPayment("Invalid provider payment state.")
        entitlement = PracticeEntitlement.objects.select_for_update().filter(checkout=checkout).first()
        if not entitlement:
            raise InvalidPayment("Payment requires reconciliation.")
        desired = (Payment.Status.REFUNDED if refunded == checkout.amount_paise or payment.status == Payment.Status.REFUNDED
                   else Payment.Status.PARTIAL_REFUND)
        changed = payment.status != desired
        if payment.status != desired:
            payment.status = desired
            payment.save(update_fields=["status"])
        if desired == Payment.Status.REFUNDED and not entitlement.is_revoked:
            entitlement.is_revoked = True
            entitlement.save(update_fields=["is_revoked", "updated_at"])
            changed = True
        if desired == Payment.Status.REFUNDED and checkout.status != Checkout.Status.REFUNDED:
            checkout.status = Checkout.Status.REFUNDED
            checkout.save(update_fields=["status", "updated_at"])
            changed = True
        if changed:
            log_audit(checkout.user, "PRACTICE_TEST_REFUND_OBSERVED",
                      f"Provider reports a refund for {checkout.reference}; refund details await reconciliation.", request=request)


def record_reported_subscription_refund(checkout_id, payment_data, subscription_data, request=None):
    """Record a refunded legacy subscription payment without guessing its period."""
    if not settings.ALLOW_TEST_PAYMENT_CREDITS or payment_data.get("captured") is not True:
        raise InvalidPayment("Provider did not confirm a captured payment.")
    if payment_data.get("status") not in ("captured", "refunded"):
        raise InvalidPayment("Invalid refunded payment state.")
    with transaction.atomic():
        initial = Checkout.objects.select_related("user").get(pk=checkout_id)
        lock_practice_user(initial.user)
        checkout = Checkout.objects.select_for_update().get(pk=checkout_id)
        if checkout.billing != PracticePlan.Billing.RECURRING:
            raise InvalidPayment("Not a recurring checkout.")
        # A current subscription fetch establishes identity, not the payment's
        # historical paid period. Only a previously stored period is trusted.
        _subscription_period(checkout, subscription_data)
        normalized = {**payment_data, "status": "captured"}
        _validate_payment(checkout, normalized, refund_recovery=True)
        alleged_sub, refunded = normalized.get("subscription_id"), normalized.get("amount_refunded")
        if ((alleged_sub and alleged_sub != checkout.provider_subscription_id)
                or type(refunded) is not int or not 0 < refunded <= checkout.amount_paise):
            raise InvalidPayment("Refunded subscription payment requires reconciliation.")
        payment = Payment.objects.select_for_update().filter(provider_payment_id=normalized["id"]).first()
        if payment and (payment.checkout_id != checkout.pk or payment.amount_paise != checkout.amount_paise
                        or payment.currency != checkout.currency):
            raise InvalidPayment("Payment belongs to another checkout or amount.")
        desired = (Payment.Status.REFUNDED if refunded == checkout.amount_paise
                   or (payment and payment.status == Payment.Status.REFUNDED) else Payment.Status.PARTIAL_REFUND)
        changed = False
        if not payment:
            # This marker prevents an old charge snapshot from granting access;
            # support must reconcile the unknown paid period. No refund ID is
            # invented, and the provider's current period is not backdated.
            payment = Payment.objects.create(checkout=checkout, provider_payment_id=normalized["id"],
                amount_paise=checkout.amount_paise, currency=checkout.currency, status=desired)
            changed = True
        elif payment.status not in (Payment.Status.FAILED, Payment.Status.CAPTURED,
                                    Payment.Status.PARTIAL_REFUND, Payment.Status.REFUNDED):
            raise InvalidPayment("Invalid provider payment state.")
        elif payment.status != desired:
            payment.status = desired
            payment.save(update_fields=["status"])
            changed = True
        entitlement = PracticeEntitlement.objects.select_for_update().filter(checkout=checkout).first()
        if (desired == Payment.Status.REFUNDED and entitlement and payment.period_end is not None
                and payment.period_end == entitlement.valid_until and not entitlement.is_revoked):
            entitlement.is_revoked = True
            entitlement.save(update_fields=["is_revoked", "updated_at"])
            changed = True
        if changed:
            log_audit(checkout.user, "PRACTICE_TEST_REFUND_OBSERVED",
                      f"Provider reports a subscription refund for {checkout.reference}; reconcile its paid period and refund details.", request=request)


def record_failure(checkout, data, request=None):
    """Record a failure without downgrading a concurrently captured payment."""
    with transaction.atomic():
        lock_practice_user(checkout.user)
        checkout = Checkout.objects.select_for_update().get(pk=checkout.pk)
        if (not isinstance(data, dict) or not re.fullmatch(r"pay_[A-Za-z0-9]+", str(data.get("id", "")))
                or data.get("status") != "failed" or data.get("order_id") != checkout.provider_order_id
                or data.get("amount") != checkout.amount_paise or data.get("currency") != checkout.currency):
            raise InvalidPayment("Invalid failed payment.")
        existing = Payment.objects.filter(provider_payment_id=data["id"]).first()
        if existing and (existing.checkout_id != checkout.pk or existing.amount_paise != checkout.amount_paise
                         or existing.currency != checkout.currency):
            raise InvalidPayment("Payment belongs to another checkout or amount.")
        if checkout.status == Checkout.Status.CREATED and not existing:
            Payment.objects.create(provider_payment_id=data["id"], checkout=checkout,
                                   amount_paise=checkout.amount_paise, currency=checkout.currency,
                                   status=Payment.Status.FAILED)
            log_audit(checkout.user, "PRACTICE_PAYMENT_FAILED",
                      f"Payment failed for checkout {checkout.reference}.", request=request)


def refunded_capture_replay(checkout, payment_data, subscription_data=None):
    """Acknowledge an old capture without extending or reactivating access."""
    try:
        _validate_payment(checkout, payment_data, refund_recovery=True)
        if checkout.billing == PracticePlan.Billing.ONE_TIME:
            if not checkout.provider_order_id or payment_data.get("order_id") != checkout.provider_order_id:
                return False
        else:
            start, end = _subscription_period(checkout, subscription_data)
            alleged_sub = payment_data.get("subscription_id")
            if alleged_sub and alleged_sub != checkout.provider_subscription_id:
                return False
    except InvalidPayment:
        return False
    payment = Payment.objects.filter(checkout=checkout, provider_payment_id=payment_data["id"],
        amount_paise=checkout.amount_paise, currency=checkout.currency,
        status__in=(Payment.Status.PARTIAL_REFUND, Payment.Status.REFUNDED)).first()
    if not payment:
        return False
    if checkout.billing == PracticePlan.Billing.RECURRING and (payment.period_start != start or payment.period_end != end):
        return False
    entitlement = PracticeEntitlement.objects.filter(checkout=checkout).first()
    if not entitlement:
        return False
    return not (checkout.billing == PracticePlan.Billing.ONE_TIME and payment.status == Payment.Status.REFUNDED
                and not entitlement.is_revoked)


@transaction.atomic
def apply_webhook_refund(refund_data, payment_data, request=None):
    """Recover a signed one-time legacy refund before its capture event.

    Recurring refunds without a known payment cannot establish a paid period
    from the refund payload alone and must be reconciled/replayed by an operator.
    """
    if not isinstance(refund_data, dict) or not isinstance(payment_data, dict):
        raise InvalidPayment("Invalid refund payload.")
    payment_id = refund_data.get("payment_id")
    if not isinstance(payment_id, str) or not re.fullmatch(r"pay_[A-Za-z0-9]+", payment_id):
        raise InvalidPayment("Invalid refund payment ID.")
    existing = Payment.objects.select_related("checkout").filter(provider_payment_id=payment_id).first()
    checkout = existing.checkout if existing else None
    if not checkout and payment_data.get("id") == payment_id:
        order_id = payment_data.get("order_id")
        if isinstance(order_id, str) and re.fullmatch(r"order_[A-Za-z0-9]+", order_id):
            checkout = Checkout.objects.filter(provider_order_id=order_id, billing=PracticePlan.Billing.ONE_TIME).first()
    if not checkout:
        raise InvalidPayment("Payment for refund not found; reconciliation is required.")
    if payment_data:
        if (payment_data.get("id") != payment_id or payment_data.get("amount") != checkout.amount_paise
                or payment_data.get("currency") != checkout.currency
                or ("captured" in payment_data and payment_data["captured"] is not True)
                or payment_data.get("status") not in ("captured", "refunded")
                or (checkout.billing == PracticePlan.Billing.ONE_TIME
                    and payment_data.get("order_id") != checkout.provider_order_id)):
            raise InvalidPayment("Refund payment snapshot does not match the checkout.")
    if not existing or existing.status == Payment.Status.FAILED:
        if (checkout.billing != PracticePlan.Billing.ONE_TIME or payment_data.get("captured") is not True
                or payment_data.get("status") not in ("captured", "refunded")):
            raise InvalidPayment("A refund alone does not establish a captured one-time payment.")
        # Capture and refund are in this same transaction: a full refund never
        # exposes an entitlement, even if the capture webhook arrives later.
        activate_capture(checkout.pk, {**payment_data, "status": "captured"}, request=request, refund_recovery=True)
    apply_refund(payment_id, refund_data, request=request)


def apply_refund(payment_id, refund_data, request=None):
    if not isinstance(refund_data, dict):
        raise InvalidPayment("Invalid refund.")
    initial = Payment.objects.select_related("checkout__user").filter(provider_payment_id=payment_id).first()
    if not initial:
        raise InvalidPayment("Payment for refund not found.")
    with transaction.atomic():
        # Use the same user -> checkout -> payment order as capture. This also
        # serializes legacy PDF debits with a full entitlement refund.
        lock_practice_user(initial.checkout.user)
        Checkout.objects.select_for_update().get(pk=initial.checkout_id)
        payment = Payment.objects.select_for_update(of=("self",)).select_related("checkout__user").get(pk=initial.pk)
        refund_id = refund_data.get("id", "")
        amount = refund_data.get("amount")
        if (not re.fullmatch(r"rfnd_[A-Za-z0-9]+", str(refund_id)) or type(amount) is not int or amount <= 0
                or refund_data.get("payment_id") != payment.provider_payment_id
                or refund_data.get("currency") != payment.currency or refund_data.get("status") != "processed"
                or payment.status == Payment.Status.FAILED):
            raise InvalidPayment("Invalid refund.")
        refund, created = Refund.objects.get_or_create(provider_refund_id=refund_id,
                                                        defaults={"payment": payment, "amount_paise": amount})
        if refund.payment_id != payment.pk or refund.amount_paise != amount:
            raise InvalidPayment("Refund does not match its original record.")
        if not created:
            return
        total = payment.refunds.aggregate(total=Sum("amount_paise"))["total"] or 0
        if total > payment.amount_paise:
            raise InvalidPayment("Refund exceeds payment amount.")
        # The API can report a full refund before its individual webhooks.
        # Preserve that observed full status as those events arrive out of order.
        payment.status = (Payment.Status.REFUNDED if total == payment.amount_paise or payment.status == Payment.Status.REFUNDED
                          else Payment.Status.PARTIAL_REFUND)
        payment.save(update_fields=["status"])
        entitlement = PracticeEntitlement.objects.select_for_update().filter(checkout=payment.checkout).first()
        if payment.status == Payment.Status.REFUNDED and entitlement and (
                payment.checkout.billing == PracticePlan.Billing.ONE_TIME or payment.period_end == entitlement.valid_until):
            entitlement.is_revoked = True
            entitlement.save(update_fields=["is_revoked", "updated_at"])
            if payment.checkout.billing == PracticePlan.Billing.ONE_TIME:
                payment.checkout.status = Checkout.Status.REFUNDED
                payment.checkout.save(update_fields=["status", "updated_at"])
        log_audit(payment.checkout.user, "PRACTICE_PAYMENT_REFUNDED", f"Refund recorded for checkout {payment.checkout.reference}.", request=request)
