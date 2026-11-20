"""Google Sign-In security and workflow tests; all provider responses are synthetic."""
import base64
import hashlib
import json
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth import SESSION_KEY
from django.core import mail
from django.db import transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from google.auth import crypt, jwt

from apps.exams.models import Category, Exam, ExamAttempt
from apps.payments.models import CreditUse
from apps.payments.packs import balances
from .google_signin import (
    FLOW_SESSION_KEY, SIGNUP_SESSION_KEY, GoogleSignInError, _verified_profile,
    configuration, exchange_code,
)
from .models import GoogleIdentity, GoogleOAuthState, User


CLIENT_ID = "123456789-testclient.apps.googleusercontent.com"
CLIENT_SECRET = "synthetic-test-secret"
HOST = {"HTTP_HOST": "127.0.0.1:8000"}
GOOGLE_SETTINGS = dict(
    GOOGLE_SIGN_IN_ENABLED=True,
    GOOGLE_OAUTH_CLIENT_ID=CLIENT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET=CLIENT_SECRET,
    PUBLIC_BASE_URL="http://127.0.0.1:8000",
    DEBUG=True,
    ALLOWED_HOSTS=["127.0.0.1", "testserver"],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)


@override_settings(**GOOGLE_SETTINGS)
class GoogleSignInWorkflowTests(TestCase):
    def profile(self, *, subject="google-subject-123", email="student@gmail.com", authoritative=True):
        return {"subject": subject, "email": email, "first_name": "Asha", "last_name": "Shah",
                "authoritative": authoritative}

    def start(self, *, mode="signin", next_url="", client=None):
        client = client or self.client
        response = client.post(reverse("accounts:google_start"), {"mode": mode, "next": next_url}, **HOST)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlsplit(response.url).netloc, "accounts.google.com", response.url)
        params = parse_qs(urlsplit(response.url).query)
        return params["state"][0], params, response

    def callback(self, state, profile=None, *, client=None, **extra):
        client = client or self.client
        with patch("apps.accounts.google_views.exchange_code", return_value=profile or self.profile()) as exchange:
            response = client.get(reverse("accounts:google_callback"), {"state": state, "code": "synthetic-code", **extra}, **HOST)
        return response, exchange

    def complete(self, *, role="STUDENT", target="JEE", client=None):
        client = client or self.client
        return client.post(reverse("accounts:google_signup"), {"first_name": "Asha", "last_name": "Shah",
                           "role": role, "target_exam": target}, **HOST)

    def test_buttons_are_enabled_without_exposing_secret_and_password_form_remains(self):
        for name in ("accounts:login", "accounts:register"):
            page = self.client.get(reverse(name), **HOST)
            self.assertContains(page, "Continue with Google")
            self.assertContains(page, reverse("accounts:google_start"))
            self.assertContains(page, 'name="password"')
            self.assertNotContains(page, CLIENT_SECRET)
        page = self.client.get(reverse("accounts:register"), {"role": "TEACHER"}, **HOST)
        self.assertContains(page, 'value="TEACHER" checked')

    def test_disabled_or_incomplete_configuration_fails_closed_and_keeps_password_login(self):
        for changes in ({"GOOGLE_SIGN_IN_ENABLED": False}, {"GOOGLE_OAUTH_CLIENT_SECRET": ""},
                        {"GOOGLE_OAUTH_CLIENT_ID": "not-a-google-client"}, {"PUBLIC_BASE_URL": ""},
                        {"PUBLIC_BASE_URL": "http://nonlocal.example"},
                        {"DEBUG": False, "PUBLIC_BASE_URL": "http://127.0.0.1:8000"}):
            with self.subTest(changes=changes), override_settings(**changes):
                self.assertIsNone(configuration())
                page = self.client.get(reverse("accounts:login"), **HOST)
                self.assertContains(page, "not configured")
                self.assertContains(page, 'name="password"')
                response = self.client.post(reverse("accounts:google_start"), **HOST)
                self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
                self.assertFalse(GoogleOAuthState.objects.exists())
        user = User.objects.create_user("password-user", "password@example.test", "Pass123456!", role="STUDENT", exam_track="jee")
        with override_settings(GOOGLE_SIGN_IN_ENABLED=False):
            response = self.client.post(reverse("accounts:login"), {"username": user.username, "password": "Pass123456!"}, **HOST)
            self.assertRedirects(response, reverse("core:dashboard_router"), fetch_redirect_response=False)
            self.assertEqual(int(self.client.session[SESSION_KEY]), user.pk)

    def test_start_uses_csrf_state_nonce_pkce_and_minimum_scopes(self):
        state, params, response = self.start(next_url="/exams/?page=1")
        self.assertEqual(params["client_id"], [CLIENT_ID])
        self.assertEqual(params["redirect_uri"], ["http://127.0.0.1:8000/auth/google/callback/"])
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(set(params["scope"][0].split()), {"openid", "email", "profile"})
        self.assertEqual(params["code_challenge_method"], ["S256"])
        flow = self.client.session[FLOW_SESSION_KEY]
        expected = base64.urlsafe_b64encode(hashlib.sha256(flow["verifier"].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(params["code_challenge"], [expected])
        self.assertEqual(params["nonce"], [flow["nonce"]])
        self.assertEqual(flow["next"], "/exams/?page=1")
        self.assertNotIn(CLIENT_SECRET, response.url)
        self.assertNotIn(flow["verifier"], response.url)
        self.assertTrue(GoogleOAuthState.objects.filter(digest=flow["digest"], consumed_at__isnull=True).exists())
        self.assertNotEqual(state, flow["digest"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(self.client.get(reverse("accounts:google_start"), **HOST).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        self.assertEqual(csrf_client.post(reverse("accounts:google_start"), **HOST).status_code, 403)

    @override_settings(PUBLIC_BASE_URL="https://127.0.0.1:8000")
    def test_https_signup_post_passes_real_csrf_origin_checks(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("accounts:login"), secure=True, **HOST)
        csrf_token = client.cookies["csrftoken"].value
        started = client.post(reverse("accounts:google_start"), {"csrfmiddlewaretoken": csrf_token},
                              secure=True, HTTP_ORIGIN="https://127.0.0.1:8000", **HOST)
        self.assertEqual(started.status_code, 302)
        state = parse_qs(urlsplit(started.url).query)["state"][0]
        with patch("apps.accounts.google_views.exchange_code", return_value=self.profile()):
            callback = client.get(reverse("accounts:google_callback"), {"state": state, "code": "synthetic"},
                                  secure=True, **HOST)
        self.assertRedirects(callback, reverse("accounts:google_signup"), fetch_redirect_response=False)
        page = client.get(callback.url, secure=True, **HOST)
        self.assertEqual(page["Referrer-Policy"], "same-origin")
        csrf_token = client.cookies["csrftoken"].value
        post = client.post(reverse("accounts:google_signup"), {
            "csrfmiddlewaretoken": csrf_token, "first_name": "Asha", "last_name": "Shah",
            "role": "STUDENT", "target_exam": "NEET"}, secure=True,
            HTTP_ORIGIN="https://127.0.0.1:8000", **HOST)
        self.assertRedirects(post, reverse("core:dashboard_router"), fetch_redirect_response=False)
        self.assertEqual(User.objects.get().exam_track, "neet")

    def test_wrong_origin_fails_without_contacting_provider_or_constructing_host_redirect(self):
        with patch("apps.accounts.google_views.create_authorization_url") as create:
            response = self.client.post(reverse("accounts:google_start"), HTTP_HOST="testserver")
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        create.assert_not_called()
        self.assertFalse(GoogleOAuthState.objects.exists())

    def test_state_is_bound_to_browser_one_time_and_expires(self):
        state, params, _ = self.start()
        other = Client()
        with patch("apps.accounts.google_views.exchange_code") as exchange:
            response = other.get(reverse("accounts:google_callback"), {"state": state, "code": "x"}, **HOST)
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        exchange.assert_not_called()
        with patch("apps.accounts.google_views.exchange_code") as exchange:
            bad = self.client.get(reverse("accounts:google_callback"), {"state": state + "x", "code": "x"}, **HOST)
        self.assertRedirects(bad, reverse("accounts:login"), fetch_redirect_response=False)
        exchange.assert_not_called()
        response, exchange = self.callback(state)
        self.assertRedirects(response, reverse("accounts:google_signup"), fetch_redirect_response=False)
        self.assertEqual(exchange.call_count, 1)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        with patch("apps.accounts.google_views.exchange_code") as exchange:
            replay = self.client.get(reverse("accounts:google_callback"), {"state": state, "code": "x"}, **HOST)
        self.assertRedirects(replay, reverse("accounts:login"), fetch_redirect_response=False)
        exchange.assert_not_called()
        state2, _, _ = self.start()
        GoogleOAuthState.objects.filter(digest=self.client.session[FLOW_SESSION_KEY]["digest"]).update(expires_at=timezone.now()-timedelta(seconds=1))
        with patch("apps.accounts.google_views.exchange_code") as exchange:
            expired = self.client.get(reverse("accounts:google_callback"), {"state": state2, "code": "x"}, **HOST)
        self.assertRedirects(expired, reverse("accounts:login"), fetch_redirect_response=False)
        exchange.assert_not_called()
        self.assertFalse(User.objects.exists())

    def test_duplicate_callback_parameters_are_rejected_without_authentication(self):
        state, _, _ = self.start()
        with patch("apps.accounts.google_views.exchange_code") as exchange:
            duplicate_states = self.client.get(reverse("accounts:google_callback"),
                                               {"state": [state, state], "code": "synthetic"}, **HOST)
        self.assertRedirects(duplicate_states, reverse("accounts:login"), fetch_redirect_response=False)
        exchange.assert_not_called()
        with patch("apps.accounts.google_views.exchange_code", side_effect=GoogleSignInError("Invalid code")) as exchange:
            duplicate_codes = self.client.get(reverse("accounts:google_callback"),
                                              {"state": state, "code": ["one", "two"]}, **HOST)
        self.assertRedirects(duplicate_codes, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertEqual(exchange.call_args.args[0], "")
        self.assertFalse(User.objects.exists())
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_multiple_starts_invalidate_previous_flow(self):
        first, _, _ = self.start()
        first_digest = self.client.session[FLOW_SESSION_KEY]["digest"]
        second, _, _ = self.start()
        self.assertNotEqual(first, second)
        self.assertIsNotNone(GoogleOAuthState.objects.get(pk=first_digest).consumed_at)
        with patch("apps.accounts.google_views.exchange_code") as exchange:
            self.client.get(reverse("accounts:google_callback"), {"state": first, "code": "x"}, **HOST)
        exchange.assert_not_called()
        response, exchange = self.callback(second)
        self.assertRedirects(response, reverse("accounts:google_signup"), fetch_redirect_response=False)

    def test_cancel_missing_code_and_provider_failure_do_not_sign_in(self):
        state, _, _ = self.start()
        with patch("apps.accounts.google_views.exchange_code") as exchange:
            cancelled = self.client.get(reverse("accounts:google_callback"), {"state": state, "error": "access_denied"}, **HOST)
        self.assertRedirects(cancelled, reverse("accounts:login"), fetch_redirect_response=False)
        exchange.assert_not_called()
        state, _, _ = self.start()
        with patch("apps.accounts.google_views.exchange_code", side_effect=GoogleSignInError("Please retry.")):
            failed = self.client.get(reverse("accounts:google_callback"), {"state": state, "code": "synthetic"}, **HOST)
        self.assertRedirects(failed, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertFalse(User.objects.exists())
        self.assertFalse(GoogleIdentity.objects.exists())

    def test_student_signup_requires_track_and_preserves_safe_next(self):
        state, _, _ = self.start(next_url="/exams/")
        response, _ = self.callback(state)
        self.assertRedirects(response, reverse("accounts:google_signup"), fetch_redirect_response=False)
        page = self.client.get(response.url, **HOST)
        self.assertContains(page, "Finish your account")
        self.assertContains(page, "student@gmail.com")
        self.assertEqual(page["Referrer-Policy"], "same-origin")
        self.assertIn("no-store", page["Cache-Control"])
        invalid = self.complete(target="")
        self.assertContains(invalid, "Students must choose JEE or NEET")
        self.assertFalse(User.objects.exists())
        response = self.complete(target="NEET")
        self.assertRedirects(response, "/exams/", fetch_redirect_response=False)
        user = User.objects.get()
        self.assertEqual((user.role, user.exam_track, user.target_exam), ("STUDENT", "neet", "NEET"))
        self.assertTrue(user.is_verified)
        self.assertTrue(user.verification_required)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(GoogleIdentity.objects.get().user_id, user.pk)
        self.assertEqual(int(self.client.session[SESSION_KEY]), user.pk)
        self.assertNotIn(SIGNUP_SESSION_KEY, self.client.session)
        self.assertFalse(CreditUse.objects.filter(user=user).exists())
        self.assertTrue(all(row["free"] for row in balances(user)))
        self.assertEqual(len(mail.outbox), 0)
        self.complete(target="JEE")
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(GoogleIdentity.objects.count(), 1)

    def test_google_only_user_can_add_a_password_and_keep_both_login_methods(self):
        state, _, _ = self.start()
        self.callback(state)
        self.complete()
        user = User.objects.get()
        other_session = Client()
        other_session.force_login(user)
        profile = self.client.get(reverse("accounts:profile"), **HOST)
        self.assertContains(profile, "Add a password")
        page = self.client.get(reverse("accounts:google_add_password"), **HOST)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page["Referrer-Policy"], "same-origin")
        self.assertIn("no-store", page["Cache-Control"])
        for values in ({"new_password1": "1", "new_password2": "1"},
                       {"new_password1": "SecureExample987!", "new_password2": "different"}):
            invalid = self.client.post(reverse("accounts:google_add_password"), values, **HOST)
            self.assertEqual(invalid.status_code, 200)
            self.assertTrue(invalid.context["form"].errors)
            user.refresh_from_db()
            self.assertFalse(user.has_usable_password())
        response = self.client.post(reverse("accounts:google_add_password"),
            {"new_password1": "SecureExample987!", "new_password2": "SecureExample987!"}, **HOST)
        self.assertRedirects(response, reverse("accounts:profile"), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertTrue(user.check_password("SecureExample987!"))
        self.assertEqual(int(self.client.session[SESSION_KEY]), user.pk)
        # The old hash is invalid in other sessions after a password is set.
        self.assertRedirects(other_session.get(reverse("accounts:profile"), **HOST),
                             reverse("accounts:login") + "?next=" + reverse("accounts:profile"), fetch_redirect_response=False)
        self.assertEqual(GoogleIdentity.objects.get().user_id, user.pk)
        # This endpoint is not a password-change shortcut for existing users.
        attempted_replacement = self.client.post(reverse("accounts:google_add_password"),
            {"new_password1": "OtherExample987!", "new_password2": "OtherExample987!"}, **HOST)
        self.assertRedirects(attempted_replacement, reverse("accounts:profile"), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertTrue(user.check_password("SecureExample987!"))
        self.client.logout()
        login_response = self.client.post(reverse("accounts:login"),
            {"username": user.email, "password": "SecureExample987!"}, **HOST)
        self.assertRedirects(login_response, reverse("core:dashboard_router"), fetch_redirect_response=False)
        self.client.logout()
        state, _, _ = self.start()
        self.callback(state)
        self.assertEqual(int(self.client.session[SESSION_KEY]), user.pk)
        self.assertEqual(User.objects.count(), 1)

    def test_password_setup_requires_login_verified_email_google_link_and_csrf(self):
        add_url = reverse("accounts:google_add_password")
        anonymous = self.client.get(add_url, **HOST)
        self.assertRedirects(anonymous, reverse("accounts:login") + "?next=" + add_url, fetch_redirect_response=False)
        password_user = User.objects.create_user("password-user", "p@example.test", "PasswordBefore987!", role="STUDENT", exam_track="jee")
        self.client.force_login(password_user)
        self.client.post(add_url, {"new_password1": "PasswordAfter987!", "new_password2": "PasswordAfter987!"}, **HOST)
        password_user.refresh_from_db()
        self.assertTrue(password_user.check_password("PasswordBefore987!"))
        self.client.logout()
        unverified = User.objects.create_user("thirdparty", "third@outside.test", None, role="STUDENT", exam_track="jee",
                                              verification_required=True, is_verified=False)
        GoogleIdentity.objects.create(user=unverified, subject="unverified-subject")
        self.client.force_login(unverified)
        blocked = self.client.post(add_url, {"new_password1": "PasswordAfter987!", "new_password2": "PasswordAfter987!"}, **HOST)
        self.assertRedirects(blocked, reverse("accounts:verification_pending"), fetch_redirect_response=False)
        unverified.refresh_from_db()
        self.assertFalse(unverified.has_usable_password())
        self.client.logout()
        no_link = User.objects.create_user("unlinked", "unlinked@example.test", None, role="STUDENT", exam_track="jee", is_verified=True)
        self.client.force_login(no_link)
        self.client.post(add_url, {"new_password1": "PasswordAfter987!", "new_password2": "PasswordAfter987!"}, **HOST)
        no_link.refresh_from_db()
        self.assertFalse(no_link.has_usable_password())
        csrf_client = Client(enforce_csrf_checks=True)
        User.objects.filter(pk=unverified.pk).update(is_verified=True)
        unverified.refresh_from_db()
        csrf_client.force_login(unverified)
        self.assertEqual(csrf_client.post(add_url, {"new_password1": "PasswordAfter987!", "new_password2": "PasswordAfter987!"}, **HOST).status_code, 403)
        unverified.refresh_from_db()
        self.assertFalse(unverified.has_usable_password())

    def test_disabling_google_keeps_password_setup_available_to_a_signed_in_google_user(self):
        state, _, _ = self.start()
        self.callback(state)
        self.complete()
        user = User.objects.get()
        with override_settings(GOOGLE_SIGN_IN_ENABLED=False):
            page = self.client.get(reverse("accounts:profile"), **HOST)
            self.assertContains(page, "Google sign-in is currently not configured")
            self.assertContains(page, "Add a password")
            response = self.client.post(reverse("accounts:google_add_password"),
                {"new_password1": "SecureExample987!", "new_password2": "SecureExample987!"}, **HOST)
            self.assertRedirects(response, reverse("accounts:profile"), fetch_redirect_response=False)
            self.client.logout()
            self.assertTrue(self.client.login(username=user.username, password="SecureExample987!"))
        user.refresh_from_db()
        self.assertTrue(user.check_password("SecureExample987!"))
        self.assertEqual(GoogleIdentity.objects.get().user_id, user.pk)

    def test_password_reset_mail_is_available_after_google_only_user_adds_password(self):
        state, _, _ = self.start()
        self.callback(state)
        self.complete()
        user = User.objects.get()
        self.client.logout()
        self.client.post(reverse("accounts:password_reset"), {"email": user.email}, **HOST)
        self.assertEqual(len(mail.outbox), 0)  # Django does not reset unusable passwords.
        state, _, _ = self.start()
        self.callback(state)
        self.client.post(reverse("accounts:google_add_password"),
            {"new_password1": "SecureExample987!", "new_password2": "SecureExample987!"}, **HOST)
        self.client.logout()
        self.client.post(reverse("accounts:password_reset"), {"email": user.email}, **HOST)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])

    def test_teacher_signup_never_grants_admin_staff_or_categories(self):
        state, _, _ = self.start()
        self.callback(state, self.profile(email="teacher@gmail.com"))
        response = self.complete(role="TEACHER", target="")
        self.assertRedirects(response, reverse("core:dashboard_router"), fetch_redirect_response=False)
        user = User.objects.get()
        self.assertEqual(user.role, "TEACHER")
        self.assertEqual(user.exam_track, "")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.assigned_categories.count(), 0)
        self.assertTrue(user.is_verified)

    def test_forged_role_or_track_never_creates_account(self):
        state, _, _ = self.start()
        self.callback(state)
        for role, target in (("ADMIN", "JEE"), ("SUPER_ADMIN", "NEET"), ("STUDENT", "OTHER")):
            response = self.complete(role=role, target=target)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors)
        self.assertFalse(User.objects.exists())
        self.assertFalse(GoogleIdentity.objects.exists())

    def test_expired_signup_cannot_create_account(self):
        state, _, _ = self.start()
        self.callback(state)
        session = self.client.session
        pending = session[SIGNUP_SESSION_KEY]
        pending["created_at"] = int(time.time()) - 901
        session[SIGNUP_SESSION_KEY] = pending
        session.save()
        response = self.complete()
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertFalse(User.objects.exists())

    def test_non_google_hosted_email_requires_existing_email_verification(self):
        state, _, _ = self.start()
        self.callback(state, self.profile(email="owner@outside.test", authoritative=False))
        response = self.complete()
        self.assertRedirects(response, reverse("accounts:verification_pending"), fetch_redirect_response=False)
        user = User.objects.get()
        self.assertFalse(user.is_verified)
        self.assertTrue(user.needs_email_verification)
        self.assertEqual(mail.outbox[0].to, ["owner@outside.test"])
        self.assertEqual(self.client.get(reverse("core:dashboard_router"), **HOST).url, reverse("accounts:verification_pending"))
        path = urlsplit(mail.outbox[0].body.split("\n\n")[1]).path
        self.client.post(path, **HOST)
        user.refresh_from_db()
        self.assertTrue(user.is_verified)
        self.assertEqual(GoogleIdentity.objects.get().user_id, user.pk)

    def test_existing_email_never_automatically_creates_or_merges_account(self):
        user = User.objects.create_user("existing", "Student@gmail.com", "old-password", role="STUDENT", exam_track="jee")
        state, _, _ = self.start()
        response, _ = self.callback(state)
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(User.objects.get().pk, user.pk)
        self.assertFalse(GoogleIdentity.objects.exists())
        self.assertNotIn(SIGNUP_SESSION_KEY, self.client.session)

    def test_signup_rechecks_email_and_subject_before_committing(self):
        state, _, _ = self.start()
        self.callback(state)
        # A regular registration wins after the Google profile was received.
        existing = User.objects.create_user("winner", "student@gmail.com", "winner-password", role="STUDENT", exam_track="jee")
        response = self.complete()
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(GoogleIdentity.objects.exists())
        existing.refresh_from_db()
        self.assertTrue(existing.check_password("winner-password"))
        self.assertNotIn(SESSION_KEY, self.client.session)
        existing.delete()
        state, _, _ = self.start()
        self.callback(state)
        # Another session connects this subject before onboarding completes.
        other = User.objects.create_user("other", "other@gmail.com", "other-password", role="STUDENT", exam_track="neet")
        GoogleIdentity.objects.create(user=other, subject="google-subject-123")
        response = self.complete()
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(GoogleIdentity.objects.get().user_id, other.pk)

    def test_google_verification_invalidates_old_email_link(self):
        from .verification import send_verification, token_user
        user = User.objects.create_user("existing", "student@gmail.com", "old-password", role="STUDENT",
                                        exam_track="jee", verification_required=True, is_verified=False)
        self.assertEqual(send_verification(user), "sent")
        old_link = urlsplit(mail.outbox[0].body.split("\n\n")[1]).path
        old_token = old_link.rstrip("/").split("/")[-1]
        self.assertIsNotNone(token_user(old_token))
        self.client.force_login(user)
        state, _, _ = self.start(mode="link")
        self.callback(state)
        self.assertIsNone(token_user(old_token))
        user.refresh_from_db()
        self.assertTrue(user.is_verified)
        self.assertTrue(user.check_password("old-password"))

    def test_existing_pending_account_links_and_preserves_attempt_credit_password_and_role(self):
        user = User.objects.create_user("existing", "student@gmail.com", "old-password", role="STUDENT",
            exam_track="jee", verification_required=True, is_verified=False)
        teacher = User.objects.create_user("teacher", "teacher@example.test", role="TEACHER")
        category = Category.objects.create(name="Google safety", slug="google-safety")
        exam = Exam.objects.create(title="Saved exam", slug="google-saved-exam", creator=teacher,
                                   category=category, target_type="jee", status="published")
        deadline = timezone.now() + timedelta(minutes=20)
        attempt = ExamAttempt.objects.create(exam=exam, student=user, status="in_progress", expires_at=deadline)
        use = CreditUse.objects.create(user=user, kind="classroom", is_free=True, resource_key="google-preserved-use")
        old_password, old_token = user.password, user.verification_token
        self.client.force_login(user)
        page = self.client.get(reverse("accounts:verification_pending"), **HOST)
        self.assertContains(page, "Have a Google account for this address?")
        self.assertContains(page, "Continue with Google")
        state, _, _ = self.start(mode="link")
        response, _ = self.callback(state)
        self.assertRedirects(response, reverse("core:dashboard_router"), fetch_redirect_response=False)
        user.refresh_from_db(); attempt.refresh_from_db(); use.refresh_from_db()
        self.assertEqual(user.pk, int(self.client.session[SESSION_KEY]))
        self.assertTrue(user.is_verified)
        self.assertNotEqual(user.verification_token, old_token)
        self.assertEqual((user.email, user.role, user.exam_track, user.password),
                         ("student@gmail.com", "STUDENT", "jee", old_password))
        self.assertEqual(attempt.expires_at, deadline)
        self.assertEqual(attempt.status, "in_progress")
        self.assertEqual(use.user_id, user.pk)
        self.assertEqual(CreditUse.objects.filter(user=user, kind="classroom").count(), 1)
        self.assertFalse(next(row for row in balances(user) if row["kind"] == "classroom")["free"])
        self.assertEqual(GoogleIdentity.objects.get(subject="google-subject-123").user_id, user.pk)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]), **HOST).status_code, 200)
        self.client.logout()
        state, _, _ = self.start()
        self.callback(state)
        self.assertEqual(self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]), **HOST).status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, deadline)
        self.assertEqual(CreditUse.objects.filter(user=user, kind="classroom").count(), 1)
        self.client.logout()
        self.assertTrue(self.client.login(username="existing", password="old-password"))

    def test_link_rejects_wrong_address_subject_collision_and_replacement(self):
        user = User.objects.create_user("existing", "student@gmail.com", "old-password", role="STUDENT",
                                        exam_track="jee", verification_required=True, is_verified=False)
        other = User.objects.create_user("other", "other@gmail.com", "old-password", role="STUDENT", exam_track="jee")
        GoogleIdentity.objects.create(user=other, subject="other-subject")
        self.client.force_login(user)
        for profile in (self.profile(email="someone-else@gmail.com"), self.profile(subject="other-subject")):
            state, _, _ = self.start(mode="link")
            response, _ = self.callback(state, profile)
            self.assertRedirects(response, reverse("accounts:verification_pending"), fetch_redirect_response=False)
            user.refresh_from_db()
            self.assertFalse(user.is_verified)
            self.assertFalse(GoogleIdentity.objects.filter(user=user).exists())
        GoogleIdentity.objects.create(user=user, subject="already-linked")
        state, _, _ = self.start(mode="link")
        self.callback(state, self.profile(subject="new-subject"))
        self.assertEqual(GoogleIdentity.objects.get(user=user).subject, "already-linked")

    def test_link_rechecks_fresh_email_and_session_user_after_google_round_trip(self):
        user = User.objects.create_user("old", "student@gmail.com", "password", role="STUDENT", exam_track="jee",
                                        verification_required=True, is_verified=False)
        other = User.objects.create_user("other", "other@gmail.com", "password", role="STUDENT", exam_track="jee")
        self.client.force_login(user)
        state, _, _ = self.start(mode="link")
        User.objects.filter(pk=user.pk).update(email="changed@gmail.com")
        self.callback(state)
        user.refresh_from_db()
        self.assertFalse(user.is_verified)
        self.assertFalse(GoogleIdentity.objects.filter(user=user).exists())
        User.objects.filter(pk=user.pk).update(email="student@gmail.com")
        state, _, _ = self.start(mode="link")
        self.client.force_login(other)
        response, exchange = self.callback(state)
        exchange.assert_not_called()
        self.assertFalse(GoogleIdentity.objects.filter(user=user).exists())
        self.assertFalse(GoogleIdentity.objects.filter(user=other).exists())

    def test_connected_subject_signs_into_same_account_and_existing_password_still_works(self):
        user = User.objects.create_user("linked", "student@gmail.com", "old-password", role="STUDENT", exam_track="jee",
                                        verification_required=True, is_verified=True)
        GoogleIdentity.objects.create(user=user, subject="google-subject-123")
        state, _, _ = self.start(next_url="/exams/")
        response, _ = self.callback(state)
        self.assertRedirects(response, "/exams/", fetch_redirect_response=False)
        self.assertEqual(int(self.client.session[SESSION_KEY]), user.pk)
        self.assertEqual(User.objects.count(), 1)
        self.client.logout()
        self.assertTrue(self.client.login(username="linked", password="old-password"))

    def test_link_rechecks_role_and_deactivation_after_google_start(self):
        user = User.objects.create_user("pending", "student@gmail.com", "password", role="STUDENT", exam_track="jee",
                                        verification_required=True, is_verified=False)
        self.client.force_login(user)
        state, _, _ = self.start(mode="link")
        User.objects.filter(pk=user.pk).update(role="ADMIN")
        response, _ = self.callback(state)
        self.assertRedirects(response, reverse("accounts:verification_pending"), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertEqual(user.role, "ADMIN")
        self.assertFalse(user.is_verified)
        self.assertFalse(GoogleIdentity.objects.exists())
        User.objects.filter(pk=user.pk).update(role="STUDENT")
        state, _, _ = self.start(mode="link")
        User.objects.filter(pk=user.pk).update(is_active=False)
        response, exchange = self.callback(state)
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        exchange.assert_not_called()
        self.assertFalse(GoogleIdentity.objects.exists())

    def test_linked_subject_never_verifies_a_different_changed_profile_email(self):
        user = User.objects.create_user("linked", "changed@gmail.com", "old-password", role="STUDENT", exam_track="jee",
                                        verification_required=True, is_verified=False)
        GoogleIdentity.objects.create(user=user, subject="google-subject-123")
        state, _, _ = self.start()
        response, _ = self.callback(state)
        self.assertRedirects(response, reverse("accounts:verification_pending"), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertFalse(user.is_verified)
        self.assertEqual(user.email, "changed@gmail.com")
        self.assertEqual(int(self.client.session[SESSION_KEY]), user.pk)

    def test_unverified_non_google_email_is_not_verified_when_connecting(self):
        user = User.objects.create_user("thirdparty", "person@outside.test", "password", role="STUDENT", exam_track="jee",
                                        verification_required=True, is_verified=False)
        self.client.force_login(user)
        state, _, _ = self.start(mode="link")
        response, _ = self.callback(state, self.profile(email="person@outside.test", authoritative=False))
        self.assertRedirects(response, reverse("accounts:verification_pending"), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertFalse(user.is_verified)
        self.assertTrue(GoogleIdentity.objects.filter(user=user).exists())

    def test_disabled_accounts_and_admin_roles_cannot_use_google_signin(self):
        for is_active, role in ((False, "STUDENT"), (True, "ADMIN"), (True, "SUPER_ADMIN")):
            with self.subTest(is_active=is_active, role=role):
                user = User.objects.create_user("user" + role + str(is_active), role.lower()+str(is_active)+"@example.test",
                                                "password", role=role, is_active=is_active)
                subject = "subject-" + role + str(is_active)
                GoogleIdentity.objects.create(user=user, subject=subject)
                state, _, _ = self.start()
                response, _ = self.callback(state, self.profile(subject=subject))
                self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
                self.assertNotIn(SESSION_KEY, self.client.session)
                user.delete()

    def test_admin_managed_teacher_can_link_without_role_or_staff_changes(self):
        teacher = User.objects.create_user("staff-teacher", "teacher@gmail.com", "password", role="TEACHER", is_staff=True)
        self.client.force_login(teacher)
        state, _, _ = self.start(mode="link")
        response, _ = self.callback(state, self.profile(email="teacher@gmail.com"))
        self.assertRedirects(response, reverse("core:dashboard_router"), fetch_redirect_response=False)
        teacher.refresh_from_db()
        self.assertEqual(teacher.role, "TEACHER")
        self.assertTrue(teacher.is_staff)
        self.assertFalse(teacher.is_superuser)

    def test_next_redirect_is_local_only(self):
        user = User.objects.create_user("linked", "student@gmail.com", "password", role="STUDENT", exam_track="jee")
        GoogleIdentity.objects.create(user=user, subject="google-subject-123")
        for destination in ("//evil.example/path", "https://evil.example/path", "/\\evil.example", "not-a-path"):
            state, _, _ = self.start(next_url=destination)
            response, _ = self.callback(state)
            self.assertRedirects(response, reverse("core:dashboard_router"), fetch_redirect_response=False)
            self.client.logout()


@override_settings(**GOOGLE_SETTINGS)
class GoogleCryptographicVerificationTests(SimpleTestCase):
    """Exercise google-auth's real signature/claim checks with synthetic RSA keys."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        bad_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.signer = crypt.RSASigner.from_string(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), key_id="test-key")
        cls.bad_signer = crypt.RSASigner.from_string(bad_key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), key_id="test-key")
        cls.public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def claims(self, **changes):
        claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "1234567",
                  "iat": int(time.time())-1, "exp": int(time.time())+300, "nonce": "test-nonce",
                  "email": "user@gmail.com", "email_verified": True, "given_name": "Asha", "family_name": "Shah"}
        claims.update(changes)
        return claims

    def verify(self, claims, *, signer=None, nonce="test-nonce"):
        token = jwt.encode(signer or self.signer, claims).decode()
        response = Mock(status=200, data=json.dumps({"test-key": self.public_pem}).encode())
        transport = Mock(return_value=response)
        with patch("apps.accounts.google_signin.GoogleRequest", return_value=transport), \
             patch("requests.sessions.Session.request", side_effect=AssertionError("No provider network in tests")):
            result = _verified_profile(token, {"client_id": CLIENT_ID}, nonce)
        transport.assert_called_once()
        self.assertEqual(transport.call_args.kwargs["timeout"], (3.05, 8))
        self.assertFalse(transport.call_args.kwargs["allow_redirects"])
        return result

    def test_valid_signature_issuer_audience_nonce_and_verified_gmail(self):
        profile = self.verify(self.claims())
        self.assertEqual(profile, {"subject": "1234567", "email": "user@gmail.com", "first_name": "Asha",
                                   "last_name": "Shah", "authoritative": True})

    def test_reject_bad_signature_expired_wrong_audience_issuer_nonce_and_unverified_email(self):
        cases = [
            (self.claims(), self.bad_signer, "test-nonce"),
            (self.claims(exp=int(time.time())-120), None, "test-nonce"),
            (self.claims(iat=int(time.time())+120), None, "test-nonce"),
            (self.claims(aud="wrong.apps.googleusercontent.com"), None, "test-nonce"),
            (self.claims(iss="https://attacker.example"), None, "test-nonce"),
            (self.claims(azp="wrong.apps.googleusercontent.com"), None, "test-nonce"),
            (self.claims(nonce="wrong"), None, "test-nonce"),
            (self.claims(email_verified=False), None, "test-nonce"),
            (self.claims(email="not-an-email"), None, "test-nonce"),
            (self.claims(sub=""), None, "test-nonce"),
            (self.claims(sub="x"*256), None, "test-nonce"),
        ]
        for claims, signer, nonce in cases:
            with self.subTest(claims=claims), self.assertRaises(GoogleSignInError):
                self.verify(claims, signer=signer, nonce=nonce)

    def test_non_google_address_does_not_become_automatically_verified(self):
        self.assertFalse(self.verify(self.claims(email="person@outside.test"))["authoritative"])
        self.assertFalse(self.verify(self.claims(email="person@outside.test", hd="other.test"))["authoritative"])
        self.assertTrue(self.verify(self.claims(email="person@school.test", hd="school.test"))["authoritative"])

    def test_exchange_validates_a_real_signed_token_and_pkce(self):
        token = jwt.encode(self.signer, self.claims()).decode()
        token_response = Mock(status_code=200)
        token_response.json.return_value = {"id_token": token, "access_token": "discard-this-token"}
        cert_response = Mock(status=200, data=json.dumps({"test-key": self.public_pem}).encode())
        transport = Mock(return_value=cert_response)
        flow = {"nonce": "test-nonce", "verifier": "v" * 86}
        config = configuration()
        with patch("apps.accounts.google_signin.requests.post", return_value=token_response) as post, \
             patch("apps.accounts.google_signin.GoogleRequest", return_value=transport):
            profile = exchange_code("synthetic-code", flow, config)
        self.assertEqual(profile["subject"], "1234567")
        self.assertTrue(profile["authoritative"])
        self.assertNotIn("access_token", profile)
        self.assertEqual(post.call_args.kwargs["data"]["redirect_uri"], config["redirect_uri"])
        self.assertEqual(post.call_args.kwargs["data"]["code_verifier"], flow["verifier"])
        self.assertEqual(post.call_args.kwargs["timeout"], (3.05, 8))
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        transport.assert_called_once()

    def test_exchange_never_follows_redirects_or_exposes_provider_failure(self):
        config = configuration()
        flow = {"nonce": "test-nonce", "verifier": "v" * 86}
        with patch("apps.accounts.google_signin.requests.post", side_effect=requests.ConnectionError("sensitive-provider-detail")) as send, \
             self.assertLogs("apps.accounts.google_signin", level="WARNING") as logs, self.assertRaises(GoogleSignInError):
            exchange_code("synthetic-code", flow, config)
        self.assertNotIn("sensitive-provider-detail", " ".join(logs.output))
        self.assertFalse(send.call_args.kwargs["allow_redirects"])
        self.assertEqual(send.call_args.kwargs["timeout"], (3.05, 8))
        self.assertEqual(send.call_args.kwargs["data"]["code_verifier"], flow["verifier"])

    def test_unexpected_provider_response_and_empty_code_fail_closed(self):
        config, flow = configuration(), {"nonce": "test-nonce", "verifier": "v" * 86}
        with patch("apps.accounts.google_signin.requests.post") as send:
            send.return_value.status_code = 302
            with self.assertRaises(GoogleSignInError):
                exchange_code("synthetic-code", flow, config)
            send.reset_mock()
            with self.assertRaises(GoogleSignInError):
                exchange_code("", flow, config)
            send.assert_not_called()
