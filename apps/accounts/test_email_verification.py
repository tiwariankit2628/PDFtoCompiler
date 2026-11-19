"""Verification links, account gates, delivery errors and running-test safety."""
import re
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlsplit

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion
from apps.questions.models import Question, QuestionOption
from .models import User
from .verification import base_url, send_verification, token_user


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   PUBLIC_BASE_URL="https://learn.example.test", DEFAULT_FROM_EMAIL="sender@example.test")
class EmailVerificationTests(TestCase):
    def register(self, *, role="STUDENT", username="newverify", email="newverify@example.test"):
        return self.client.post(reverse("accounts:register"), {
            "first_name": "New", "last_name": "Learner", "username": username, "email": email,
            "role": role, "target_exam": "JEE" if role == "STUDENT" else "",
            "password": "StrongPassword123!", "confirm_password": "StrongPassword123!",
        }, HTTP_HOST="testserver")

    def mail_path(self, index=-1):
        body = mail.outbox[index].body
        url = re.search(r"https://learn\.example\.test/\S+", body).group(0)
        return urlsplit(url).path

    def test_new_registration_requires_confirmation_without_trusting_host_header(self):
        response = self.register()
        self.assertRedirects(response, reverse("accounts:verification_pending"), fetch_redirect_response=False)
        user = User.objects.get(username="newverify")
        self.assertTrue(user.verification_required)
        self.assertFalse(user.is_verified)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["newverify@example.test"])
        self.assertNotIn("testserver", mail.outbox[0].body)
        path = self.mail_path()
        self.assertRedirects(self.client.get(reverse("payments:plans")), reverse("accounts:verification_pending"), fetch_redirect_response=False)
        blocked = self.client.post(reverse("exams:classroom_join"), {"code": "ABCDE"})
        self.assertRedirects(blocked, reverse("accounts:verification_pending"), fetch_redirect_response=False)
        blocked_json = self.client.post(reverse("exams:classroom_join"), "{}", content_type="application/json")
        self.assertEqual(blocked_json.status_code, 403)
        link = self.client.get(path)
        self.assertContains(link, "Confirm your email")
        self.assertIn("no-store", link["Cache-Control"])
        self.assertEqual(link["Referrer-Policy"], "same-origin")
        user.refresh_from_db()
        self.assertFalse(user.is_verified)  # email scanners cannot verify with GET
        self.client.post(path)
        user.refresh_from_db()
        self.assertTrue(user.is_verified)
        self.assertEqual(self.client.get(reverse("payments:plans")).status_code, 200)
        self.assertContains(self.client.get(path), "no longer valid")
        self.assertRedirects(self.client.post(path), reverse("accounts:verification_pending"), fetch_redirect_response=False)

    def test_teacher_registration_is_verified_too_and_old_accounts_are_grandfathered(self):
        self.register(role="TEACHER", username="newteacher", email="newteacher@example.test")
        teacher = User.objects.get(username="newteacher")
        self.assertTrue(teacher.verification_required)
        self.assertFalse(teacher.is_verified)
        self.assertRedirects(self.client.get(reverse("exams:classroom_hub")), reverse("accounts:verification_pending"), fetch_redirect_response=False)
        self.client.post(self.mail_path())
        self.assertEqual(self.client.get(reverse("exams:classroom_hub")).status_code, 200)
        self.client.logout()
        legacy = User.objects.create_user("legacy-verification", email="legacy@example.test", role="STUDENT", exam_track="neet", is_verified=False)
        self.client.force_login(legacy)
        self.assertEqual(self.client.get(reverse("payments:plans")).status_code, 200)
        legacy.refresh_from_db()
        self.assertFalse(legacy.verification_required)
        self.assertFalse(legacy.is_verified)

    def test_resend_throttle_old_link_expiry_and_invalid_signature(self):
        self.register()
        user = User.objects.get(username="newverify")
        old_path = self.mail_path()
        self.assertEqual(self.client.get(reverse("accounts:verification_resend")).status_code, 405)
        self.client.post(reverse("accounts:verification_resend"))
        self.assertEqual(len(mail.outbox), 1)
        User.objects.filter(pk=user.pk).update(verification_sent_at=timezone.now() - timedelta(seconds=61))
        self.client.post(reverse("accounts:verification_resend"))
        self.assertEqual(len(mail.outbox), 2)
        new_path = self.mail_path()
        self.assertNotEqual(new_path, old_path)
        self.assertContains(self.client.get(old_path), "no longer valid")
        token = new_path.rstrip("/").split("/")[-1]
        self.assertIsNotNone(token_user(token))
        self.assertIsNone(token_user(token + "tampered"))
        with override_settings(EMAIL_VERIFICATION_MAX_AGE=-1):
            self.assertIsNone(token_user(token))
            self.assertContains(self.client.get(new_path), "no longer valid")
        self.client.post(new_path)
        user.refresh_from_db()
        self.assertTrue(user.is_verified)

    def test_bad_configuration_or_mail_failure_never_claims_delivery(self):
        with override_settings(PUBLIC_BASE_URL="https://example.test@attacker.invalid/"):
            self.assertEqual(base_url(), "")
            self.register()
            self.assertEqual(len(mail.outbox), 0)
            self.assertContains(self.client.get(reverse("accounts:verification_pending")), "not configured or could not be sent")
        user = User.objects.get(username="newverify")
        with patch("apps.accounts.verification.send_mail", side_effect=OSError("secret smtp details")):
            self.assertEqual(send_verification(user), "unavailable")
        user.refresh_from_db()
        self.assertFalse(user.is_verified)
        with override_settings(DEBUG=False, PUBLIC_BASE_URL="http://insecure.example.test"):
            self.assertEqual(base_url(), "")
        for value in ("", "file:///tmp/x", "https://example.test/path", "https://example.test?q=1", "https://a.test:bad"):
            with self.subTest(value=value), override_settings(PUBLIC_BASE_URL=value):
                self.assertEqual(base_url(), "")

    def test_resend_does_not_mail_a_deleted_or_deactivated_account(self):
        user = User.objects.create_user("verify-disabled", email="disabled@example.test", role="STUDENT",
                                        is_active=False, verification_required=True)
        self.assertEqual(send_verification(user), "unavailable")
        self.assertEqual(len(mail.outbox), 0)
        user.delete()
        self.assertEqual(send_verification(user), "unavailable")
        self.assertEqual(len(mail.outbox), 0)

    def test_admin_email_edits_require_new_verification_for_students_and_teachers(self):
        admin = User.objects.create_user("verify-edit-admin", email="vea@example.test", role="ADMIN")
        student = User.objects.create_user("verify-edit-student", email="ves@example.test", role="STUDENT",
                                           exam_track="jee", is_verified=True, verification_required=True)
        teacher = User.objects.create_user("verify-edit-teacher", email="vet@example.test", role="TEACHER",
                                           is_verified=True, verification_required=True)
        old_student_token, old_teacher_token = student.verification_token, teacher.verification_token
        self.client.force_login(admin)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("accounts:student_edit", args=[student.pk]), {
                "first_name": "Student", "last_name": "One", "email": "new-student@example.test",
                "target_exam": "JEE", "phone": "", "institute_name": "", "bio": "", "is_active": "on",
                # Even a checked old verification checkbox cannot verify a new address.
                "is_verified": "on",
            })
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        student.refresh_from_db()
        self.assertFalse(student.is_verified)
        self.assertTrue(student.verification_required)
        self.assertNotEqual(student.verification_token, old_student_token)
        self.assertEqual(mail.outbox[-1].to, ["new-student@example.test"])
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("accounts:teacher_edit", args=[teacher.pk]), {
                "first_name": "Teacher", "last_name": "One", "email": "new-teacher@example.test",
                "phone": "", "institute_name": "", "bio": "", "is_active": "on",
            })
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        teacher.refresh_from_db()
        self.assertFalse(teacher.is_verified)
        self.assertTrue(teacher.verification_required)
        self.assertNotEqual(teacher.verification_token, old_teacher_token)
        self.assertEqual(mail.outbox[-1].to, ["new-teacher@example.test"])
        self.client.force_login(student)
        self.assertRedirects(self.client.get(reverse("payments:plans")), reverse("accounts:verification_pending"), fetch_redirect_response=False)
        self.client.post(self.mail_path(-2))
        student.refresh_from_db()
        self.assertTrue(student.is_verified)
        self.client.force_login(teacher)
        self.assertRedirects(self.client.get(reverse("exams:classroom_hub")), reverse("accounts:verification_pending"), fetch_redirect_response=False)
        self.client.post(self.mail_path(-1))
        teacher.refresh_from_db()
        self.assertTrue(teacher.is_verified)

    def test_django_admin_email_edit_invalidates_previous_verification(self):
        root = User.objects.create_superuser("verify-root", "verify-root@example.test", "StrongPassword123!")
        student = User.objects.create_user("verify-managed", email="managed-old@example.test", role="STUDENT",
                                           exam_track="jee", is_verified=True, verification_required=True)
        old_token = student.verification_token
        self.client.force_login(root)
        page = self.client.get(reverse("admin:accounts_user_change", args=[student.pk]))
        self.assertEqual(page.status_code, 200)
        joined = timezone.localtime(student.date_joined)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("admin:accounts_user_change", args=[student.pk]), {
                "username": student.username, "email": "managed-new@example.test", "role": "STUDENT",
                "date_joined_0": joined.strftime("%Y-%m-%d"), "date_joined_1": joined.strftime("%H:%M:%S"),
                "is_active": "on", "is_verified": "on", "first_name": "", "last_name": "",
                "phone": "", "target_exam": "", "institute_name": "", "bio": "", "_save": "Save",
            })
        self.assertEqual(response.status_code, 302, response.context and response.context["adminform"].form.errors)
        student.refresh_from_db()
        self.assertFalse(student.is_verified)
        self.assertTrue(student.verification_required)
        self.assertNotEqual(student.verification_token, old_token)
        self.assertEqual(mail.outbox[-1].to, ["managed-new@example.test"])

    def test_changing_address_during_attempt_preserves_session_timer_and_answers(self):
        teacher = User.objects.create_user("verify-teacher", email="vt@example.test", role="TEACHER")
        student = User.objects.create_user("verify-active", email="old@example.test", role="STUDENT", exam_track="jee", is_verified=True)
        category = Category.objects.create(name="Verification category", slug="verify-category")
        exam = Exam.objects.create(title="Keep the timer", slug="verify-timer", creator=teacher, category=category,
                                   target_type="jee", status="published", duration_minutes=23, is_free=True)
        q = Question.objects.create(category=category, creator=teacher, target_type="jee", subject="Physics", question_text="Working question")
        a = QuestionOption.objects.create(question=q, option_key="A", option_text="A", is_correct=True)
        QuestionOption.objects.create(question=q, option_key="B", option_text="B")
        ExamQuestion.objects.create(exam=exam, question=q, order=1, positive_marks=4, negative_marks=1)
        exam.recalculate_marks()
        self.client.force_login(student)
        self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        attempt = ExamAttempt.objects.get(exam=exam, student=student)
        deadline = attempt.expires_at
        with self.captureOnCommitCallbacks(execute=True):
            changed = self.client.post(reverse("accounts:profile"), {
                "first_name": "Active", "last_name": "Student", "email": "new@example.test",
                "target_exam": "JEE", "phone": "", "institute_name": "", "bio": ""})
        self.assertRedirects(changed, reverse("accounts:verification_pending"), fetch_redirect_response=False)
        student.refresh_from_db()
        self.assertTrue(student.verification_required)
        self.assertFalse(student.is_verified)
        self.assertEqual(student.email, "new@example.test")
        self.assertEqual(mail.outbox[-1].to, ["new@example.test"])
        pending = self.client.get(reverse("accounts:verification_pending"))
        self.assertContains(pending, "Keep the timer")
        session = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(session.status_code, 200)
        self.assertIn("no-store", session["Cache-Control"])
        saved = self.client.post(reverse("exams:exam_save_answer", args=[attempt.attempt_uuid]),
            data={"response_id": attempt.responses.get().pk, "selected_option_id": a.pk, "action": "save_next"},
            content_type="application/json")
        self.assertEqual(saved.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, deadline)
        self.assertEqual(attempt.responses.get().selected_option_id, a.pk)
        self.assertRedirects(self.client.get(reverse("payments:plans")), reverse("accounts:verification_pending"), fetch_redirect_response=False)
        self.client.post(self.mail_path())
        student.refresh_from_db()
        self.assertTrue(student.is_verified)
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, deadline)
