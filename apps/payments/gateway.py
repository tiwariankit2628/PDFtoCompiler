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


class GatewayUnavailable(Exception):
    pass


class InvalidPayment(Exception):
    pass


def gateway_ready(require_webhook=False):
    key = settings.RAZORPAY_KEY_ID or ""
    secret = settings.RAZORPAY_KEY_SECRET or ""
    webhook = settings.RAZORPAY_WEBHOOK_SECRET or ""
    # Never accept live keys, defaults, or obvious example values.
    invalid = (not re.fullmatch(r"rzp_test_[A-Za-z0-9]+", key)
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


def _validate_payment(checkout, payment):
    if not isinstance(payment, dict):
        raise InvalidPayment("Payment data unavailable.")
    if (not str(payment.get("id", "")).startswith("pay_") or payment.get("status") != "captured"
            or payment.get("amount") != checkout.amount_paise or payment.get("currency") != checkout.currency):
        raise InvalidPayment("Payment is not captured for the expected amount and currency.")


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


def activate_capture(checkout_id, payment_data, subscription_data=None, request=None):
    """Only called after checkout signature + API fetch, or a verified webhook."""
    with transaction.atomic():
        checkout = Checkout.objects.select_for_update().select_related("plan", "user").get(pk=checkout_id)
        _validate_payment(checkout, payment_data)
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
        if payment.checkout_id != checkout.pk:
            raise InvalidPayment("Payment already belongs to another checkout.")
        if not created:
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
        return activate_capture(checkout.pk, payment, request=request)
    if provider_id != checkout.provider_subscription_id:
        raise InvalidPayment("Subscription does not match checkout.")
    api.utility.verify_subscription_payment_signature({"razorpay_subscription_id": checkout.provider_subscription_id,
        "razorpay_payment_id": payment_id, "razorpay_signature": signature})
    payment = api.payment.fetch(payment_id, timeout=15)
    if not isinstance(payment, dict) or payment.get("id") != payment_id:
        raise InvalidPayment("Fetched payment ID does not match the signed confirmation.")
    subscription = api.subscription.fetch(checkout.provider_subscription_id, timeout=15)
    return activate_capture(checkout.pk, payment, subscription, request=request)


def apply_refund(payment_id, refund_data, request=None):
    with transaction.atomic():
        payment = Payment.objects.select_for_update().select_related("checkout__user").filter(provider_payment_id=payment_id).first()
        if not payment:
            raise InvalidPayment("Payment for refund not found.")
        refund_id = refund_data.get("id", "")
        amount = refund_data.get("amount")
        if not str(refund_id).startswith("rfnd_") or not isinstance(amount, int) or amount <= 0:
            raise InvalidPayment("Invalid refund.")
        refund, created = Refund.objects.get_or_create(provider_refund_id=refund_id,
                                                        defaults={"payment": payment, "amount_paise": amount})
        if refund.payment_id != payment.pk:
            raise InvalidPayment("Refund belongs to another payment.")
        if not created:
            return
        total = payment.refunds.aggregate(total=Sum("amount_paise"))["total"] or 0
        if total > payment.amount_paise:
            raise InvalidPayment("Refund exceeds payment amount.")
        payment.status = Payment.Status.REFUNDED if total == payment.amount_paise else Payment.Status.PARTIAL_REFUND
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
