"""Tests for the redesigned auth + role-aware public registration.

Covers Student/Teacher registration, server-side role validation, dashboard
routing, unauthorized access, and the requested navigation/dashboard removals.
"""
from django.test import TestCase
from django.urls import reverse

from apps.exams.models import Category
from .models import User


BASE = {
    "first_name": "Sam", "last_name": "Lee",
    "email": "sam@example.com", "password": "StrongPass99!",
    "confirm_password": "StrongPass99!",
}


class PublicRegistrationTests(TestCase):
    def _post(self, **extra):
        data = dict(BASE)
        data.update(extra)
        return self.client.post(reverse("accounts:register"), data)

    def test_role_link_only_preselects_the_two_public_roles(self):
        for requested, expected in (("teacher", "TEACHER"), ("STUDENT", "STUDENT"), ("ADMIN", "STUDENT"), ("", "STUDENT")):
            with self.subTest(requested=requested):
                response = self.client.get(reverse("accounts:register"), {"role": requested})
                self.assertEqual(response.context["form"]["role"].value(), expected)
                self.assertContains(response, f'value="{expected}" checked')

    def test_student_registration_creates_student_and_routes(self):
        resp = self._post(username="stud1", email="s1@example.com", role="STUDENT", target_exam="JEE")
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(username="stud1")
        self.assertEqual(user.role, User.Roles.STUDENT)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        # Logged in + routed to the student dashboard.
        follow = self.client.get(resp.url, follow=True)
        self.assertEqual(follow.redirect_chain[-1][0], reverse("core:student_dashboard"))

    def test_teacher_registration_creates_unprivileged_teacher(self):
        resp = self._post(username="teach1", email="t1@example.com", role="TEACHER")
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(username="teach1")
        self.assertEqual(user.role, User.Roles.TEACHER)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        # No categories granted by self-registration.
        self.assertEqual(user.assigned_categories.count(), 0)
        follow = self.client.get(resp.url, follow=True)
        self.assertEqual(follow.redirect_chain[-1][0], reverse("core:teacher_dashboard"))

    def test_missing_role_defaults_to_student_legacy_client(self):
        # A missing role defaults to student, which still requires an exam.
        resp = self._post(username="legacy1", email="l1@example.com", target_exam="NEET")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(User.objects.get(username="legacy1").role, User.Roles.STUDENT)

    def test_cannot_self_register_as_admin(self):
        resp = self._post(username="evil1", email="e1@example.com", role="ADMIN")
        # Form is rejected (invalid choice); no account is created.
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="evil1").exists())

    def test_cannot_self_register_as_superuser_role(self):
        resp = self._post(username="evil2", email="e2@example.com", role="SUPER_ADMIN")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="evil2").exists())

    def test_cannot_inject_staff_or_superuser_flags(self):
        # Even if a crafted POST includes elevated flags, they are ignored.
        resp = self._post(username="inject1", email="i1@example.com", role="TEACHER",
                          is_staff="on", is_superuser="true")
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(username="inject1")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)


class DashboardRoutingAndAccessTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="s_user", email="s_user@ex.com", password="Password123!", role=User.Roles.STUDENT, exam_track="jee")
        self.teacher = User.objects.create_user(
            username="t_user", email="t_user@ex.com", password="Password123!", role=User.Roles.TEACHER)

    def test_router_sends_student_and_teacher_correctly(self):
        self.client.login(username="s_user", password="Password123!")
        r = self.client.get(reverse("core:dashboard_router"))
        self.assertRedirects(r, reverse("core:student_dashboard"))
        self.client.logout()
        self.client.login(username="t_user", password="Password123!")
        r = self.client.get(reverse("core:dashboard_router"))
        self.assertRedirects(r, reverse("core:teacher_dashboard"))

    def test_student_cannot_access_teacher_area(self):
        self.client.login(username="s_user", password="Password123!")
        self.assertEqual(self.client.get(reverse("questions:paper_upload")).status_code, 403)
        self.assertEqual(self.client.get(reverse("core:teacher_dashboard")).status_code, 403)

    def test_teacher_cannot_access_student_dashboard(self):
        self.client.login(username="t_user", password="Password123!")
        self.assertEqual(self.client.get(reverse("core:student_dashboard")).status_code, 403)

    def test_anonymous_redirected_to_login(self):
        r = self.client.get(reverse("core:student_dashboard"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("accounts:login"), r.url)

    def test_self_registered_teacher_sees_pending_access_guidance(self):
        self.client.login(username="t_user", password="Password123!")
        html = self.client.get(reverse("core:teacher_dashboard")).content.decode()
        self.assertIn("awaiting category access", html)

    def test_teacher_with_category_has_no_pending_banner(self):
        cat = Category.objects.create(name="Physics", slug="physics", is_active=True)
        self.teacher.assigned_categories.add(cat)
        self.client.login(username="t_user", password="Password123!")
        html = self.client.get(reverse("core:teacher_dashboard")).content.decode()
        self.assertNotIn("awaiting category access", html)


class NavigationAndSectionRemovalTests(TestCase):
    def test_removed_nav_links_absent_on_home(self):
        html = self.client.get(reverse("core:home")).content.decode()
        self.assertNotIn("Exam Categories", html)   # nav link removed
        self.assertNotIn("Architecture", html)      # nav + footer link removed
        self.assertIn("Contact", html)              # useful nav kept

    def test_removed_student_dashboard_sections_absent(self):
        user = User.objects.create_user(
            username="s_sec", email="s_sec@ex.com", password="Password123!", role=User.Roles.STUDENT, exam_track="jee")
        self.client.login(username="s_sec", password="Password123!")
        html = self.client.get(reverse("core:student_dashboard")).content.decode()
        self.assertNotIn("Available Online Examinations", html)
        self.assertNotIn("Explore Examination Categories", html)

    def test_register_page_offers_both_roles(self):
        html = self.client.get(reverse("accounts:register")).content.decode()
        self.assertIn('value="STUDENT"', html)
        self.assertIn('value="TEACHER"', html)
