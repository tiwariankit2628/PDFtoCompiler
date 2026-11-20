"""PostgreSQL-only interleavings for Google state, linking and onboarding."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import time

from django.contrib.auth.models import AnonymousUser
from django.db import close_old_connections, connection, transaction
from django.test import Client, RequestFactory, TransactionTestCase, override_settings
from django.urls import reverse

from apps.payments.services import lock_practice_user
from .google_signin import FLOW_SESSION_KEY, SIGNUP_SESSION_KEY, configuration, consume_authorization_state, create_authorization_url
from .models import GoogleIdentity, GoogleOAuthState, User
from .test_google_signin import GOOGLE_SETTINGS, HOST


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row locks and independent connections required")
@override_settings(**GOOGLE_SETTINGS)
class GoogleConcurrencyTests(TransactionTestCase):
    def profile(self, *, email="new@gmail.com", subject="synthetic-sub"):
        return {"subject": subject, "email": email, "first_name": "New", "last_name": "User", "authoritative": True}

    def run_parallel(self, tasks):
        def run(task):
            close_old_connections()
            try:
                return task()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = [executor.submit(run, task) for task in tasks]
            return [future.result(timeout=30) for future in futures]

    def pending_client(self, profile):
        client = Client()
        session = client.session
        session[SIGNUP_SESSION_KEY] = {"profile": profile, "created_at": int(time.time()), "next": ""}
        session.save()
        return client

    def test_two_workers_consume_the_same_state_only_once(self):
        request = RequestFactory().post("/auth/google/start/", **HOST)
        request.user = AnonymousUser()
        request.session = {}
        url = create_authorization_url(request, configuration(), mode="signin")
        state = parse_qs(urlsplit(url).query)["state"][0]
        flow = request.session[FLOW_SESSION_KEY].copy()
        barrier = Barrier(2)

        def consume():
            local = RequestFactory().get("/auth/google/callback/", **HOST)
            local.session = {FLOW_SESSION_KEY: flow.copy()}
            barrier.wait(timeout=10)
            return consume_authorization_state(local, state)

        results = self.run_parallel([consume, consume])
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertIsNotNone(GoogleOAuthState.objects.get(pk=flow["digest"]).consumed_at)

    def _race_signup(self, profiles, barrier_model):
        clients = [self.pending_client(profile) for profile in profiles]
        barrier = Barrier(2)
        manager = barrier_model.objects
        method_name = "create_user" if barrier_model is User else "create"
        original = getattr(manager, method_name)

        def create(*args, **kwargs):
            barrier.wait(timeout=10)
            return original(*args, **kwargs)

        def submit(client):
            return client.post(reverse("accounts:google_signup"), {
                "first_name": "New", "last_name": "User", "role": "STUDENT", "target_exam": "JEE"}, **HOST)

        with patch.object(manager, method_name, side_effect=create):
            responses = self.run_parallel([lambda: submit(clients[0]), lambda: submit(clients[1])])
        self.assertCountEqual([response.url for response in responses],
                              [reverse("core:dashboard_router"), reverse("accounts:login")])
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(GoogleIdentity.objects.count(), 1)
        self.assertEqual(GoogleIdentity.objects.get().user_id, User.objects.get().pk)
        self.assertEqual(User.objects.get().exam_track, "jee")

    def test_concurrent_signup_same_email_creates_one_account(self):
        self._race_signup([self.profile(), self.profile()], User)

    def test_concurrent_signup_same_subject_rolls_back_losing_account(self):
        self._race_signup([self.profile(email="first@gmail.com"), self.profile(email="second@gmail.com")], GoogleIdentity)

    def test_concurrent_first_password_cannot_replace_the_winner(self):
        user = User.objects.create_user("google-only", "new@gmail.com", None, role="STUDENT", exam_track="jee", is_verified=True)
        GoogleIdentity.objects.create(user=user, subject="synthetic-sub")
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(user)
        barrier = Barrier(2)
        original_lock = lock_practice_user

        def synchronize(fresh):
            barrier.wait(timeout=10)
            return original_lock(fresh)

        def submit(client, password):
            return client.post(reverse("accounts:google_add_password"),
                               {"new_password1": password, "new_password2": password}, **HOST)

        with patch("apps.accounts.google_views.lock_practice_user", side_effect=synchronize):
            responses = self.run_parallel([lambda: submit(clients[0], "PasswordFirst987!"),
                                           lambda: submit(clients[1], "PasswordSecond987!")])
        self.assertEqual([response.status_code for response in responses], [302, 302])
        user.refresh_from_db()
        self.assertEqual(sum(user.check_password(value) for value in ("PasswordFirst987!", "PasswordSecond987!")), 1)
        self.assertEqual(GoogleIdentity.objects.get().user_id, user.pk)

    def test_email_change_holding_lock_prevents_stale_google_link(self):
        user = User.objects.create_user("pending", "new@gmail.com", "old-password", role="STUDENT", exam_track="jee",
                                        verification_required=True, is_verified=False)
        client = Client()
        client.force_login(user)
        response = client.post(reverse("accounts:google_start"), {"mode": "link"}, **HOST)
        state = parse_qs(urlsplit(response.url).query)["state"][0]
        acquired, attempted = Event(), Event()
        original_lock = lock_practice_user

        def observed_lock(fresh):
            attempted.set()
            return original_lock(fresh)

        def change_address():
            with transaction.atomic():
                current = original_lock(user)
                current.email = "changed@gmail.com"
                current.save(update_fields=["email"])
                acquired.set()
                if not attempted.wait(timeout=10):
                    raise AssertionError("Google callback did not try to acquire the account lock")

        def callback():
            if not acquired.wait(timeout=10):
                raise AssertionError("Email-change transaction did not acquire the account lock")
            return client.get(reverse("accounts:google_callback"), {"state": state, "code": "synthetic"}, **HOST)

        with patch("apps.accounts.google_views.lock_practice_user", side_effect=observed_lock), \
             patch("apps.accounts.google_views.exchange_code", return_value=self.profile()):
            results = self.run_parallel([change_address, callback])
        self.assertEqual(results[1].url, reverse("accounts:verification_pending"))
        user.refresh_from_db()
        self.assertEqual(user.email, "changed@gmail.com")
        self.assertFalse(user.is_verified)
        self.assertTrue(user.check_password("old-password"))
        self.assertFalse(GoogleIdentity.objects.exists())
