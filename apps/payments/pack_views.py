"""Account-bound checkout for fixed, one-time TEST-mode products."""
import json
import logging
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, connection, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.cache import patch_cache_control
from django.views import View

from apps.accounts.models import User
from apps.core.permissions import ActiveUserRequiredMixin
from apps.core.utils import log_audit
from . import gateway, pack_gateway
from .models import PackCheckout
from .packs import PRODUCTS, eligible_products, has_series_access
from .services import lock_practice_user

logger = logging.getLogger(__name__)


class PackMixin(ActiveUserRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        try:
            response = super().dispatch(request, *args, **kwargs)
        except User.DoesNotExist:
            raise Http404() from None
        except OperationalError as exc:
            if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                raise
            response = HttpResponse("Billing is busy. Please retry; no new payment is assumed.", status=503)
            response["Retry-After"] = "1"
        patch_cache_control(response, private=True, no_store=True)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response


class StartPackCheckout(PackMixin, View):
    def post(self, request, product_code):
        product = PRODUCTS.get(product_code)
        if not product or product not in eligible_products(request.user):
            raise Http404()
        if request.POST.get("accept_terms") != "yes":
            messages.error(request, "Confirm the product and price before continuing.")
            return redirect("payments:plans")
        if not gateway.gateway_ready():
            messages.error(request, "Test checkout is not configured. No payment or credits were created.")
            return redirect("payments:plans")
        with transaction.atomic():
            user = lock_practice_user(request.user)
            if not user.is_active or product not in eligible_products(user):
                raise PermissionDenied()
            if user.needs_email_verification:
                return redirect("accounts:verification_pending")
            if product.code.startswith("series_"):
                from apps.exams.series import series_ready
                if not series_ready(user.exam_track):
                    messages.warning(request, "This series is not on sale until all 16 matching tests are ready.")
                    return redirect("exams:series")
                if has_series_access(user, user.exam_track):
                    messages.info(request, "You already have access to this series.")
                    return redirect("exams:series")
            pending = PackCheckout.objects.filter(user=user, product=product.code, status=PackCheckout.Status.INITIATING).first()
            if pending:
                messages.warning(request, "A previous order needs reconciliation. Contact support before trying again.")
                return redirect("payments:plans")
            existing = PackCheckout.objects.filter(user=user, product=product.code, status=PackCheckout.Status.CREATED).first()
            if existing:
                return redirect("payments:pack_checkout", reference=existing.reference)
            checkout = PackCheckout.objects.create(user=user, product=product.code, amount_paise=product.amount_paise,
                                                   units=product.units, currency="INR")
        log_audit(user, "PACK_TEST_CHECKOUT_STARTED", f"Test checkout {checkout.reference} initiated.", request=request)
        try:
            pack_gateway.create_provider_order(checkout)
        except Exception:
            # Provider may have created an order despite a timeout; leave the
            # initiating record for operator reconciliation, never assume success.
            logger.warning("Could not link test order for checkout %s.", checkout.reference)
            messages.error(request, "Checkout could not be confirmed. No credits were activated; contact support before retrying.")
            return redirect("payments:plans")
        return redirect("payments:pack_checkout", reference=checkout.reference)


class PackCheckoutView(PackMixin, View):
    def get(self, request, reference):
        checkout = get_object_or_404(PackCheckout, reference=reference, user=request.user)
        if checkout.status not in (PackCheckout.Status.CREATED, PackCheckout.Status.PAID):
            messages.warning(request, "This checkout is not available.")
            return redirect("payments:plans")
        return render(request, "payments/pack_checkout.html", {"checkout": checkout,
            "product": PRODUCTS.get(checkout.product), "public_key": settings.RAZORPAY_KEY_ID if gateway.gateway_ready() else ""})


class VerifyPackCheckout(PackMixin, View):
    def post(self, request, reference):
        checkout = get_object_or_404(PackCheckout, reference=reference, user=request.user)
        try:
            data = json.loads(request.body)
            if not isinstance(data, dict):
                raise ValueError()
            pid, oid, signature = (data.get("razorpay_payment_id", ""), data.get("razorpay_order_id", ""), data.get("razorpay_signature", ""))
            if not all(isinstance(v, str) for v in (pid, oid, signature)):
                raise ValueError()
            pack_gateway.verify_checkout(checkout, pid, oid, signature, request=request)
        except Exception:
            log_audit(request.user, "PACK_PAYMENT_VERIFY_REJECTED", f"Unverified confirmation for {checkout.reference}.", request=request)
            return JsonResponse({"status": "pending", "message": "Payment could not be verified as active and captured. Check billing history or contact support."}, status=400)
        return JsonResponse({"status": "verified", "message": "Test payment verified on the server."})
