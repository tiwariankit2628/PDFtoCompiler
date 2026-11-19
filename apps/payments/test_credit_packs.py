"""Commercial ledger and Razorpay sandbox tests; no external transactions."""
import hashlib
import hmac
import io
import json
from datetime import timedelta
from itertools import count
from unittest.mock import MagicMock, patch

import razorpay
from django.core.exceptions import PermissionDenied
from django.core.management import call_command, CommandError
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from . import gateway, pack_gateway
from .models import CreditGrant, CreditUse, PackCheckout, PackPayment, PackRefund, PracticeEntitlement
from .packs import PRODUCTS, balances, debit, has_series_access, usable_grants
from .services import QuotaExceeded
from .test_helpers import sandbox_grant


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   ALLOW_TEST_PAYMENT_CREDITS=True)
class CreditLedgerTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user("credit-student", email="cs@example.test", role="STUDENT", exam_track="jee")
        self.teacher = User.objects.create_user("credit-teacher", email="ct@example.test", role="TEACHER")

    def use(self, user, kind, key):
        with transaction.atomic():
            return debit(user, kind, key)

    def test_published_products_and_independent_one_time_allowances(self):
        self.assertEqual([(PRODUCTS[k].amount_paise, PRODUCTS[k].units) for k in
                          ("pdf", "classroom", "activation", "series_jee", "series_neet")],
                         [(29900, 20), (9900, 20), (29900, 20), (29900, 1), (29900, 1)])
        self.assertTrue(self.use(self.student, "pdf", "fixture-pdf-1").is_free)
        self.assertTrue(self.use(self.student, "classroom", "fixture-class-1").is_free)
        self.assertTrue(self.use(self.teacher, "activation", "fixture-activation-1").is_free)
        with self.assertRaises(QuotaExceeded):
            self.use(self.student, "pdf", "fixture-pdf-2")
        with self.assertRaises(QuotaExceeded):
            self.use(self.student, "classroom", "fixture-class-2")
        with self.assertRaises(QuotaExceeded):
            self.use(self.teacher, "activation", "fixture-activation-2")
        with self.assertRaises(ValueError):
            self.use(self.student, "activation", "wrong-role")
        self.assertFalse(CreditUse.objects.filter(resource_key="wrong-role").exists())

    def test_debit_rechecks_fresh_verification_and_role(self):
        # Simulate a request that authenticated before an email or role change.
        User.objects.filter(pk=self.student.pk).update(verification_required=True, is_verified=False)
        with self.assertRaises(PermissionDenied):
            self.use(self.student, "pdf", "stale-verification")
        self.assertFalse(CreditUse.objects.filter(resource_key="stale-verification").exists())
        User.objects.filter(pk=self.student.pk).update(is_verified=True, role="TEACHER")
        with self.assertRaises(PermissionDenied):
            self.use(self.student, "pdf", "stale-role")
        self.assertFalse(CreditUse.objects.filter(resource_key="stale-role").exists())

    def test_twenty_paid_credits_do_not_expire_or_reset_each_month(self):
        self.use(self.student, "pdf", "fixture-free")
        grant = sandbox_grant(self.student, "pdf")
        future = timezone.now() + timedelta(days=365 * 4)
        with patch("django.utils.timezone.now", return_value=future):
            for i in range(20):
                use = self.use(self.student, "pdf", f"fixture-paid-{i}")
                self.assertEqual(use.grant_id, grant.pk)
            with self.assertRaises(QuotaExceeded):
                self.use(self.student, "pdf", "fixture-over-limit")
        grant.refresh_from_db()
        self.assertEqual(grant.remaining, 0)
        self.assertEqual(CreditUse.objects.filter(user=self.student, kind="pdf", reversed_at__isnull=True).count(), 21)
        self.assertEqual(next(row for row in balances(self.student) if row["kind"] == "pdf")["paid"], 0)

    def test_failed_operation_rolls_back_its_debit_and_cross_track_series_are_distinct(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                debit(self.student, "pdf", "rolled-back")
                raise RuntimeError("simulate operation failure")
        self.assertFalse(CreditUse.objects.filter(resource_key="rolled-back").exists())
        self.assertTrue(self.use(self.student, "pdf", "later").is_free)
        sandbox_grant(self.student, "series_jee")
        self.assertTrue(has_series_access(self.student, "jee"))
        self.assertFalse(has_series_access(self.student, "neet"))

    @override_settings(ALLOW_TEST_PAYMENT_CREDITS=False)
    def test_sandbox_grants_are_never_usable_when_test_flag_off(self):
        grant = sandbox_grant(self.student, "classroom")
        self.assertFalse(usable_grants(self.student, "classroom").exists())
        self.assertEqual(next(row for row in balances(self.student) if row["kind"] == "classroom")["paid"], 0)
        self.use(self.student, "classroom", "free-not-test-payment")
        with self.assertRaises(QuotaExceeded):
            self.use(self.student, "classroom", "no-test-payment")
        grant.refresh_from_db()
        self.assertEqual(grant.remaining, 20)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   ALLOW_TEST_PAYMENT_CREDITS=True, LEGACY_BILLING_NEW_CHECKOUTS=False,
                   RAZORPAY_KEY_ID="rzp_test_Unit123", RAZORPAY_KEY_SECRET="unit_secret",
                   RAZORPAY_WEBHOOK_SECRET="unit_webhook_secret")
class PackGatewayTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user("pack-user", email="pack@example.test", role="STUDENT", exam_track="jee")
        self.other = User.objects.create_user("pack-other", email="other-pack@example.test", role="STUDENT", exam_track="neet")
        self.teacher = User.objects.create_user("pack-teacher", email="teacher-pack@example.test", role="TEACHER")
        self.client.force_login(self.student)
        self.api = razorpay.Client(auth=("rzp_test_Unit123", "unit_secret"))
        ids = count(1)
        self.api.order.create = MagicMock(side_effect=lambda data, **kw: {
            "id": f"order_Unit{next(ids)}", "amount": data["amount"], "currency": data["currency"]})
        self.api.payment.fetch = MagicMock()
        patcher = patch("apps.payments.gateway.client", return_value=self.api)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def signature(message, secret="unit_secret"):
        return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    def checkout(self, product="classroom"):
        response = self.client.post(reverse("payments:pack_start", args=[product]), {"accept_terms": "yes"})
        self.assertEqual(response.status_code, 302)
        checkout = PackCheckout.objects.get(user=self.client.session.get("_auth_user_id"), product=product)
        self.assertEqual(checkout.status, PackCheckout.Status.CREATED)
        return checkout

    def payment(self, checkout, **overrides):
        return {"id": "pay_Unit123", "order_id": checkout.provider_order_id,
                "status": "captured", "amount": checkout.amount_paise, "currency": "INR", **overrides}

    def verify(self, checkout, *, signature=None, pid="pay_Unit123", order=None):
        order = order or checkout.provider_order_id
        data = {"razorpay_order_id": order, "razorpay_payment_id": pid,
                "razorpay_signature": signature or self.signature(f"{order}|{pid}")}
        return self.client.post(reverse("payments:pack_verify", args=[checkout.reference]),
                                json.dumps(data), content_type="application/json")

    def webhook(self, event, event_id, secret="unit_webhook_secret"):
        body = json.dumps(event, separators=(",", ":"))
        return self.client.post(reverse("payments:webhook"), body, content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE=self.signature(body, secret), HTTP_X_RAZORPAY_EVENT_ID=event_id)

    def test_signature_capture_order_amount_currency_and_ownership_required(self):
        checkout = self.checkout()
        html = self.client.get(reverse("payments:pack_checkout", args=[checkout.reference]))
        self.assertContains(html, "99.00")
        self.assertContains(html, "rzp_test_Unit123")
        self.assertNotContains(html, "unit_secret")
        self.assertNotContains(html, "unit_webhook_secret")
        self.api.payment.fetch.return_value = self.payment(checkout)
        self.assertEqual(self.verify(checkout, signature="0" * 64).status_code, 400)
        self.api.payment.fetch.assert_not_called()
        for payment in (self.payment(checkout, status="authorized"), self.payment(checkout, amount=100),
                        self.payment(checkout, currency="USD"), self.payment(checkout, order_id="order_Wrong"),
                        self.payment(checkout, id="pay_Different")):
            self.api.payment.fetch.return_value = payment
            self.assertEqual(self.verify(checkout).status_code, 400)
            self.assertFalse(CreditGrant.objects.exists())
        self.api.payment.fetch.return_value = self.payment(checkout)
        self.client.force_login(self.other)
        self.assertEqual(self.verify(checkout).status_code, 404)
        self.assertEqual(self.client.get(reverse("payments:pack_checkout", args=[checkout.reference])).status_code, 404)
        self.client.force_login(self.student)
        self.assertEqual(self.verify(checkout).status_code, 200)
        self.assertEqual(self.verify(checkout).status_code, 200)
        grant = CreditGrant.objects.get(checkout=checkout)
        self.assertEqual((grant.product, grant.units, grant.remaining, grant.is_test), ("classroom", 20, 20, True))
        self.assertEqual(PackPayment.objects.count(), 1)

    def test_signed_webhooks_idempotence_and_refunds_suspend_unused_access(self):
        checkout = self.checkout("pdf")
        payment = self.payment(checkout)
        captured = {"event": "payment.captured", "payload": {"payment": {"entity": payment}}}
        self.assertEqual(self.webhook(captured, "bad", "wrong").status_code, 400)
        self.assertFalse(CreditGrant.objects.exists())
        self.assertEqual(self.webhook(captured, "capture").status_code, 200)
        self.assertEqual(self.webhook(captured, "capture").status_code, 200)
        self.assertEqual(self.webhook(captured, "capture-again").status_code, 200)
        self.assertEqual(CreditGrant.objects.count(), 1)
        with transaction.atomic():
            debit(self.student, "pdf", "before-refund-free")
            paid_use = debit(self.student, "pdf", "before-refund-paid")
        grant = CreditGrant.objects.get(checkout=checkout)
        grant.refresh_from_db()
        self.assertEqual(grant.remaining, 19)
        partial = {"event": "refund.processed", "payload": {"refund": {"entity":
                    {"id": "rfnd_Part1", "payment_id": payment["id"], "amount": 10000}}}}
        self.assertEqual(self.webhook(partial, "partial").status_code, 200)
        self.assertEqual(self.webhook(partial, "partial-duplicate").status_code, 200)
        grant.refresh_from_db()
        self.assertTrue(grant.is_revoked)
        self.assertEqual(grant.remaining, 19)
        self.assertIsNone(paid_use.reversed_at)
        with self.assertRaises(QuotaExceeded):
            with transaction.atomic():
                debit(self.student, "pdf", "after-refund")
        full = {"event": "refund.processed", "payload": {"refund": {"entity":
                 {"id": "rfnd_Part2", "payment_id": payment["id"], "amount": 19900}}}}
        self.assertEqual(self.webhook(full, "full").status_code, 200)
        self.assertEqual(PackRefund.objects.count(), 2)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, PackCheckout.Status.REFUNDED)
        self.assertEqual(self.webhook(captured, "after-refund-replay").status_code, 400)
        self.assertTrue(CreditUse.objects.filter(pk=paid_use.pk).exists())

    def test_failure_retry_and_uncertain_order_do_not_activate_or_create_duplicate_orders(self):
        checkout = self.checkout("classroom")
        failed = {"event": "payment.failed", "payload": {"payment": {"entity":
                  self.payment(checkout, status="failed", id="pay_Failed1")}}}
        self.assertEqual(self.webhook(failed, "fail").status_code, 200)
        self.assertEqual(PackPayment.objects.get().status, "failed")
        self.assertFalse(CreditGrant.objects.exists())
        self.client.post(reverse("payments:pack_start", args=["classroom"]), {"accept_terms": "yes"})
        self.assertEqual(PackCheckout.objects.filter(user=self.student, product="classroom").count(), 1)
        self.api.payment.fetch.return_value = self.payment(checkout)
        self.assertEqual(self.verify(checkout).status_code, 200)
        self.assertEqual(PackPayment.objects.count(), 2)
        self.api.order.create.side_effect = TimeoutError("provider uncertainty")
        self.client.post(reverse("payments:pack_start", args=["pdf"]), {"accept_terms": "yes"})
        self.assertEqual(PackCheckout.objects.get(user=self.student, product="pdf").status, "initiating")
        calls = self.api.order.create.call_count
        self.client.post(reverse("payments:pack_start", args=["pdf"]), {"accept_terms": "yes"})
        self.assertEqual(self.api.order.create.call_count, calls)
        self.assertFalse(CreditGrant.objects.filter(product="pdf").exists())

    def test_reconciliation_links_only_matching_test_order_and_never_grants_access(self):
        self.api.order.create.side_effect = TimeoutError("lost response")
        self.client.post(reverse("payments:pack_start", args=["pdf"]), {"accept_terms": "yes"})
        checkout = PackCheckout.objects.get(user=self.student, product="pdf")
        self.assertEqual(checkout.status, "initiating")
        order = {"id": "order_Reconciled", "amount": checkout.amount_paise,
                 "currency": checkout.currency, "receipt": str(checkout.reference)}
        self.api.order.fetch = MagicMock(return_value={**order, "receipt": "wrong"})
        with self.assertRaises(CommandError):
            call_command("reconcile_pack_checkout", str(checkout.reference), order["id"], stdout=io.StringIO())
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, "initiating")
        self.api.order.fetch.return_value = order
        out = io.StringIO()
        call_command("reconcile_pack_checkout", str(checkout.reference), order["id"], stdout=out)
        checkout.refresh_from_db()
        self.assertEqual((checkout.status, checkout.provider_order_id), ("created", order["id"]))
        self.assertIn("No credits activated", out.getvalue())
        self.assertFalse(CreditGrant.objects.exists())
        capture = {"event": "payment.captured", "payload": {"payment": {"entity": self.payment(checkout)}}}
        self.assertEqual(self.webhook(capture, "reconciled-capture").status_code, 200)
        self.assertEqual(CreditGrant.objects.get(checkout=checkout).remaining, 20)

    def test_roles_terms_series_readiness_and_legacy_checkout_cutover(self):
        self.assertEqual(self.client.post(reverse("payments:pack_start", args=["activation"]), {"accept_terms": "yes"}).status_code, 404)
        self.assertEqual(self.client.post(reverse("payments:pack_start", args=["series_neet"]), {"accept_terms": "yes"}).status_code, 404)
        self.assertEqual(self.client.post(reverse("payments:pack_start", args=["pdf"]), {}).status_code, 302)
        self.assertEqual(self.client.post(reverse("payments:pack_start", args=["series_jee"]), {"accept_terms": "yes"}).status_code, 302)
        self.assertFalse(PackCheckout.objects.exists())  # series slots are intentionally empty
        self.assertEqual(self.client.post(reverse("payments:checkout_start", args=["anything"]), {"accept_terms": "yes"}).status_code, 302)
        self.api.order.create.assert_not_called()
        self.client.force_login(self.teacher)
        self.assertContains(self.client.get(reverse("payments:plans")), "Classroom teacher pack")
        self.assertEqual(self.client.post(reverse("payments:pack_start", args=["pdf"]), {"accept_terms": "yes"}).status_code, 404)

    def test_test_flag_off_and_live_keys_fail_closed(self):
        checkout = self.checkout("pdf")
        payment = self.payment(checkout)
        with override_settings(ALLOW_TEST_PAYMENT_CREDITS=False):
            self.assertFalse(gateway.gateway_ready())
            with self.assertRaises(gateway.InvalidPayment):
                pack_gateway.activate_capture(checkout.pk, payment)
            self.assertEqual(self.webhook({"event": "payment.captured", "payload": {"payment": {"entity": payment}}}, "off").status_code, 503)
        with override_settings(RAZORPAY_KEY_ID="rzp_live_ActualLiveLooking"):
            self.assertFalse(gateway.gateway_ready())
            self.assertEqual(self.client.post(reverse("payments:pack_start", args=["classroom"]), {"accept_terms": "yes"}).status_code, 302)
        self.assertFalse(CreditGrant.objects.exists())
        self.assertFalse(PracticeEntitlement.objects.exists())
