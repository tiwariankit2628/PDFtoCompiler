"""Synthetic signed provider races against the real database; no network calls."""
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, local
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from . import gateway, pack_gateway
from .models import (Checkout, CreditGrant, PackCheckout, PackPayment, PackRefund, Payment,
                     PracticeEntitlement, PracticePlan, Refund, WebhookEvent)
from .packs import debit
from .services import QuotaExceeded


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   ALLOW_TEST_PAYMENT_CREDITS=True, RAZORPAY_KEY_ID="rzp_test_Concurrent123",
                   RAZORPAY_KEY_SECRET="synthetic-secret", RAZORPAY_WEBHOOK_SECRET="synthetic-webhook-secret")
class PackWebhookConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user("webhook_racer", email="webhook-racer@example.test",
                                             role="STUDENT", exam_track="jee")
        self.checkout = PackCheckout.objects.create(user=self.user, product="classroom", amount_paise=9900,
            units=20, currency="INR", status="created", provider_order_id="order_Concurrent123")
        self.payment = {"id": "pay_Concurrent123", "order_id": self.checkout.provider_order_id,
                        "amount": 9900, "currency": "INR", "status": "captured", "captured": True}

    @staticmethod
    def event(event_type, payment):
        return {"event": event_type, "payload": {"payment": {"entity": payment}}}

    def post(self, event, event_id):
        close_old_connections()
        try:
            body = json.dumps(event, separators=(",", ":"))
            sig = hmac.new(b"synthetic-webhook-secret", body.encode(), hashlib.sha256).hexdigest()
            return Client().post(reverse("payments:webhook"), body, content_type="application/json",
                                 HTTP_X_RAZORPAY_SIGNATURE=sig, HTTP_X_RAZORPAY_EVENT_ID=event_id).status_code
        finally:
            close_old_connections()

    def assert_eventually_acknowledged(self, jobs, results):
        for (event, event_id), status in zip(jobs, results):
            self.assertIn(status, (200, 503))
            if status == 503:  # SQLite contention is retryable, never a fake success.
                self.assertEqual(self.post(event, event_id), 200)

    def test_same_payment_failure_and_capture_arrive_concurrently(self):
        barrier = Barrier(2)
        original_capture, original_failure = pack_gateway.activate_capture, pack_gateway.record_failure
        def capture(*args, **kwargs):
            barrier.wait(timeout=10)
            return original_capture(*args, **kwargs)
        def failure(*args, **kwargs):
            barrier.wait(timeout=10)
            return original_failure(*args, **kwargs)
        jobs = [(self.event("payment.failed", {**self.payment, "status": "failed", "captured": False}), "race-failed"),
                (self.event("payment.captured", self.payment), "race-captured")]
        with patch.object(pack_gateway, "activate_capture", capture), patch.object(pack_gateway, "record_failure", failure):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda job: self.post(*job), jobs))
        self.assert_eventually_acknowledged(jobs, results)
        self.assertEqual(PackPayment.objects.get(checkout=self.checkout).status, "captured")
        self.assertEqual(PackPayment.objects.count(), 1)
        grant = CreditGrant.objects.get(checkout=self.checkout)
        self.assertEqual((grant.remaining, grant.is_revoked), (20, False))
        self.assertEqual(WebhookEvent.objects.filter(event_id__in=["race-failed", "race-captured"]).count(), 2)

    def test_duplicate_capture_event_arrives_concurrently(self):
        barrier = Barrier(2)
        original = pack_gateway.activate_capture
        def capture(*args, **kwargs):
            barrier.wait(timeout=10)  # both requests passed the event-id lookup
            return original(*args, **kwargs)
        event = self.event("payment.captured", self.payment)
        jobs = [(event, "same-event-id"), (event, "same-event-id")]
        with patch.object(pack_gateway, "activate_capture", capture):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda job: self.post(*job), jobs))
        self.assert_eventually_acknowledged(jobs, results)
        self.assertEqual(PackPayment.objects.count(), 1)
        self.assertEqual(CreditGrant.objects.count(), 1)
        self.assertEqual(CreditGrant.objects.get(checkout=self.checkout).remaining, 20)
        self.assertEqual(WebhookEvent.objects.filter(event_id="same-event-id").count(), 1)

    @skipUnless(connection.vendor == "postgresql", "Requires independent PostgreSQL row locks")
    def test_early_refund_is_atomic_against_capture_and_credit_use(self):
        with transaction.atomic():
            debit(self.user, "classroom", "prior-free")
        refund = {"id": "rfnd_Concurrent123", "payment_id": self.payment["id"], "amount": 9900,
                  "currency": "INR", "status": "processed"}
        refund_event = {"event": "refund.processed", "payload": {
            "payment": {"entity": {**self.payment, "status": "refunded"}}, "refund": {"entity": refund}}}
        capture_event = self.event("payment.captured", self.payment)
        grant_created, capture_waiting, debit_waiting, release = Event(), Event(), Event(), Event()
        actor = local()
        original_apply, original_lock = pack_gateway.apply_refund, pack_gateway.lock_practice_user
        from . import packs
        original_debit_lock = packs.lock_practice_user

        def paused_apply(*args, **kwargs):
            grant_created.set()  # exists only in the uncommitted refund transaction
            if not release.wait(timeout=12):
                raise AssertionError("Refund was not released")
            return original_apply(*args, **kwargs)

        def observed_lock(user):
            if getattr(actor, "name", "") == "capture":
                capture_waiting.set()
            return original_lock(user)

        def observed_debit_lock(user):
            if getattr(actor, "name", "") == "debit":
                debit_waiting.set()
            return original_debit_lock(user)

        def send(name, event, event_id):
            actor.name = name
            return self.post(event, event_id)

        def try_debit():
            close_old_connections()
            actor.name = "debit"
            try:
                with transaction.atomic():
                    debit(self.user, "classroom", "must-not-spend-refunded-grant")
                return "spent"
            except QuotaExceeded:
                return "blocked"
            finally:
                close_old_connections()

        with patch.object(pack_gateway, "apply_refund", paused_apply), \
             patch.object(pack_gateway, "lock_practice_user", observed_lock), \
             patch.object(packs, "lock_practice_user", observed_debit_lock):
            with ThreadPoolExecutor(max_workers=3) as pool:
                first = pool.submit(send, "refund", refund_event, "refund-first")
                try:
                    self.assertTrue(grant_created.wait(timeout=10), "Refund did not create the uncommitted grant")
                    self.assertFalse(CreditGrant.objects.filter(checkout=self.checkout).exists())
                    capture = pool.submit(send, "capture", capture_event, "capture-second")
                    spend = pool.submit(try_debit)
                    self.assertTrue(capture_waiting.wait(timeout=10), "Capture did not attempt the account lock")
                    self.assertTrue(debit_waiting.wait(timeout=10), "Debit did not attempt the account lock")
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=30), 200)
                self.assertEqual(capture.result(timeout=30), 200)
                self.assertEqual(spend.result(timeout=30), "blocked")
        self.assertEqual(PackPayment.objects.get(checkout=self.checkout).status, "refunded")
        self.assertEqual(PackRefund.objects.count(), 1)
        grant = CreditGrant.objects.get(checkout=self.checkout)
        self.assertTrue(grant.is_revoked)
        self.assertEqual(grant.remaining, 20)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   ALLOW_TEST_PAYMENT_CREDITS=True, RAZORPAY_KEY_ID="rzp_test_Concurrent123",
                   RAZORPAY_KEY_SECRET="synthetic-secret", RAZORPAY_WEBHOOK_SECRET="synthetic-webhook-secret")
class LegacyWebhookConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user("legacy_webhook_racer", email="legacy-race@example.test",
                                             role="STUDENT", exam_track="jee")
        plan = PracticePlan.objects.create(name="Legacy prepaid", slug="legacy-race", billing="one_time",
                                          price_paise=14950, duration_days=30, monthly_pdf_uploads=20)
        self.checkout = Checkout.objects.create(user=self.user, plan=plan, billing="one_time", amount_paise=14950,
            duration_days=30, currency="INR", status="created", provider_order_id="order_LegacyRace")
        self.payment = {"id": "pay_LegacyRace", "order_id": self.checkout.provider_order_id,
                        "amount": 14950, "currency": "INR", "status": "captured", "captured": True}

    post = PackWebhookConcurrencyTests.post
    event = staticmethod(PackWebhookConcurrencyTests.event)
    assert_eventually_acknowledged = PackWebhookConcurrencyTests.assert_eventually_acknowledged

    def test_same_legacy_failure_and_capture_arrive_concurrently(self):
        barrier = Barrier(2)
        original_capture, original_failure = gateway.activate_capture, gateway.record_failure
        def capture(*args, **kwargs):
            barrier.wait(timeout=10)
            return original_capture(*args, **kwargs)
        def failure(*args, **kwargs):
            barrier.wait(timeout=10)
            return original_failure(*args, **kwargs)
        jobs = [(self.event("payment.failed", {**self.payment, "status": "failed", "captured": False}), "legacy-race-failed"),
                (self.event("payment.captured", self.payment), "legacy-race-captured")]
        with patch.object(gateway, "activate_capture", capture), patch.object(gateway, "record_failure", failure):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda job: self.post(*job), jobs))
        self.assert_eventually_acknowledged(jobs, results)
        self.assertEqual(Payment.objects.get(checkout=self.checkout).status, "captured")
        self.assertEqual(Payment.objects.count(), 1)
        self.assertFalse(PracticeEntitlement.objects.get(checkout=self.checkout).is_revoked)
        self.assertEqual(WebhookEvent.objects.filter(event_id__in=["legacy-race-failed", "legacy-race-captured"]).count(), 2)

    @skipUnless(connection.vendor == "postgresql", "Requires independent PostgreSQL row locks")
    def test_early_legacy_full_refund_is_atomic_against_capture_and_pdf_use(self):
        with transaction.atomic():
            debit(self.user, "pdf", "legacy-prior-free")
        refund = {"id": "rfnd_LegacyRace", "payment_id": self.payment["id"], "amount": 14950,
                  "currency": "INR", "status": "processed"}
        refund_event = {"event": "refund.processed", "payload": {
            "payment": {"entity": {**self.payment, "status": "refunded"}}, "refund": {"entity": refund}}}
        capture_event = self.event("payment.captured", self.payment)
        entitlement_created, capture_waiting, debit_waiting, release = Event(), Event(), Event(), Event()
        actor = local()
        original_apply, original_lock = gateway.apply_refund, gateway.lock_practice_user
        from . import packs
        original_debit_lock = packs.lock_practice_user

        def paused_apply(*args, **kwargs):
            entitlement_created.set()
            if not release.wait(timeout=12):
                raise AssertionError("Refund was not released")
            return original_apply(*args, **kwargs)

        def observed_lock(user):
            if getattr(actor, "name", "") == "capture":
                capture_waiting.set()
            return original_lock(user)

        def observed_debit_lock(user):
            if getattr(actor, "name", "") == "debit":
                debit_waiting.set()
            return original_debit_lock(user)

        def send(name, event, event_id):
            actor.name = name
            return self.post(event, event_id)

        def try_debit():
            close_old_connections()
            actor.name = "debit"
            try:
                with transaction.atomic():
                    debit(self.user, "pdf", "legacy-must-not-spend-refunded-entitlement")
                return "spent"
            except QuotaExceeded:
                return "blocked"
            finally:
                close_old_connections()

        with patch.object(gateway, "apply_refund", paused_apply), \
             patch.object(gateway, "lock_practice_user", observed_lock), \
             patch.object(packs, "lock_practice_user", observed_debit_lock):
            with ThreadPoolExecutor(max_workers=3) as pool:
                first = pool.submit(send, "refund", refund_event, "legacy-refund-first")
                try:
                    self.assertTrue(entitlement_created.wait(timeout=10), "Refund did not create the uncommitted entitlement")
                    self.assertFalse(PracticeEntitlement.objects.filter(checkout=self.checkout).exists())
                    capture = pool.submit(send, "capture", capture_event, "legacy-capture-second")
                    spend = pool.submit(try_debit)
                    self.assertTrue(capture_waiting.wait(timeout=10), "Capture did not attempt the account lock")
                    self.assertTrue(debit_waiting.wait(timeout=10), "Debit did not attempt the account lock")
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=30), 200)
                self.assertEqual(capture.result(timeout=30), 200)
                self.assertEqual(spend.result(timeout=30), "blocked")
        self.assertEqual(Payment.objects.get(checkout=self.checkout).status, "refunded")
        self.assertEqual(Refund.objects.count(), 1)
        self.assertTrue(PracticeEntitlement.objects.get(checkout=self.checkout).is_revoked)
