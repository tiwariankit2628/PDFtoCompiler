"""Student billing UI and authenticated, signature-verified Razorpay callbacks."""
import hashlib
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.cache import patch_cache_control
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.core.permissions import StudentRequiredMixin
from apps.core.utils import log_audit
from . import gateway
from .gateway import GatewayUnavailable, InvalidPayment
from .models import Checkout, Payment, PracticeEntitlement, PracticePlan, WebhookEvent
from .services import usage_summary

logger = logging.getLogger(__name__)


class PrivateBillingMixin(StudentRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        patch_cache_control(response, private=True, no_store=True)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response


class PlansView(PrivateBillingMixin, View):
    def get(self, request):
        from django.core.paginator import Paginator
        checkouts = Paginator(Checkout.objects.filter(user=request.user).select_related("plan", "entitlement")
                              .prefetch_related("payments"), 20).get_page(request.GET.get("billing_page"))
        return render(request, "payments/plans.html", {
            "summary": usage_summary(request.user),
            "plans": PracticePlan.objects.filter(is_available=True).order_by("price_paise", "name"),
            "checkouts": checkouts, "gateway_ready": gateway.gateway_ready(),
        })


class CreateCheckoutView(PrivateBillingMixin, View):
    def post(self, request, slug):
        plan = get_object_or_404(PracticePlan, slug=slug, is_available=True)
        if plan.billing == PracticePlan.Billing.FREE or plan.price_paise <= 0:
            raise Http404()
        if request.POST.get("accept_terms") != "yes":
            messages.error(request, "Please confirm the price and billing terms before continuing.")
            return redirect("payments:plans")
        if not gateway.gateway_ready():
            messages.error(request, "Razorpay test mode is not configured. No payment or upgrade was created.")
            return redirect("payments:plans")
        # The row is saved before any network call. If the provider response is
        # lost, don't silently create another subscription/charge on retry.
        with transaction.atomic():
            from django.contrib.auth import get_user_model
            get_user_model().objects.select_for_update().get(pk=request.user.pk)
            if PracticeEntitlement.objects.filter(user=request.user, plan=plan, is_revoked=False, valid_until__gt=timezone.now()).exists():
                messages.info(request, "You already have paid access to this plan. Wait for it to expire before buying again.")
                return redirect("payments:plans")
            if (plan.billing == PracticePlan.Billing.RECURRING and Checkout.objects.filter(
                    user=request.user, plan=plan, billing=PracticePlan.Billing.RECURRING,
                    status=Checkout.Status.PAID, cancellation_requested=False).exists()):
                messages.info(request, "An existing subscription may still renew. Manage it in billing history before creating another.")
                return redirect("payments:plans")
            unresolved = Checkout.objects.filter(user=request.user, plan=plan, status=Checkout.Status.INITIATING).first()
            if unresolved:
                messages.warning(request, "A previous checkout is awaiting reconciliation. Contact support before creating another.")
                return redirect("payments:plans")
            existing = Checkout.objects.filter(user=request.user, plan=plan, status=Checkout.Status.CREATED,
                                                created_at__gte=timezone.now() - timedelta(hours=24)).first()
            if existing:
                return redirect("payments:checkout", reference=existing.reference)
            checkout = Checkout.objects.create(user=request.user, plan=plan, billing=plan.billing,
                amount_paise=plan.price_paise, currency="INR", duration_days=plan.duration_days,
                provider_plan_id=plan.razorpay_plan_id, billing_period=plan.billing_period,
                billing_interval=plan.billing_interval, recurring_cycles=plan.recurring_cycles)
        log_audit(request.user, "PRACTICE_CHECKOUT_STARTED", f"Test checkout {checkout.reference} initiated.", request=request)
        try:
            gateway.create_provider_checkout(checkout)
        except Exception:
            # We cannot know whether a remote create succeeded after a timeout.
            # Keep the initiating record for reconciliation, not fake failure/success.
            logger.warning("Razorpay test checkout creation did not return a verified response for %s.", checkout.reference)
            messages.error(request, "Checkout could not be confirmed. No access was activated; contact support before retrying.")
            return redirect("payments:plans")
        return redirect("payments:checkout", reference=checkout.reference)


class CheckoutView(PrivateBillingMixin, View):
    def get(self, request, reference):
        checkout = get_object_or_404(Checkout.objects.select_related("plan"), reference=reference, user=request.user)
        if checkout.status not in (Checkout.Status.CREATED, Checkout.Status.PAID):
            messages.warning(request, "This checkout is not available for payment.")
            return redirect("payments:plans")
        return render(request, "payments/checkout.html", {"checkout": checkout,
                      "public_key": settings.RAZORPAY_KEY_ID if gateway.gateway_ready() else ""})


class VerifyCheckoutView(PrivateBillingMixin, View):
    def post(self, request, reference):
        checkout = get_object_or_404(Checkout.objects.select_related("plan"), reference=reference, user=request.user)
        try:
            data = json.loads(request.body)
        except (TypeError, ValueError):
            return JsonResponse({"error": "Invalid confirmation."}, status=400)
        if not isinstance(data, dict):
            return JsonResponse({"error": "Invalid confirmation."}, status=400)
        payment_id = data.get("razorpay_payment_id", "")
        provider_id = (data.get("razorpay_order_id", "") if checkout.billing == PracticePlan.Billing.ONE_TIME
                       else data.get("razorpay_subscription_id", ""))
        signature = data.get("razorpay_signature", "")
        if not all(isinstance(v, str) for v in (payment_id, provider_id, signature)):
            return JsonResponse({"error": "Invalid confirmation."}, status=400)
        try:
            gateway.verify_checkout(checkout, payment_id, provider_id, signature, request=request)
        except Exception:
            log_audit(request.user, "PRACTICE_PAYMENT_VERIFY_REJECTED", f"Unverified confirmation for checkout {checkout.reference}.", request=request)
            return JsonResponse({"status": "pending", "message": "Payment was not verified as captured. Access remains unchanged; check billing history after the webhook arrives."}, status=400)
        return JsonResponse({"status": "verified", "message": "Payment verified on the server."})


class CancelSubscriptionView(PrivateBillingMixin, View):
    def post(self, request, reference):
        checkout = get_object_or_404(Checkout, reference=reference, user=request.user, billing=PracticePlan.Billing.RECURRING)
        if not checkout.provider_subscription_id or checkout.cancellation_requested or checkout.status in (Checkout.Status.CANCELLED, Checkout.Status.REFUNDED):
            messages.warning(request, "This subscription cannot be cancelled here.")
            return redirect("payments:plans")
        try:
            result = gateway.client().subscription.cancel(checkout.provider_subscription_id, {"cancel_at_cycle_end": 1}, timeout=15)
            if not isinstance(result, dict) or result.get("id") != checkout.provider_subscription_id:
                raise InvalidPayment("Provider cancellation response did not match.")
        except Exception:
            messages.error(request, "Cancellation could not be confirmed. Please check with support; no changes were assumed.")
            return redirect("payments:plans")
        checkout.cancellation_requested = True
        checkout.save(update_fields=["cancellation_requested", "updated_at"])
        log_audit(request.user, "PRACTICE_SUBSCRIPTION_CANCEL_REQUESTED", f"Cancellation requested for {checkout.reference}.", request=request)
        messages.success(request, "Cancellation requested at the end of the current billing cycle. Paid access remains until its expiry.")
        return redirect("payments:plans")


def _entity(payload, name):
    value = payload.get(name, {}) if isinstance(payload, dict) else {}
    return value.get("entity", {}) if isinstance(value, dict) else {}


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    """Signature is checked against raw UTF-8 bytes before JSON is trusted."""
    if not gateway.gateway_ready(require_webhook=True):
        return HttpResponse(status=503)
    if len(request.body) > 1024 * 1024:
        return HttpResponse(status=413)
    try:
        body = request.body.decode("utf-8")
        signature = request.headers.get("X-Razorpay-Signature", "")
        gateway.client().utility.verify_webhook_signature(body, signature, settings.RAZORPAY_WEBHOOK_SECRET)
        data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("event"), str):
            raise ValueError("Invalid event")
    except Exception:
        return HttpResponse(status=400)
    event_type = data["event"][:80]
    event_id = request.headers.get("X-Razorpay-Event-Id", "").strip()
    if not event_id:
        event_id = "body-" + hashlib.sha256(request.body).hexdigest()
    if len(event_id) > 140:
        return HttpResponse(status=400)
    payload = data.get("payload", {})
    payment = _entity(payload, "payment")
    subscription = _entity(payload, "subscription")
    refund = _entity(payload, "refund")
    try:
        with transaction.atomic():
            if WebhookEvent.objects.filter(event_id=event_id).exists():
                return HttpResponse(status=200)
            if event_type in ("payment.captured", "order.paid"):
                order_id = payment.get("order_id")
                checkout = (Checkout.objects.filter(provider_order_id=order_id, billing=PracticePlan.Billing.ONE_TIME).first()
                            if isinstance(order_id, str) and order_id.startswith("order_") else None)
                if checkout:
                    gateway.activate_capture(checkout.pk, payment, request=request)
                elif isinstance(order_id, str) and order_id.startswith("order_") and not payment.get("invoice_id"):
                    # Do not acknowledge a potentially unresolved order: a
                    # signed retry can be processed after safe reconciliation.
                    raise InvalidPayment("Order not yet linked.")
            elif event_type == "subscription.charged":
                subscription_id = subscription.get("id")
                checkout = (Checkout.objects.filter(provider_subscription_id=subscription_id, billing=PracticePlan.Billing.RECURRING).first()
                            if isinstance(subscription_id, str) and subscription_id.startswith("sub_") else None)
                if not checkout:
                    # Retry in case provider response and webhook crossed in flight.
                    raise InvalidPayment("Subscription not yet linked.")
                gateway.activate_capture(checkout.pk, payment, subscription, request=request)
            elif event_type in ("subscription.cancelled", "subscription.completed", "subscription.halted"):
                subscription_id = subscription.get("id")
                checkout = (Checkout.objects.select_for_update().filter(provider_subscription_id=subscription_id, billing=PracticePlan.Billing.RECURRING).first()
                            if isinstance(subscription_id, str) and subscription_id.startswith("sub_") else None)
                if checkout:
                    checkout.status = Checkout.Status.CANCELLED
                    checkout.save(update_fields=["status", "updated_at"])
                    log_audit(checkout.user, "PRACTICE_SUBSCRIPTION_ENDED", f"Subscription state {event_type} for {checkout.reference}.", request=request)
            elif event_type == "payment.failed":
                order_id = payment.get("order_id")
                checkout = (Checkout.objects.select_for_update().filter(provider_order_id=order_id, billing=PracticePlan.Billing.ONE_TIME).first()
                            if isinstance(order_id, str) and order_id.startswith("order_") else None)
                if checkout and checkout.status == Checkout.Status.CREATED:
                    payment_id = payment.get("id")
                    if (isinstance(payment_id, str) and payment_id.startswith("pay_")
                            and payment.get("amount") == checkout.amount_paise and payment.get("currency") == checkout.currency):
                        failed, created = Payment.objects.get_or_create(provider_payment_id=payment_id,
                            defaults={"checkout": checkout, "amount_paise": checkout.amount_paise,
                                      "currency": checkout.currency, "status": Payment.Status.FAILED})
                        if failed.checkout_id != checkout.pk:
                            raise InvalidPayment("Payment belongs to another checkout.")
                        if created:
                            log_audit(checkout.user, "PRACTICE_PAYMENT_FAILED", f"Payment failed for checkout {checkout.reference}.", request=request)
                    # An order may be retried with a new provider payment ID.
                    # Keep the checkout open; a failure is not a paid access grant.
            elif event_type == "refund.processed":
                gateway.apply_refund(refund.get("payment_id"), refund, request=request)
            WebhookEvent.objects.create(event_id=event_id, event_type=event_type)
    except (InvalidPayment, IntegrityError, KeyError, TypeError, ValueError):
        # Roll back the event marker so Razorpay can retry after reconciliation.
        return HttpResponse(status=400)
    return HttpResponse(status=200)
