"""Meaningful recorded-data aggregates; no real provider, account or private file."""
from datetime import date, datetime, timedelta

from unittest.mock import patch

import pyotp
from cryptography.fernet import Fernet
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.founder_metrics import activity_chart, finance_summary
from apps.core.founder_security import encrypt_secret
from apps.core.models import ActivityTrackingState, AuditLog, FounderAccess, LoginEvent
from apps.exams.models import (Category, ClassroomActivation, ClassroomParticipant, ClassroomSession,
                               Exam, ExamAttempt, TestSeries)
from apps.payments.models import (Checkout, CreditGrant, CreditUse, PackCheckout, PackPayment,
                                  PackRefund, Payment, PracticePlan, Refund)
from apps.questions.models import QuestionPaperUpload

KEY = Fernet.generate_key().decode()
SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
PASSWORD = "SyntheticMetricsPassword987!"


@override_settings(DEBUG=True, FOUNDER_TOTP_ENCRYPTION_KEY=KEY,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class FounderMetricsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser("metrics-owner", "metrics-owner@example.test", PASSWORD)
        self.a = User.objects.create_user("metrics-a", "metrics-a@example.test", PASSWORD, role="STUDENT", exam_track="jee")
        self.b = User.objects.create_user("metrics-b", "metrics-b@example.test", PASSWORD, role="STUDENT", exam_track="neet")
        self.c = User.objects.create_user("metrics-c", "metrics-c@example.test", PASSWORD, role="STUDENT", exam_track="", is_active=False)
        self.teacher = User.objects.create_user("metrics-teacher", "metrics-teacher@example.test", PASSWORD, role="TEACHER")
        FounderAccess.objects.create(user=self.owner, is_enabled=True, encrypted_totp_secret=encrypt_secret(SECRET))
        self.client.force_login(self.owner)
        result = self.client.post(reverse("founder:verify"), {"password": PASSWORD, "code": pyotp.TOTP(SECRET).now()})
        self.assertEqual(result.status_code, 302)
        self.now = timezone.now()
        ActivityTrackingState.objects.update_or_create(key="sign_in", defaults={"started_at": self.now - timedelta(days=8)})

    def pack(self, user, product, status, amount, pid, currency="INR"):
        checkout = PackCheckout.objects.create(user=user, product=product, amount_paise=amount, units=20,
                                               currency=currency, status="paid")
        payment = PackPayment.objects.create(checkout=checkout, provider_payment_id=pid, amount_paise=amount,
                                             currency=currency, status=status)
        return checkout, payment

    def test_people_sign_ins_tracks_and_attempt_types_are_distinct(self):
        for user, hours in ((self.a, 2), (self.a, 3), (self.b, 5), (self.teacher, 6), (self.c, 200)):
            LoginEvent.objects.create(user=user, role_snapshot=user.role, track_snapshot=user.exam_track,
                                      occurred_at=self.now - timedelta(hours=hours))
        LoginEvent.objects.create(user=self.c, role_snapshot="STUDENT", occurred_at=self.now + timedelta(days=1))
        LoginEvent.objects.create(user=None, role_snapshot="STUDENT", occurred_at=self.now)
        category = Category.objects.create(name="Metrics", slug="metrics")
        private = Exam.objects.create(title="Private", slug="metrics-private", category=category,
                                      creator=self.a, is_private_practice=True)
        class_exam = Exam.objects.create(title="Classroom", slug="metrics-class", category=category,
                                         creator=self.teacher, is_classroom_only=True)
        session = ClassroomSession.objects.create(exam=class_exam, teacher=self.teacher, code="METRIC")
        series_exam = Exam.objects.create(title="Series", slug="metrics-series", category=category,
                                          creator=self.teacher, target_type="jee", status="published")
        series = TestSeries.objects.get(track="jee")
        slot = series.slots.get(position=1); slot.exam = series_exam; slot.save(update_fields=["exam"])
        ordinary = Exam.objects.create(title="Public", slug="metrics-public", category=category, creator=self.teacher)
        ExamAttempt.objects.create(student=self.a, exam=private)
        ExamAttempt.objects.create(student=self.b, exam=class_exam, classroom_session=session)
        ExamAttempt.objects.create(student=self.a, exam=series_exam, status="submitted")
        ExamAttempt.objects.create(student=self.b, exam=ordinary, status="submitted")
        QuestionPaperUpload.objects.create(title="Failed private upload", category=category, uploaded_by=self.a,
                                           is_student_private=True, status="failed")
        response = self.client.get(reverse("founder:overview"))
        self.assertEqual(response.status_code, 200)
        people = response.context["people"]
        self.assertEqual((people["students"], people["students_enabled"], people["teachers"],
                          people["jee"], people["neet"], people["unclassified"]), (3, 2, 1, 1, 1, 1))
        signs = response.context["sign_ins"]
        # a twice, b, teacher and founder once; an anonymized event is still
        # counted as an event, but not a distinct person. Future is excluded.
        self.assertEqual((signs["unique"], signs["events"], signs["students"], signs["teachers"]), (4, 6, 2, 1))
        attempts = response.context["attempts"]
        self.assertEqual(tuple(attempts[k] for k in ("total", "private", "classroom", "series", "institutional")),
                         (4, 1, 1, 1, 1))
        self.assertEqual(response.context["alerts"]["failed_papers"], 1)
        self.assertContains(response, "not a live-user count")
        self.assertContains(response, "Earlier history is not reconstructed")
        # A title/blank slot never becomes ready just because it is assigned.
        series_page = self.client.get(reverse("founder:series"))
        jee = next(g for g in series_page.context["groups"] if g["series"].track == "jee")
        self.assertEqual((jee["assigned"], jee["ready"], jee["attempts"], jee["students"], jee["on_sale"]),
                         (1, 0, 1, 1, False))
        self.assertContains(series_page, "Needs review")
        self.assertContains(series_page, "Unassigned slot")

    def test_finance_counts_people_vs_records_and_recorded_refunds_without_fabricated_net(self):
        p1, first = self.pack(self.a, "pdf", "captured", 29900, "pay_Synthetic1")
        p2, second = self.pack(self.b, "classroom", "partial_refund", 9900, "pay_Synthetic2")
        _, no_row = self.pack(self.b, "activation", "refunded", 29900, "pay_Synthetic3")
        self.pack(self.a, "pdf", "captured", 5000, "pay_SyntheticUSD", currency="USD")
        self.pack(self.b, "pdf", "failed", 10000, "pay_SyntheticFailed")
        PackRefund.objects.create(payment=second, provider_refund_id="rfnd_SyntheticPack", amount_paise=3000)
        plan = PracticePlan.objects.create(name="Historical test plan", slug="historical-metrics", billing="one_time", price_paise=5000)
        checkout = Checkout.objects.create(user=self.a, plan=plan, billing="one_time", amount_paise=5000, status="paid")
        old = Payment.objects.create(checkout=checkout, provider_payment_id="pay_SyntheticOld", amount_paise=5000,
                                     status="partial_refund", currency="INR")
        Refund.objects.create(payment=old, provider_refund_id="rfnd_SyntheticOld", amount_paise=500)
        grant = CreditGrant.objects.create(user=self.a, checkout=p1, product="pdf", units=20, remaining=19, is_test=True)
        CreditUse.objects.create(user=self.a, kind="pdf", grant=grant, resource_key="synthetic-use-only")
        pending = PackCheckout.objects.create(user=self.a, product="pdf", amount_paise=29900, units=20, status="initiating")
        summary = finance_summary()
        self.assertEqual({key: summary[key] for key in ("buyers", "captured_count", "gross_paise", "refund_paise", "refund_count", "missing_refund_rows", "uncertain_checkouts")},
                         {"buyers": 2, "captured_count": 5, "gross_paise": 74700, "refund_paise": 3500,
                          "refund_count": 2, "missing_refund_rows": 1, "uncertain_checkouts": 1})
        only_a = finance_summary(self.a)
        self.assertEqual((only_a["buyers"], only_a["captured_count"], only_a["gross_paise"], only_a["refund_paise"]),
                         (1, 3, 34900, 500))
        response = self.client.get(reverse("founder:payments"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test-only gateway")
        self.assertContains(response, "not revenue")
        self.assertContains(response, "747.00")
        self.assertContains(response, "35.00")
        self.assertContains(response, "mode not stored per payment")
        self.assertContains(response, "USD 5,000 minor units")
        self.assertContains(response, "A checkout is not proof of payment")
        self.assertContains(response, str(pending.reference))  # no payment exists for this checkout
        self.assertContains(response, "Creating checkout")
        self.assertContains(response, "pay_SyntheticFailed")  # displayed but not counted as captured
        self.assertContains(response, "rfnd_SyntheticPack")
        self.assertNotContains(response, "synthetic-use-only")
        scoped = self.client.get(reverse("founder:payments") + f"?account={self.a.pk}")
        self.assertEqual(scoped.context["money"]["buyers"], 1)
        self.assertContains(scoped, "pay_Synthetic1")
        self.assertContains(scoped, str(pending.reference))
        self.assertNotContains(scoped, str(p2.reference))
        self.assertNotContains(scoped, "pay_Synthetic2")
        self.assertTrue(AuditLog.objects.filter(action="FOUNDER_FINANCE_VIEWED", description__contains=f"#{self.a.pk}").exists())
        for bad in ("x", "-1", "9" * 200):
            self.assertEqual(self.client.get(reverse("founder:payments") + "?account=" + bad).status_code, 404)

    def test_checkout_pagination_keeps_the_selected_account_scope(self):
        for _ in range(22):
            PackCheckout.objects.create(user=self.a, product="pdf", amount_paise=29900,
                                        units=20, status="initiating")
        other = PackCheckout.objects.create(user=self.b, product="pdf", amount_paise=29900,
                                             units=20, status="initiating")
        url = reverse("founder:payments")
        first = self.client.get(url, {"account": self.a.pk})
        self.assertEqual(first.context["pack_checkouts"].paginator.count, 22)
        self.assertContains(first, f"account={self.a.pk}&amp;pack_checkout_page=2#pack_checkout_page")
        self.assertNotContains(first, str(other.reference))
        second = self.client.get(url, {"account": self.a.pk, "pack_checkout_page": 2})
        self.assertEqual(len(second.context["pack_checkouts"]), 2)
        self.assertNotContains(second, str(other.reference))

    def test_tracking_snapshot_and_last_login_do_not_create_historical_events(self):
        old = self.now - timedelta(days=30)
        User.objects.filter(pk=self.a.pk).update(last_login=old)
        self.assertFalse(LoginEvent.objects.filter(user=self.a).exists())
        detail = self.client.get(reverse("founder:account_detail", args=[self.a.pk]))
        self.assertEqual(detail.context["logins"].paginator.count, 0)
        self.assertContains(detail, "older activity is not reconstructed")
        self.assertTrue(detail.context["account"].last_login)
        another = self.client_class()
        another.force_login(self.a)
        event = LoginEvent.objects.filter(user=self.a).latest("pk")
        self.assertEqual((event.role_snapshot, event.track_snapshot), ("STUDENT", "jee"))
        User.objects.filter(pk=self.a.pk).update(role="TEACHER", exam_track="neet")
        event.refresh_from_db()
        self.assertEqual((event.role_snapshot, event.track_snapshot), ("STUDENT", "jee"))
        self.assertFalse(hasattr(event, "ip_address"))

    def test_chart_groups_local_days_and_counts_unique_people_not_events(self):
        day = date(2026, 1, 10)
        local_time = timezone.make_aware(datetime(2026, 1, 10, 0, 10), timezone.get_current_timezone())
        User.objects.filter(pk=self.a.pk).update(date_joined=local_time)
        LoginEvent.objects.create(user=self.a, role_snapshot="STUDENT", occurred_at=local_time)
        LoginEvent.objects.create(user=self.a, role_snapshot="STUDENT", occurred_at=local_time + timedelta(hours=1))
        LoginEvent.objects.create(user=self.b, role_snapshot="STUDENT", occurred_at=local_time + timedelta(hours=2))
        row = next(row for row in activity_chart(today=day) if row["day"] == day)
        self.assertEqual((row["registrations"], row["sign_ins"]), (1, 2))
        self.assertGreater(row["sign_in_height"], row["reg_height"])

    def test_chart_marks_untracked_and_partial_days_and_excludes_future_records(self):
        day = date(2026, 1, 10)
        now = timezone.make_aware(datetime(2026, 1, 10, 12, 0), timezone.get_current_timezone())
        marker = timezone.make_aware(datetime(2026, 1, 8, 10, 0), timezone.get_current_timezone())
        ActivityTrackingState.objects.filter(pk="sign_in").update(started_at=marker)
        User.objects.filter(pk=self.a.pk).update(date_joined=now + timedelta(hours=1))
        LoginEvent.objects.create(user=self.a, role_snapshot="STUDENT", occurred_at=now - timedelta(hours=1))
        LoginEvent.objects.create(user=self.b, role_snapshot="STUDENT", occurred_at=now + timedelta(hours=1))
        rows = {row["day"]: row for row in activity_chart(today=day, now=now)}
        self.assertEqual((rows[date(2026, 1, 7)]["sign_in_coverage"],
                          rows[date(2026, 1, 7)]["sign_in_available"]), ("before", False))
        self.assertEqual((rows[date(2026, 1, 8)]["sign_in_coverage"],
                          rows[date(2026, 1, 8)]["sign_in_available"]), ("partial", True))
        self.assertEqual((rows[day]["sign_ins"], rows[day]["registrations"]), (1, 0))
        # Even an imported event from before the marker is shown as a recorded
        # event, but its historical coverage remains explicitly unknown.
        LoginEvent.objects.create(user=self.b, role_snapshot="STUDENT", occurred_at=marker - timedelta(days=1))
        previous = {row["day"]: row for row in activity_chart(today=day, now=now)}[date(2026, 1, 7)]
        self.assertEqual((previous["sign_ins"], previous["sign_in_coverage"], previous["sign_in_available"]),
                         (1, "before", True))

    def test_teacher_support_has_real_session_and_window_metadata_without_codes_or_contents(self):
        category = Category.objects.create(name="Teacher support", slug="teacher-support-metrics")
        exam = Exam.objects.create(title="PRIVATE-TEACHER-TITLE", slug="teacher-private-metrics", category=category,
                                   creator=self.teacher, is_classroom_only=True)
        window = ClassroomActivation.objects.create(teacher=self.teacher, exam=exam, starts_at=self.now,
                                                     ends_at=self.now + timedelta(hours=24))
        session = ClassroomSession.objects.create(teacher=self.teacher, exam=exam, activation=window,
                                                   code="SECRETJOIN", label="SECRETCLASSLABEL")
        ClassroomParticipant.objects.create(student=self.a, session=session)
        ExamAttempt.objects.create(student=self.a, exam=exam, classroom_session=session, expires_at=self.now + timedelta(minutes=30))
        QuestionPaperUpload.objects.create(title="PRIVATE-TEACHER-PDF", category=category, uploaded_by=self.teacher,
                                           is_classroom_private=True, status="failed")
        result = self.client.get(reverse("founder:account_detail", args=[self.teacher.pk]))
        self.assertContains(result, "Teacher activation windows")
        self.assertContains(result, "Teacher classroom sessions")
        self.assertEqual(result.context["classrooms"][0].participant_count, 1)
        self.assertEqual(result.context["classrooms"][0].attempt_count, 1)
        self.assertEqual(result.context["activations"][0].session_count, 1)
        self.assertContains(result, "Extraction Failed")
        for secret in ("SECRETJOIN", "SECRETCLASSLABEL", "PRIVATE-TEACHER-TITLE", "PRIVATE-TEACHER-PDF"):
            self.assertNotContains(result, secret)
        window.refresh_from_db()
        self.assertEqual(window.ends_at, self.now + timedelta(hours=24))

    def test_tracking_database_error_does_not_block_the_existing_login(self):
        other = self.client_class()
        with patch("apps.core.signals.LoginEvent.objects.create", side_effect=DatabaseError("synthetic outage")), \
             self.assertLogs("apps.core.signals", level="ERROR") as logs:
            self.assertTrue(other.login(username=self.a.username, password=PASSWORD))
        self.assertEqual(int(other.session["_auth_user_id"]), self.a.pk)
        self.assertFalse(LoginEvent.objects.filter(user=self.a).exists())
        self.assertIn("could not be recorded", logs.output[0])
        self.assertNotIn(PASSWORD, " ".join(logs.output))

    def test_directory_search_does_not_expose_query_to_url_or_audit_and_escapes_markup(self):
        User.objects.filter(pk=self.a.pk).update(first_name='<script>alert("founder-xss")</script>')
        result = self.client.post(reverse("founder:accounts"), {"q": self.a.email})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("?", result.request["PATH_INFO"])
        self.assertContains(result, "&lt;script&gt;", html=False)
        self.assertNotContains(result, '<script>alert("founder-xss")</script>')
        self.assertNotIn(self.a.email, AuditLog.objects.filter(action="FOUNDER_ACCOUNT_SEARCH").latest("pk").description)
        invalid = self.client.post(reverse("founder:accounts"), {"q": "a"})
        self.assertContains(invalid, "Enter 2–120 characters")
        response = self.client.get(reverse("founder:accounts") + "?role=STUDENT")
        self.assertEqual(response.context["users"].paginator.count, 3)
