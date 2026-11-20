"""PostgreSQL races: a TOTP cannot be replayed and lockouts cannot be lost."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch

import pyotp
from cryptography.fernet import Fernet
from django.db import close_old_connections, connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.founder_security import encrypt_secret
from apps.core.models import AuditLog, FounderAccess
from apps.payments.services import lock_practice_user

KEY = Fernet.generate_key().decode()
SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
PASSWORD = "SyntheticConcurrentPassword987!"


@skipUnless(connection.vendor == "postgresql", "PostgreSQL independent row locks required")
@override_settings(DEBUG=True, FOUNDER_TOTP_ENCRYPTION_KEY=KEY,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class FounderConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser("concurrent-founder", "concurrent-founder@example.test", PASSWORD)
        self.access = FounderAccess.objects.create(user=self.owner, is_enabled=True,
                                                    encrypted_totp_secret=encrypt_secret(SECRET))

    def clients(self, n):
        clients = [Client() for _ in range(n)]
        for client in clients:
            client.force_login(self.owner)
        return clients

    def parallel_posts(self, clients, data):
        barrier = Barrier(len(clients))
        def submit(client):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return client.post(reverse("founder:verify"), data)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(clients)) as pool:
            futures = [pool.submit(submit, client) for client in clients]
            return [future.result(timeout=30) for future in futures]

    def test_two_sessions_cannot_accept_the_same_totp(self):
        clients = self.clients(2)
        now = timezone.now()
        code = pyotp.TOTP(SECRET).at(int(now.timestamp()))
        with patch("apps.core.founder_security.timezone.now", return_value=now):
            results = self.parallel_posts(clients, {"password": PASSWORD, "code": code})
        self.assertCountEqual([response.status_code for response in results], [302, 200])
        self.assertEqual(AuditLog.objects.filter(action="FOUNDER_UNLOCKED").count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="FOUNDER_UNLOCK_FAILED").count(), 1)
        self.access.refresh_from_db()
        self.assertEqual(self.access.last_accepted_step, int(now.timestamp()) // 30)

    def test_five_concurrent_failures_produce_one_persistent_lockout(self):
        clients = self.clients(5)
        results = self.parallel_posts(clients, {"password": PASSWORD, "code": "not-a-code"})
        self.assertEqual(sum(response.status_code == 429 for response in results), 1)
        self.assertEqual(sum(response.status_code == 200 for response in results), 4)
        self.access.refresh_from_db()
        self.assertEqual(self.access.failed_attempts, 5)
        self.assertGreater(self.access.locked_until, timezone.now())
        self.assertEqual(AuditLog.objects.filter(action="FOUNDER_UNLOCK_FAILED").count(), 5)

    def test_revocation_holding_account_lock_blocks_inflight_verification(self):
        client = self.clients(1)[0]
        acquired, attempted = Event(), Event()
        code = pyotp.TOTP(SECRET).now()
        original_lock = lock_practice_user

        def observed_lock(user):
            attempted.set()
            return original_lock(user)

        def revoke():
            close_old_connections()
            try:
                with transaction.atomic():
                    original_lock(self.owner)
                    access = FounderAccess.objects.select_for_update().get(pk=self.access.pk)
                    access.is_enabled = False
                    access.save(update_fields=["is_enabled"])
                    acquired.set()
                    if not attempted.wait(timeout=10):
                        raise AssertionError("Verification did not reach the account lock")
            finally:
                close_old_connections()

        def verify():
            close_old_connections()
            try:
                if not acquired.wait(timeout=10):
                    raise AssertionError("Revocation did not acquire the account lock")
                return client.post(reverse("founder:verify"), {"password": PASSWORD, "code": code})
            finally:
                close_old_connections()

        with patch("apps.payments.services.lock_practice_user", side_effect=observed_lock):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(revoke), pool.submit(verify)]
                results = [future.result(timeout=30) for future in futures]
        self.assertEqual(results[1].status_code, 403)
        self.assertFalse(AuditLog.objects.filter(action="FOUNDER_UNLOCKED").exists())
