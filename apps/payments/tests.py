"""No network calls: real HMAC verification plus mocked Razorpay API responses."""
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import razorpay
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from . import gateway
from .models import Checkout, Payment, PracticeEntitlement, PracticePlan, Refund, WebhookEvent
from .services import current_plan


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   RAZORPAY_KEY_ID="rzp_test_Unit123", RAZORPAY_KEY_SECRET="safe_unit_secret",
                   RAZORPAY_WEBHOOK_SECRET="safe_webhook_secret")
class RazorpayPhase7Tests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("payer", email="payer@example.test", password="x", role=User.Roles.STUDENT)
        self.other = User.objects.create_user("another", email="another@example.test", password="x", role=User.Roles.STUDENT)
        self.client.force_login(self.user)
        self.plan = PracticePlan.objects.create(name="Prepaid", slug="prepaid", billing="one_time",
            price_paise=14950, duration_days=30, monthly_pdf_uploads=20, monthly_attempts=50)
        self.recurring = PracticePlan.objects.create(name="Recurring", slug="recurring", billing="recurring",
            price_paise=9900, razorpay_plan_id="plan_Unit123", billing_period="monthly", billing_interval=1,
            recurring_cycles=12, monthly_pdf_uploads=50)
        self.api = razorpay.Client(auth=("rzp_test_Unit123", "safe_unit_secret"))
        self.api.order.create = MagicMock(return_value={"id":"order_Unit123", "amount":14950, "currency":"INR"})
        self.api.payment.fetch = MagicMock()
        self.api.plan.fetch = MagicMock(return_value={"id":"plan_Unit123", "period":"monthly", "interval":1,
                                                      "item":{"amount":9900,"currency":"INR"}})
        self.api.subscription.create = MagicMock(return_value={"id":"sub_Unit123", "plan_id":"plan_Unit123"})
        self.api.subscription.fetch = MagicMock()
        self.api.subscription.cancel = MagicMock(return_value={"id":"sub_Unit123", "status":"active"})
        self.patcher = patch("apps.payments.gateway.client", return_value=self.api)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def start(self, plan=None):
        plan = plan or self.plan
        response = self.client.post(reverse("payments:checkout_start", args=[plan.slug]), {"accept_terms":"yes"})
        self.assertEqual(response.status_code, 302)
        return Checkout.objects.get(user=self.user, plan=plan)

    def signature(self, message, secret="safe_unit_secret"):
        return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    def verify(self, checkout, payment_id, provider_id, signature=None):
        is_order = checkout.billing == "one_time"
        message = f"{provider_id}|{payment_id}" if is_order else f"{payment_id}|{provider_id}"
        payload = {"razorpay_payment_id":payment_id, "razorpay_signature":signature or self.signature(message),
                   ("razorpay_order_id" if is_order else "razorpay_subscription_id"):provider_id}
        return self.client.post(reverse("payments:verify", args=[checkout.reference]),
                                json.dumps(payload), content_type="application/json")

    def webhook(self, event, event_id="event-1", secret="safe_webhook_secret"):
        body = json.dumps(event, separators=(",", ":"))
        return self.client.post(reverse("payments:webhook"), body, content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE=self.signature(body, secret), HTTP_X_RAZORPAY_EVENT_ID=event_id)

    @staticmethod
    def one_payment(status="captured", amount=14950, pid="pay_Unit123", order="order_Unit123"):
        return {"id":pid,"order_id":order,"status":status,"amount":amount,"currency":"INR"}

    def test_missing_placeholder_or_live_keys_fail_closed(self):
        for key, secret in (("", ""), ("rzp_test_placeholder_key", "placeholder_secret"),
                            ("rzp_live_ABC123", "safe_secret")):
            with self.subTest(key=key), override_settings(RAZORPAY_KEY_ID=key, RAZORPAY_KEY_SECRET=secret):
                self.assertFalse(gateway.gateway_ready())
                response = self.client.post(reverse("payments:checkout_start", args=[self.plan.slug]), {"accept_terms":"yes"})
                self.assertRedirects(response, reverse("payments:plans"), fetch_redirect_response=False)
                self.assertFalse(Checkout.objects.exists())
                self.assertEqual(self.client.post(reverse("payments:webhook"), "{}", content_type="application/json").status_code, 503)
        self.api.order.create.assert_not_called()

    def test_no_checkout_without_terms_and_secrets_never_in_html(self):
        response = self.client.post(reverse("payments:checkout_start", args=[self.plan.slug]), {})
        self.assertRedirects(response, reverse("payments:plans"), fetch_redirect_response=False)
        self.assertFalse(Checkout.objects.exists())
        checkout = self.start()
        html = self.client.get(reverse("payments:checkout", args=[checkout.reference]))
        self.assertContains(html, "rzp_test_Unit123")
        self.assertContains(html, "149.50")
        self.assertNotContains(html, "safe_unit_secret")
        self.assertNotContains(html, "safe_webhook_secret")
        self.assertFalse(PracticeEntitlement.objects.exists())
        self.assertEqual(current_plan(self.user).billing, "free")

    def test_signature_capture_amount_and_ownership_required(self):
        checkout = self.start()
        self.api.payment.fetch.return_value = self.one_payment()
        self.assertEqual(self.verify(checkout, "pay_Unit123", "order_Unit123", "0" * 64).status_code, 400)
        self.api.payment.fetch.assert_not_called()
        for payment in (self.one_payment(status="authorized"), self.one_payment(amount=1),
                        self.one_payment(order="order_Wrong"), self.one_payment(pid="pay_Different")):
            self.api.payment.fetch.return_value = payment
            self.assertEqual(self.verify(checkout, "pay_Unit123", "order_Unit123").status_code, 400)
            self.assertFalse(PracticeEntitlement.objects.exists())
        self.api.payment.fetch.return_value = self.one_payment()
        self.assertEqual(self.verify(checkout, "pay_Unit123", "order_Unit123").json()["status"], "verified")
        entitlement = PracticeEntitlement.objects.get(checkout=checkout)
        original_end = entitlement.valid_until
        self.assertEqual(current_plan(self.user), self.plan)
        self.assertEqual(self.verify(checkout, "pay_Unit123", "order_Unit123").status_code, 200)
        entitlement.refresh_from_db(); self.assertEqual(entitlement.valid_until, original_end)
        self.assertEqual(Payment.objects.count(), 1)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("payments:checkout", args=[checkout.reference])).status_code, 404)
        self.assertEqual(self.verify(checkout, "pay_Unit123", "order_Unit123").status_code, 404)
        entitlement.valid_until = timezone.now() - timedelta(seconds=1); entitlement.save()
        self.assertTrue(current_plan(self.user).is_default)

    def test_signed_webhook_idempotency_and_refund_revoke(self):
        checkout = self.start()
        event = {"event":"payment.captured", "payload":{"payment":{"entity":self.one_payment()}}}
        self.assertEqual(self.webhook(event, "invalid", "badsecret").status_code, 400)
        self.assertFalse(PracticeEntitlement.objects.exists())
        self.assertEqual(self.webhook(event).status_code, 200)
        access = PracticeEntitlement.objects.get(checkout=checkout)
        first_end = access.valid_until
        self.assertEqual(self.webhook(event).status_code, 200)
        self.assertEqual(self.webhook(event, "another-delivery").status_code, 200)
        access.refresh_from_db(); self.assertEqual(access.valid_until, first_end)
        self.assertEqual(Payment.objects.count(), 1)
        partial = {"event":"refund.processed", "payload":{"refund":{"entity":{
            "id":"rfnd_Part1", "payment_id":"pay_Unit123", "amount":5000}}}}
        full = {"event":"refund.processed", "payload":{"refund":{"entity":{
            "id":"rfnd_Part2", "payment_id":"pay_Unit123", "amount":9950}}}}
        self.assertEqual(self.webhook(partial, "refund-1").status_code, 200)
        access.refresh_from_db(); self.assertFalse(access.is_revoked)
        self.assertEqual(self.webhook(full, "refund-2").status_code, 200)
        access.refresh_from_db(); checkout.refresh_from_db()
        self.assertTrue(access.is_revoked)
        self.assertEqual(checkout.status, "refunded")
        self.assertEqual(Refund.objects.count(), 2)
        self.assertTrue(current_plan(self.user).is_default)
        self.assertEqual(self.webhook(event, "replay-after-refund").status_code, 400)
        self.assertTrue(PracticeEntitlement.objects.get(checkout=checkout).is_revoked)

    def test_webhook_rejects_wrong_amount_and_missing_order_without_activation(self):
        self.start()
        wrong = {"event":"payment.captured", "payload":{"payment":{"entity":self.one_payment(amount=10)}}}
        self.assertEqual(self.webhook(wrong).status_code, 400)
        self.assertFalse(WebhookEvent.objects.exists())
        self.assertFalse(PracticeEntitlement.objects.exists())
        missing = {"event":"payment.captured", "payload":{"payment":{"entity":{**self.one_payment(), "order_id":None}}}}
        self.assertEqual(self.webhook(missing, "missing-order").status_code, 200)
        self.assertFalse(PracticeEntitlement.objects.exists())

    def test_failed_payment_can_be_retried_without_unlocking_or_double_order(self):
        checkout = self.start()
        failed = {"event":"payment.failed", "payload":{"payment":{"entity":self.one_payment(status="failed", pid="pay_Failed123")}}}
        self.assertEqual(self.webhook(failed, "failure-1").status_code, 200)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, "created")
        self.assertEqual(checkout.payments.get().status, "failed")
        self.assertFalse(PracticeEntitlement.objects.exists())
        second = self.client.post(reverse("payments:checkout_start", args=[self.plan.slug]), {"accept_terms":"yes"})
        self.assertEqual(second.status_code, 302)
        self.assertEqual(Checkout.objects.count(), 1)
        captured = {"event":"payment.captured", "payload":{"payment":{"entity":self.one_payment()}}}
        self.assertEqual(self.webhook(captured, "capture-after-retry").status_code, 200)
        self.assertEqual(checkout.payments.count(), 2)
        self.assertEqual(current_plan(self.user), self.plan)
        self.client.post(reverse("payments:checkout_start", args=[self.plan.slug]), {"accept_terms":"yes"})
        self.assertEqual(Checkout.objects.count(), 1)

    def test_default_plan_cannot_be_unset_via_validation(self):
        from django.core.exceptions import ValidationError
        plan = PracticePlan.objects.get(is_default=True)
        plan.is_default = False
        with self.assertRaises(ValidationError):
            plan.full_clean()

    def test_recurring_signature_cycle_renewal_and_cancel(self):
        checkout = self.start(self.recurring)
        now = timezone.now()
        sub = {"id":"sub_Unit123", "plan_id":"plan_Unit123", "status":"active",
               "current_start":int(now.timestamp()), "current_end":int((now+timedelta(days=30)).timestamp())}
        payment = {"id":"pay_Sub123", "subscription_id":"sub_Unit123", "status":"captured", "amount":9900, "currency":"INR"}
        self.api.payment.fetch.return_value = payment
        self.api.subscription.fetch.return_value = sub
        self.assertEqual(self.verify(checkout, "pay_Sub123", "sub_Unit123").status_code, 200)
        entitlement = PracticeEntitlement.objects.get(checkout=checkout)
        self.assertEqual(int(entitlement.valid_until.timestamp()), sub["current_end"])
        next_sub = {**sub, "current_start":sub["current_end"], "current_end":int((now+timedelta(days=60)).timestamp())}
        renewal = {**payment, "id":"pay_Sub456"}
        event = {"event":"subscription.charged", "payload":{"subscription":{"entity":next_sub}, "payment":{"entity":renewal}}}
        self.assertEqual(self.webhook(event, "cycle-2").status_code, 200)
        entitlement.refresh_from_db(); self.assertEqual(int(entitlement.valid_until.timestamp()), next_sub["current_end"])
        self.assertEqual(checkout.payments.count(), 2)
        self.assertEqual(self.webhook(event, "cycle-2-repeat").status_code, 200)
        self.assertEqual(checkout.payments.count(), 2)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse("payments:cancel_subscription", args=[checkout.reference])).status_code, 404)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse("payments:cancel_subscription", args=[checkout.reference])).status_code, 302)
        self.api.subscription.cancel.assert_called_once_with("sub_Unit123", {"cancel_at_cycle_end":1}, timeout=15)
        checkout.refresh_from_db(); self.assertTrue(checkout.cancellation_requested)
        self.assertEqual(current_plan(self.user), self.recurring)
        cancelled = {"event":"subscription.cancelled", "payload":{"subscription":{"entity":sub}}}
        self.assertEqual(self.webhook(cancelled, "cancelled").status_code, 200)
        checkout.refresh_from_db(); self.assertEqual(checkout.status, "cancelled")
        self.assertEqual(current_plan(self.user), self.recurring)  # paid-through access survives cancellation

    def test_recurring_plan_price_or_schedule_mismatch_never_creates_provider_subscription(self):
        self.api.plan.fetch.return_value["item"]["amount"] = 100
        response = self.client.post(reverse("payments:checkout_start", args=[self.recurring.slug]), {"accept_terms":"yes"})
        self.assertRedirects(response, reverse("payments:plans"), fetch_redirect_response=False)
        self.api.subscription.create.assert_not_called()
        self.assertEqual(Checkout.objects.get(plan=self.recurring).status, "initiating")
        self.assertFalse(PracticeEntitlement.objects.exists())
        # An unresolved remote creation is not automatically retried later.
        self.client.post(reverse("payments:checkout_start", args=[self.recurring.slug]), {"accept_terms":"yes"})
        self.assertEqual(Checkout.objects.filter(plan=self.recurring).count(), 1)

    def test_recurring_wrong_period_or_unverified_payment_does_not_unlock(self):
        checkout = self.start(self.recurring)
        now = timezone.now()
        self.api.subscription.fetch.return_value = {"id":"sub_Unit123", "plan_id":"plan_Unit123",
              "current_start":int(now.timestamp()), "current_end":int((now + timedelta(days=401)).timestamp())}
        self.api.payment.fetch.return_value = {"id":"pay_Sub123", "status":"captured", "amount":9900, "currency":"INR"}
        self.assertEqual(self.verify(checkout, "pay_Sub123", "sub_Unit123").status_code, 400)
        self.assertFalse(PracticeEntitlement.objects.exists())

    def test_unresolved_checkout_can_only_be_linked_with_matching_provider_record(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        checkout = Checkout.objects.create(user=self.user, plan=self.plan, billing="one_time", amount_paise=14950,
                                           duration_days=30, currency="INR")
        self.api.order.fetch = MagicMock(return_value={"id":"order_Unit123", "receipt":"wrong", "amount":14950, "currency":"INR"})
        with self.assertRaises(CommandError):
            call_command("reconcile_practice_checkout", str(checkout.reference), "order_Unit123", verbosity=0)
        checkout.refresh_from_db(); self.assertEqual(checkout.status, "initiating")
        self.api.order.fetch.return_value["receipt"] = str(checkout.reference)
        call_command("reconcile_practice_checkout", str(checkout.reference), "order_Unit123", verbosity=0)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, "created")
        self.assertEqual(checkout.provider_order_id, "order_Unit123")
        self.assertFalse(PracticeEntitlement.objects.exists())
        event = {"event":"payment.captured", "payload":{"payment":{"entity":self.one_payment()}}}
        self.assertEqual(self.webhook(event, "replayed-valid").status_code, 200)
        self.assertEqual(current_plan(self.user), self.plan)

    def test_billing_admin_is_read_only_for_transactions(self):
        admin = User.objects.create_superuser("billingadmin", email="billingadmin@example.test", password="x", role=User.Roles.SUPER_ADMIN)
        checkout = self.start()
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("admin:payments_checkout_change", args=[checkout.pk])).status_code, 200)
        self.assertEqual(self.client.post(reverse("admin:payments_checkout_change", args=[checkout.pk]), {"status":"paid"}).status_code, 403)
        checkout.refresh_from_db(); self.assertEqual(checkout.status, "created")
        self.assertFalse(PracticeEntitlement.objects.exists())
