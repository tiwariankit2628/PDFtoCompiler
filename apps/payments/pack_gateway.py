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


def activate_capture(checkout_id, payment_data, request=None):
    if not settings.ALLOW_TEST_PAYMENT_CREDITS:
        raise InvalidPayment("Test payment credits are disabled outside a sandbox.")
    with transaction.atomic():
        initial = PackCheckout.objects.select_related("user").get(pk=checkout_id)
        lock_practice_user(initial.user)
        checkout = PackCheckout.objects.select_for_update().get(pk=checkout_id)
        product = PRODUCTS.get(checkout.product)
        if not product or checkout.units != product.units or checkout.amount_paise <= 0 or checkout.currency != "INR":
            raise InvalidPayment("Invalid product snapshot.")
        if (not isinstance(payment_data, dict)
                or not re.fullmatch(r"pay_[A-Za-z0-9]+", str(payment_data.get("id", "")))
                or payment_data.get("status") != "captured"
                or payment_data.get("amount") != checkout.amount_paise
                or payment_data.get("currency") != checkout.currency
                or not checkout.provider_order_id
                or payment_data.get("order_id") != checkout.provider_order_id):
            raise InvalidPayment("Payment is not captured for this order, amount and currency.")
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
        if payment.checkout_id != checkout.pk or payment.status != Payment.Status.CAPTURED:
            raise InvalidPayment("Payment cannot be replayed.")
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
    return activate_capture(checkout.pk, payment, request=request)


def record_failure(checkout, data, request=None):
    if (not isinstance(data, dict) or not re.fullmatch(r"pay_[A-Za-z0-9]+", str(data.get("id", "")))
            or data.get("status") != "failed" or data.get("order_id") != checkout.provider_order_id
            or data.get("amount") != checkout.amount_paise or data.get("currency") != checkout.currency):
        raise InvalidPayment("Invalid failed payment.")
    payment, created = PackPayment.objects.get_or_create(provider_payment_id=data["id"],
        defaults={"checkout": checkout, "amount_paise": checkout.amount_paise,
                  "currency": checkout.currency, "status": Payment.Status.FAILED})
    if payment.checkout_id != checkout.pk:
        raise InvalidPayment("Payment belongs to another checkout.")
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
        payment.status = Payment.Status.REFUNDED if total == payment.amount_paise else Payment.Status.PARTIAL_REFUND
        payment.save(update_fields=["status"])
        # Any refund freezes unused units and series access pending support
        # reconciliation. Previously started attempts and past results survive.
        CreditGrant.objects.filter(checkout=payment.checkout).update(is_revoked=True)
        if total == payment.amount_paise:
            payment.checkout.status = PackCheckout.Status.REFUNDED
            payment.checkout.save(update_fields=["status", "updated_at"])
        log_audit(payment.checkout.user, "PACK_TEST_PAYMENT_REFUNDED", f"Refund recorded for {payment.checkout.reference}.", request=request)
