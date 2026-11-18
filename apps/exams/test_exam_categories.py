"""Category setup, editability and authorization regressions."""
import importlib
import io
from types import SimpleNamespace

from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from .forms import ExamForm
from .models import Category, Exam, ExamAttempt


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CategorySetupTests(TestCase):
    def setUp(self):
        # The migration creates defaults for a fresh test database. Each test
        # works with its own isolated catalog, rolled back by Django afterwards.
        Category.objects.all().delete()
        self.admin = User.objects.create_superuser("category-admin", "ca@example.test", "x")
        self.teacher = User.objects.create_user("category-teacher", email="ct@example.test", password="x", role="TEACHER")
        self.student = User.objects.create_user("category-student", email="cs@example.test", password="x", role="STUDENT")

    @staticmethod
    def migrate_data():
        migration = importlib.import_module("apps.exams.migrations.0012_seed_empty_categories")
        migration.seed_empty_catalog(apps, SimpleNamespace(connection=connection))

    @staticmethod
    def form_data(category="", title="Test with category"):
        return {"title": title, "category": category, "duration_minutes": "60", "total_marks": "100",
                "passing_percentage": "40", "default_positive_marks": "4", "default_negative_marks": "1",
                "max_attempts": "1", "status": "draft"}

    def test_data_migration_initializes_empty_catalog_once_without_touching_exam_data(self):
        self.migrate_data()
        self.assertEqual(Category.objects.count(), 7)
        self.assertEqual(Category.objects.filter(is_active=True).count(), 7)
        category = Category.objects.get(slug="engineering-medical-entrance")
        self.assertIn("JEE", category.description)
        self.assertIn("NEET", category.description)
        exam = Exam.objects.create(title="Existing examination", category=category, creator=self.teacher)
        attempt = ExamAttempt.objects.create(exam=exam, student=self.student, status="submitted", score_obtained=12)
        self.teacher.assigned_categories.add(category)
        before = list(Category.objects.values_list("pk", "name", "slug", "is_active", "updated_at"))
        self.migrate_data()
        self.assertEqual(before, list(Category.objects.values_list("pk", "name", "slug", "is_active", "updated_at")))
        attempt.refresh_from_db()
        self.assertEqual(attempt.score_obtained, 12)
        self.assertEqual(list(self.teacher.assigned_categories.values_list("pk", flat=True)), [category.pk])
        self.assertEqual(Exam.objects.get(pk=exam.pk).category_id, category.pk)

    def test_migration_never_changes_or_augments_existing_catalog_including_inactive_and_system(self):
        cases = [
            ("An active custom category", "custom", True),
            ("An intentionally inactive category", "inactive", False),
            ("Engineering & Medical Entrance", "different-slug", False),
            ("Other name", "engineering-medical-entrance", False),
            ("Student Private Practice (System)", "student-private-practice", False),
        ]
        for name, slug, active in cases:
            with self.subTest(name=name, slug=slug):
                Category.objects.all().delete()
                cat = Category.objects.create(name=name, slug=slug, is_active=active, description="Keep this")
                before = list(Category.objects.values())
                self.migrate_data()
                self.assertEqual(before, list(Category.objects.values()))
                cat.refresh_from_db()
                self.assertEqual((cat.name, cat.slug, cat.is_active, cat.description), (name, slug, active, "Keep this"))

    def test_optional_seed_command_is_idempotent_and_skips_name_and_slug_collisions(self):
        name_conflict = Category.objects.create(name="Competitive Examinations", slug="custom-competitive", is_active=False)
        slug_conflict = Category.objects.create(name="Private custom name", slug="engineering-medical-entrance", is_active=False)
        output = io.StringIO()
        call_command("seed_categories", stdout=output)
        self.assertIn("Skipped 'Competitive Examinations'", output.getvalue())
        self.assertEqual(Category.objects.count(), 7)  # two existing + five genuinely new
        self.assertFalse(Category.objects.filter(slug="competitive-examinations").exists())
        name_conflict.refresh_from_db(); slug_conflict.refresh_from_db()
        self.assertFalse(name_conflict.is_active)
        self.assertEqual((slug_conflict.name, slug_conflict.is_active), ("Private custom name", False))
        before = list(Category.objects.values_list("pk", "name", "slug", "is_active", "updated_at"))
        call_command("seed_categories", stdout=io.StringIO())
        self.assertEqual(before, list(Category.objects.values_list("pk", "name", "slug", "is_active", "updated_at")))

    def test_fresh_seeded_choices_allow_admin_and_unassigned_teacher_to_create(self):
        self.migrate_data()
        category = Category.objects.get(slug="engineering-medical-entrance")
        for user in (self.admin, self.teacher):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                page = self.client.get(reverse("exams:exam_create"))
                self.assertContains(page, "Engineering &amp; Medical Entrance", html=False)
                self.assertNotContains(page, "No examination categories are available")
                title = "Exam " + user.username
                response = self.client.post(reverse("exams:exam_create"), self.form_data(category.pk, title))
                exam = Exam.objects.get(title=title)
                self.assertRedirects(response, reverse("exams:exam_questions", args=[exam.pk]), fetch_redirect_response=False)
                self.assertEqual((exam.creator_id, exam.category_id), (user.pk, category.pk))

    def test_empty_and_deliberately_inactive_catalogs_show_role_specific_guidance(self):
        for with_inactive in (False, True):
            with self.subTest(with_inactive=with_inactive):
                Category.objects.all().delete()
                if with_inactive:
                    inactive = Category.objects.create(name="Do not activate", slug="do-not-activate", is_active=False)
                self.client.force_login(self.admin)
                url = reverse("exams:exam_create")
                page = self.client.get(url)
                self.assertContains(page, "No examination categories are available")
                self.assertContains(page, reverse("exams:category_create"))
                self.assertContains(page, reverse("exams:category_list"))
                self.assertContains(page, "Recheck categories")
                self.assertNotContains(page, "Do not activate")
                response = self.client.post(url, self.form_data(title="Preserved form draft"))
                self.assertContains(response, "Select an examination category")
                self.assertContains(response, "Preserved form draft")
                self.assertFalse(Exam.objects.filter(title="Preserved form draft").exists())
                self.client.force_login(self.teacher)
                teacher_page = self.client.get(url)
                self.assertContains(teacher_page, "Ask an administrator")
                self.assertNotContains(teacher_page, reverse("exams:category_create"))
                self.assertEqual(self.client.get(reverse("exams:category_list")).status_code, 403)
                self.assertEqual(self.client.post(reverse("exams:category_create"), {}).status_code, 403)
                self.client.force_login(self.student)
                self.assertEqual(self.client.get(url).status_code, 403)
                if with_inactive:
                    inactive.refresh_from_db()
                    self.assertFalse(inactive.is_active)
                    self.assertEqual(Category.objects.count(), 1)

    def test_admin_can_create_a_category_with_an_auto_generated_slug(self):
        self.client.force_login(self.admin)
        data = {"name": "NEET & JEE", "slug": "", "icon": "bi-cpu", "display_order": 0, "is_active": "on"}
        response = self.client.post(reverse("exams:category_create"), data)
        self.assertRedirects(response, reverse("exams:category_list"), fetch_redirect_response=False)
        self.assertTrue(Category.objects.filter(name="NEET & JEE", slug="neet-jee", is_active=True).exists())
        response = self.client.post(reverse("exams:category_create"), dict(data, name="!!!"))
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "slug", "Enter a URL slug containing letters or numbers.")

    def test_category_queryset_preserves_assigned_and_fallback_policy_and_rejects_forgery(self):
        allowed = Category.objects.create(name="Assigned", slug="assigned")
        other = Category.objects.create(name="Unassigned", slug="unassigned")
        inactive = Category.objects.create(name="Inactive", slug="inactive", is_active=False)
        self.teacher.assigned_categories.add(allowed, inactive)
        self.assertEqual(set(ExamForm(user=self.teacher).fields["category"].queryset), {allowed})
        self.assertEqual(set(ExamForm(user=self.admin).fields["category"].queryset), {allowed, other})
        self.client.force_login(self.teacher)
        for blocked in (other, inactive):
            with self.subTest(blocked=blocked.name):
                response = self.client.post(reverse("exams:exam_create"), self.form_data(blocked.pk))
                self.assertEqual(response.status_code, 200)
                self.assertIn("category", response.context["form"].errors)
                self.assertFalse(Exam.objects.filter(title="Test with category").exists())
        # Existing behavior: no active assignment falls back to active categories.
        self.teacher.assigned_categories.remove(allowed)
        self.assertEqual(set(ExamForm(user=self.teacher).fields["category"].queryset), {allowed, other})
        self.assertEqual(self.teacher.assigned_categories.count(), 1)  # no permissions silently changed

    def test_edit_preserves_current_unavailable_category_without_permitting_other_unavailable_choices(self):
        assigned = Category.objects.create(name="Assigned", slug="assigned")
        current = Category.objects.create(name="Old category", slug="old-category", is_active=False)
        other_inactive = Category.objects.create(name="Other inactive", slug="other-inactive", is_active=False)
        other_unassigned = Category.objects.create(name="Other unassigned", slug="other-unassigned")
        self.teacher.assigned_categories.add(assigned)
        exam = Exam.objects.create(title="Old exam", category=current, creator=self.teacher)
        self.client.force_login(self.teacher)
        url = reverse("exams:exam_edit", args=[exam.pk])
        page = self.client.get(url)
        self.assertContains(page, "current; unavailable for new exams")
        self.assertContains(page, "You may keep it")
        self.assertEqual(set(page.context["form"].fields["category"].queryset), {assigned, current})
        for blocked in (other_inactive, other_unassigned):
            response = self.client.post(url, self.form_data(blocked.pk, "Forged edit"))
            self.assertEqual(response.status_code, 200)
            self.assertIn("category", response.context["form"].errors)
            exam.refresh_from_db()
            self.assertEqual((exam.category_id, exam.title), (current.pk, "Old exam"))
        response = self.client.post(url, self.form_data(current.pk, "Safe edit"))
        self.assertRedirects(response, reverse("exams:exam_list"), fetch_redirect_response=False)
        exam.refresh_from_db()
        self.assertEqual((exam.category_id, exam.title), (current.pk, "Safe edit"))
        current.refresh_from_db()
        self.assertFalse(current.is_active)
        # A non-owner with no assignment to the current category still cannot edit.
        outsider = User.objects.create_user("outside", email="outside@example.test", password="x", role="TEACHER")
        outsider.assigned_categories.add(assigned)
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, self.form_data(current.pk)).status_code, 403)
