"""Founder authorization, TOTP, CSRF, audit and privacy; entirely synthetic data."""
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pyotp
from cryptography.fernet import Fernet
from django.contrib.auth.hashers import make_password
from django.core.exceptions import PermissionDenied
from django.core.management import CommandError, call_command
from django.db import OperationalError, connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.founder_security import (FounderConfigurationError, SESSION_KEY, encrypt_secret,
                                        verify_step_up)
from apps.core.models import AuditLog, FounderAccess
from apps.exams.models import Category, Exam, ExamAttempt
from apps.payments.models import CreditGrant, CreditUse, PackCheckout
from apps.questions.models import Question, QuestionPaperUpload

TEST_KEY = Fernet.generate_key().decode()
TEST_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
PASSWORD = "SyntheticFounderPassword987!"


@override_settings(DEBUG=True, FOUNDER_TOTP_ENCRYPTION_KEY=TEST_KEY,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class FounderSecurityTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser("owner", "owner@example.test", PASSWORD, first_name="Owner")
        self.other_super = User.objects.create_superuser("other-super", "other@example.test", PASSWORD)
        self.role_super = User.objects.create_user("role-super", "role-super@example.test", PASSWORD, role="SUPER_ADMIN")
        self.admin = User.objects.create_user("admin-test", "admin@example.test", PASSWORD, role="ADMIN")
        self.teacher = User.objects.create_user("teacher-test", "teacher@example.test", PASSWORD, role="TEACHER")
        self.student = User.objects.create_user("student-test", "student@example.test", PASSWORD, role="STUDENT", exam_track="jee")

    def enroll(self, user=None):
        return FounderAccess.objects.create(user=user or self.owner, is_enabled=True,
                                            encrypted_totp_secret=encrypt_secret(TEST_SECRET))

    def unlock(self, client=None, *, password=PASSWORD, when=None):
        client = client or self.client
        client.force_login(self.owner)
        when = when or timezone.now()
        code = pyotp.TOTP(TEST_SECRET).at(int(when.timestamp()))
        with patch("apps.core.founder_security.timezone.now", return_value=when):
            return client.post(reverse("founder:verify"), {"password": password, "code": code})

    def test_no_automatic_founders_and_student_teacher_admin_are_denied_even_with_bad_rows(self):
        self.assertEqual(FounderAccess.objects.count(), 0)
        anonymous = self.client.get(reverse("founder:overview"))
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn(reverse("accounts:login"), anonymous["Location"])
        for person in (self.owner, self.other_super, self.role_super, self.admin, self.teacher, self.student):
            self.client.force_login(person)
            self.assertEqual(self.client.get(reverse("founder:overview")).status_code, 403, person.username)
        # A manually inserted row cannot promote a student, teacher or admin.
        for person in (self.student, self.teacher, self.admin):
            self.enroll(person)
            self.client.force_login(person)
            for name in ("overview", "verify", "accounts", "payments", "series", "activity"):
                self.assertEqual(self.client.get(reverse(f"founder:{name}")).status_code, 403)
        self.client.force_login(self.other_super)
        legacy = self.client.get(reverse("core:super_admin_dashboard"))
        self.assertContains(legacy, "Administrator Operations Portal")
        self.assertNotContains(legacy, "FOUNDER SPACE")
        self.assertEqual(self.client.get(reverse("accounts:admin_list")).status_code, 200)

    def test_inconsistent_student_teacher_or_admin_superuser_flags_do_not_grant_founder_access(self):
        for person in (self.student, self.teacher, self.admin):
            # A legacy or manually updated row can have inconsistent flags even
            # though User.save normally synchronizes the Django superuser role.
            User.objects.filter(pk=person.pk).update(is_superuser=True)
            person.refresh_from_db()
            self.assertTrue(person.is_super_admin)
            with self.assertRaises(CommandError):
                call_command("founder_access", "grant", username=person.username, confirm=True, stdout=StringIO())
            self.enroll(person)  # Even a manually inserted access row fails closed.
            self.client.force_login(person)
            for name in ("overview", "verify", "accounts", "payments", "series", "activity"):
                self.assertEqual(self.client.get(reverse(f"founder:{name}")).status_code, 403)
            operations = self.client.get(reverse("core:super_admin_dashboard"))
            self.assertContains(operations, "Administrator Operations Portal")
            self.assertNotContains(operations, "Founder workspace")
            person.refresh_from_db()
            self.assertNotEqual(person.role, User.Roles.SUPER_ADMIN)

        # A deliberately enrolled SUPER_ADMIN role does not need a separate
        # Django is_superuser flag, which remains unchanged.
        call_command("founder_access", "grant", username=self.role_super.username, confirm=True, stdout=StringIO())
        self.role_super.refresh_from_db()
        self.assertEqual((self.role_super.role, self.role_super.is_superuser), ("SUPER_ADMIN", False))
        self.assertTrue(FounderAccess.objects.get(user=self.role_super).is_enabled)

    def test_grant_revoke_is_deliberate_role_preserving_and_secret_is_encrypted(self):
        before = (self.owner.role, self.owner.is_staff, self.owner.is_superuser, self.owner.password)
        with self.assertRaises(CommandError):
            call_command("founder_access", "grant", username=self.owner.username, stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("founder_access", "grant", username=self.student.username, confirm=True, stdout=StringIO())
        self.assertFalse(FounderAccess.objects.exists())
        out = StringIO()
        call_command("founder_access", "grant", username=self.owner.username, confirm=True, stdout=out)
        access = FounderAccess.objects.get(user=self.owner)
        lines = out.getvalue().splitlines()
        secret = lines[1]
        self.assertEqual(len(secret), 32)
        self.assertNotIn(secret, access.encrypted_totp_secret)
        self.assertNotIn(secret, " ".join(AuditLog.objects.values_list("description", flat=True)))
        self.owner.refresh_from_db()
        self.assertEqual((self.owner.role, self.owner.is_staff, self.owner.is_superuser, self.owner.password), before)
        with self.assertRaises(CommandError):
            call_command("founder_access", "grant", username=self.owner.username, confirm=True, stdout=StringIO())
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse("founder:verify"), {
            "password": PASSWORD, "code": pyotp.TOTP(secret).now()}).status_code, 302)
        self.assertEqual(self.client.get(reverse("founder:overview")).status_code, 200)
        # Revocation works even if the encryption key is unavailable; any open
        # founder session is denied on the next request.
        with override_settings(FOUNDER_TOTP_ENCRYPTION_KEY=""):
            call_command("founder_access", "revoke", username=self.owner.username, confirm=True, stdout=StringIO())
        access.refresh_from_db()
        self.assertFalse(access.is_enabled)
        self.assertEqual(access.encrypted_totp_secret, "")
        self.assertEqual(self.client.get(reverse("founder:overview")).status_code, 403)
        self.owner.refresh_from_db()
        self.assertEqual((self.owner.role, self.owner.is_staff, self.owner.is_superuser, self.owner.password), before)
        self.assertTrue(AuditLog.objects.filter(action="FOUNDER_ACCESS_GRANTED").exists())
        self.assertTrue(AuditLog.objects.filter(action="FOUNDER_ACCESS_REVOKED").exists())

    @override_settings(DEBUG=False)
    def test_public_demo_passwords_cannot_be_seeded_in_non_debug_deployments(self):
        count = User.objects.count()
        with self.assertRaises(CommandError):
            call_command("seed_admin", stdout=StringIO())
        self.assertEqual(User.objects.count(), count)
        self.assertFalse(User.objects.filter(username="admin").exists())

    def test_missing_invalid_or_replaced_key_fails_closed(self):
        access = self.enroll()
        self.client.force_login(self.owner)
        for key in ("", "not-a-key", Fernet.generate_key().decode()):
            with override_settings(FOUNDER_TOTP_ENCRYPTION_KEY=key):
                for name in ("verify", "overview", "accounts", "payments"):
                    response = self.client.get(reverse(f"founder:{name}"))
                    self.assertEqual(response.status_code, 503)
                    self.assertNotContains(response, access.encrypted_totp_secret, status_code=503)
                    self.assertNotContains(response, TEST_SECRET, status_code=503)
        with override_settings(FOUNDER_TOTP_ENCRYPTION_KEY=""):
            with self.assertRaises(CommandError):
                call_command("founder_access", "grant", username=self.other_super.username, confirm=True, stdout=StringIO())
        self.assertFalse(FounderAccess.objects.filter(user=self.other_super).exists())
        access.encrypted_totp_secret = encrypt_secret("A" * 33)  # invalid Base32 length
        access.save(update_fields=["encrypted_totp_secret"])
        malformed = self.client.get(reverse("founder:verify"))
        self.assertEqual(malformed.status_code, 503)
        self.assertNotContains(malformed, "A" * 33, status_code=503)

    def test_encryption_key_cannot_reuse_django_signing_keys(self):
        with override_settings(SECRET_KEY=TEST_KEY):
            with self.assertRaises(FounderConfigurationError):
                encrypt_secret(TEST_SECRET)
        with override_settings(SECRET_KEY_FALLBACKS=[TEST_KEY]):
            with self.assertRaises(FounderConfigurationError):
                encrypt_secret(TEST_SECRET)

    def test_step_up_rechecks_email_eligibility_after_locking_the_account(self):
        access = self.enroll()
        User.objects.filter(pk=self.owner.pk).update(verification_required=True, is_verified=False)
        self.owner.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            verify_step_up(SimpleNamespace(user=self.owner), PASSWORD, pyotp.TOTP(TEST_SECRET).now())
        access.refresh_from_db()
        self.assertEqual((access.last_accepted_step, access.failed_attempts), (-1, 0))
        self.assertFalse(AuditLog.objects.filter(action="FOUNDER_UNLOCKED").exists())

    def test_step_up_requires_both_factors_per_session_and_does_not_echo_them(self):
        access = self.enroll()
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.get(reverse("core:super_admin_dashboard")), reverse("founder:overview"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("founder:overview")), reverse("founder:verify"))
        code = pyotp.TOTP(TEST_SECRET).now()
        bad = self.client.post(reverse("founder:verify"), {"password": "incorrect-password-secret", "code": code})
        self.assertEqual(bad.status_code, 200)
        self.assertNotContains(bad, "incorrect-password-secret")
        self.assertNotContains(bad, code)
        access.refresh_from_db()
        self.assertEqual(access.last_accepted_step, -1)
        old_session_key = self.client.session.session_key
        good = self.client.post(reverse("founder:verify"), {"password": PASSWORD, "code": code})
        self.assertRedirects(good, reverse("founder:overview"), fetch_redirect_response=False)
        self.assertNotEqual(self.client.session.session_key, old_session_key)
        response = self.client.get(reverse("founder:overview"))
        self.assertContains(response, "YOUR COMMAND CENTRE")
        self.assertNotContains(response, PASSWORD)
        self.assertNotContains(response, TEST_SECRET)
        self.assertNotContains(response, self.owner.password)
        another = Client()
        another.force_login(self.owner)
        self.assertRedirects(another.get(reverse("founder:accounts")), reverse("founder:verify"))
        # A code accepted in one browser cannot be replayed in another.
        replay = another.post(reverse("founder:verify"), {"password": PASSWORD, "code": code})
        self.assertEqual(replay.status_code, 200)
        self.assertContains(replay, "Verification was not accepted")
        self.assertRedirects(another.get(reverse("founder:overview")), reverse("founder:verify"))
        self.assertTrue(AuditLog.objects.filter(action="FOUNDER_UNLOCKED", user=self.owner).exists())
        self.assertTrue(AuditLog.objects.filter(action="FOUNDER_UNLOCK_FAILED", user=self.owner).exists())
        details = " ".join(AuditLog.objects.values_list("description", flat=True))
        for value in (PASSWORD, code, TEST_SECRET, "incorrect-password-secret"):
            self.assertNotIn(value, details)

    @override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.PBKDF2PasswordHasher",
                                         "django.contrib.auth.hashers.MD5PasswordHasher"])
    def test_password_hash_upgrade_during_step_up_preserves_the_authenticated_session(self):
        self.owner.password = make_password(PASSWORD, hasher="md5")
        self.owner.save(update_fields=["password"])
        self.enroll()
        self.client.force_login(self.owner)
        response = self.client.post(reverse("founder:verify"),
                                    {"password": PASSWORD, "code": pyotp.TOTP(TEST_SECRET).now()})
        self.assertEqual(response.status_code, 302)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.password.startswith("pbkdf2_sha256$"))
        self.assertTrue(self.owner.check_password(PASSWORD))
        self.assertEqual(self.client.get(reverse("founder:overview")).status_code, 200)

    def test_colliding_adjacent_totp_steps_cannot_replay_the_same_value(self):
        access = self.enroll()
        now = timezone.now()
        self.client.force_login(self.owner)
        other = Client()
        other.force_login(self.owner)
        with patch("apps.core.founder_security.timezone.now", return_value=now), \
             patch("apps.core.founder_security.pyotp.TOTP.at", return_value="123456"):
            first = self.client.post(reverse("founder:verify"), {"password": PASSWORD, "code": "123456"})
            replay = other.post(reverse("founder:verify"), {"password": PASSWORD, "code": "123456"})
        self.assertEqual(first.status_code, 302)
        self.assertContains(replay, "Verification was not accepted")
        access.refresh_from_db()
        self.assertEqual(access.last_accepted_step, int(now.timestamp()) // 30 + 1)

    def test_lockout_is_persistent_cross_session_and_expires(self):
        self.enroll()
        now = timezone.now().replace(microsecond=0)
        self.client.force_login(self.owner)
        other = Client()
        other.force_login(self.owner)
        for number in range(5):
            client = self.client if number % 2 == 0 else other
            with patch("apps.core.founder_security.timezone.now", return_value=now):
                response = client.post(reverse("founder:verify"), {"password": PASSWORD, "code": "not-a-code"})
            self.assertEqual(response.status_code, 429 if number == 4 else 200)
        self.assertEqual(response["Retry-After"], "900")
        access = FounderAccess.objects.get(user=self.owner)
        self.assertEqual(access.failed_attempts, 5)
        with patch("apps.core.founder_security.timezone.now", return_value=now + timedelta(minutes=1)):
            blocked = other.post(reverse("founder:verify"), {"password": PASSWORD,
                "code": pyotp.TOTP(TEST_SECRET).at(int((now + timedelta(minutes=1)).timestamp()))})
        self.assertEqual(blocked.status_code, 429)
        later = now + timedelta(minutes=16)
        with patch("apps.core.founder_security.timezone.now", return_value=later):
            good = other.post(reverse("founder:verify"), {"password": PASSWORD,
                "code": pyotp.TOTP(TEST_SECRET).at(int(later.timestamp()))})
        self.assertEqual(good.status_code, 302)
        access.refresh_from_db()
        self.assertEqual(access.failed_attempts, 0)
        self.assertIsNone(access.locked_until)

    def test_expiry_lock_revocation_and_demote_fail_closed(self):
        access = self.enroll()
        now = timezone.now().replace(microsecond=0)
        self.assertEqual(self.unlock(when=now).status_code, 302)
        with patch("apps.core.founder_security.timezone.now", return_value=now + timedelta(minutes=14)):
            self.assertEqual(self.client.get(reverse("founder:overview")).status_code, 200)
        with patch("apps.core.founder_security.timezone.now", return_value=now + timedelta(minutes=15)):
            self.assertRedirects(self.client.get(reverse("founder:overview")), reverse("founder:verify"), fetch_redirect_response=False)
        later = now + timedelta(minutes=16)
        with patch("apps.core.founder_security.timezone.now", return_value=later):
            self.assertEqual(self.client.post(reverse("founder:verify"), {"password": PASSWORD,
                "code": pyotp.TOTP(TEST_SECRET).at(int(later.timestamp()))}).status_code, 302)
            self.assertEqual(self.client.get(reverse("founder:lock")).status_code, 405)
            self.assertEqual(self.client.post(reverse("founder:lock")).status_code, 302)
            self.assertNotIn(SESSION_KEY, self.client.session)
            self.assertRedirects(self.client.get(reverse("founder:overview")), reverse("founder:verify"), fetch_redirect_response=False)
        # Re-enrollment/version changes invalidate an already unlocked session.
        later = now + timedelta(minutes=17)
        with patch("apps.core.founder_security.timezone.now", return_value=later):
            self.assertEqual(self.client.post(reverse("founder:verify"), {"password": PASSWORD,
                "code": pyotp.TOTP(TEST_SECRET).at(int(later.timestamp()))}).status_code, 302)
            access.access_version = __import__("uuid").uuid4()
            access.save(update_fields=["access_version"])
            self.assertRedirects(self.client.get(reverse("founder:overview")), reverse("founder:verify"), fetch_redirect_response=False)
        User.objects.filter(pk=self.owner.pk).update(is_superuser=False, role="ADMIN")
        self.assertEqual(self.client.get(reverse("founder:overview")).status_code, 403)

    def test_busy_sqlite_verification_fails_safely_without_echoing_credentials(self):
        if connection.vendor != "sqlite":
            self.skipTest("SQLite-specific contention response")
        self.enroll()
        self.client.force_login(self.owner)
        with patch("apps.core.founder_views.verify_step_up", side_effect=OperationalError("database is locked")):
            response = self.client.post(reverse("founder:verify"), {"password": PASSWORD, "code": "123456"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "1")
        self.assertNotContains(response, PASSWORD, status_code=503)
        self.assertNotContains(response, "123456", status_code=503)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_csrf_https_and_protective_headers(self):
        self.enroll()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        verify = client.get(reverse("founder:verify"))
        code = pyotp.TOTP(TEST_SECRET).now()
        self.assertEqual(client.post(reverse("founder:verify"), {"password": PASSWORD, "code": code}).status_code, 403)
        cookie = client.cookies["csrftoken"].value
        self.assertEqual(client.post(reverse("founder:verify"), {"password": PASSWORD, "code": code}, HTTP_X_CSRFTOKEN=cookie).status_code, 302)
        self.assertEqual(client.post(reverse("founder:lock")).status_code, 403)
        response = client.get(reverse("founder:overview"))
        for token in ("private", "no-store", "no-cache"):
            self.assertIn(token, response["Cache-Control"])
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertIn("frame-ancestors 'none'", response["Content-Security-Policy"])
        self.assertIn("form-action 'self'", response["Content-Security-Policy"])
        self.assertIn("noindex", response["X-Robots-Tag"])
        with override_settings(DEBUG=False):
            insecure = client.get(reverse("founder:overview"))
            self.assertEqual(insecure.status_code, 403)
            self.assertIn("no-store", insecure["Cache-Control"])
            self.assertEqual(client.get(reverse("founder:overview"), secure=True).status_code, 200)

    def test_support_is_audited_and_never_renders_private_content_or_changes_records(self):
        self.enroll()
        category = Category.objects.create(name="Privacy", slug="founder-privacy")
        exam = Exam.objects.create(title="PRIVATE-EXAM-TITLE-NEVER-SHOW", slug="founder-private-exam",
                                   category=category, creator=self.student, is_private_practice=True, duration_minutes=47)
        paper = QuestionPaperUpload.objects.create(title="PRIVATE-PAPER-TITLE-NEVER-SHOW", category=category,
            uploaded_by=self.student, is_student_private=True, private_pdf_file="papers/private-source-secret.pdf",
            practice_exam=exam, status="extracted")
        Question.objects.create(category=category, creator=self.student, source_paper=paper, is_private_practice=True,
                                subject="Math", question_text="PRIVATE-QUESTION-SECRET-NEVER-SHOW")
        deadline = timezone.now() + timedelta(minutes=27)
        attempt = ExamAttempt.objects.create(exam=exam, student=self.student, expires_at=deadline,
                                              status="in_progress", score_obtained="41.37")
        checkout = PackCheckout.objects.create(user=self.student, product="pdf", amount_paise=29900, units=20, status="paid")
        grant = CreditGrant.objects.create(user=self.student, checkout=checkout, product="pdf", units=20, remaining=19, is_test=True)
        use = CreditUse.objects.create(user=self.student, kind="pdf", grant=grant, resource_key="private-pdf-resource")
        legacy_log = AuditLog.objects.create(user=self.student, action="PDF_PAPER_UPLOADED",
            description="Private title PRIVATE-EXAM-TITLE-NEVER-SHOW and path private-source-secret.pdf; score 41.37")
        AuditLog.objects.create(user=self.student, action="FOUNDER_ACCOUNT_VIEWED",
            description=f"Opened support metadata for account #{self.student.pk}. EXTRA-SECRET-OTP")
        self.assertEqual(self.unlock().status_code, 302)
        response = self.client.get(reverse("founder:account_detail", args=[self.student.pk]))
        self.assertEqual(response.status_code, 200)
        for secret in ("PRIVATE-EXAM-TITLE-NEVER-SHOW", "PRIVATE-PAPER-TITLE-NEVER-SHOW", "PRIVATE-QUESTION-SECRET-NEVER-SHOW",
                       "private-source-secret.pdf", "private-pdf-resource", "41.37", self.student.password):
            self.assertNotContains(response, secret)
        self.assertContains(response, "Private practice")
        self.assertContains(response, "Original expiry")
        self.assertTrue(AuditLog.objects.filter(action="FOUNDER_ACCOUNT_VIEWED", user=self.owner,
                                                 description__contains=f"#{self.student.pk}").exists())
        search = self.client.post(reverse("founder:accounts"), {"q": self.student.email})
        self.assertContains(search, self.student.username)
        entry = AuditLog.objects.filter(action="FOUNDER_ACCOUNT_SEARCH").latest("pk")
        self.assertNotIn(self.student.email, entry.description)
        for route in ("founder:overview", "founder:series", "founder:payments", "founder:activity"):
            page = self.client.get(reverse(route))
            self.assertEqual(page.status_code, 200)
            for private in ("PRIVATE-EXAM-TITLE-NEVER-SHOW", "PRIVATE-QUESTION-SECRET-NEVER-SHOW", "private-source-secret.pdf"):
                self.assertNotContains(page, private)
        activity = self.client.get(reverse("founder:activity"))
        self.assertContains(activity, "PDF_PAPER_UPLOADED")  # metadata remains visible
        self.assertContains(activity, "Details withheld to protect private content")
        self.assertContains(activity, f"Opened support metadata for account #{self.student.pk}.")
        for secret in ("private-source-secret.pdf", "PRIVATE-EXAM-TITLE-NEVER-SHOW", "41.37", "EXTRA-SECRET-OTP"):
            self.assertNotContains(activity, secret)
        legacy_log.refresh_from_db()
        self.assertIn("private-source-secret.pdf", legacy_log.description)  # not deleted or modified
        self.assertTrue(AuditLog.objects.filter(action="FOUNDER_OVERVIEW_VIEWED", user=self.owner).exists())
        self.assertTrue(AuditLog.objects.filter(action="FOUNDER_SERIES_VIEWED", user=self.owner).exists())
        # Existing owner-only file endpoint remains owner-only.
        self.assertIn(self.client.get(reverse("questions:practice_pdf", args=[paper.pk])).status_code, (403, 404))
        attempt.refresh_from_db(); grant.refresh_from_db(); use.refresh_from_db(); paper.refresh_from_db()
        self.assertEqual((attempt.status, attempt.expires_at, str(attempt.score_obtained)), ("in_progress", deadline, "41.37"))
        self.assertEqual((grant.remaining, grant.is_revoked, use.reversed_at, paper.private_pdf_file.name),
                         (19, False, None, "papers/private-source-secret.pdf"))

    def test_sensitive_views_are_not_mutable_through_post_or_get(self):
        self.enroll()
        self.assertEqual(self.unlock().status_code, 302)
        for name in ("overview", "account_detail", "series", "payments", "activity"):
            args = [self.student.pk] if name == "account_detail" else []
            response = self.client.post(reverse(f"founder:{name}", args=args), {"role": "SUPER_ADMIN", "is_active": "false"})
            self.assertEqual(response.status_code, 405)
        self.student.refresh_from_db()
        self.assertEqual((self.student.role, self.student.is_active), ("STUDENT", True))
