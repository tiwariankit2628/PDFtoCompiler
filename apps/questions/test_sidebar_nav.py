"""Student sidebar active-highlight: exactly one correct item per page.

Covers the filter mapping, every sidebar destination, a related page (editing a
bank question), and query parameters / pagination.
"""
from types import SimpleNamespace

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category
from apps.questions.models import Question, QuestionOption
from apps.questions.templatetags.sidebar_nav import sidebar_key


class SidebarKeyFilterTests(TestCase):
    def test_exact_route_mapping(self):
        cases = {
            "questions:practice_list": "pdf_practice",
            "questions:practice_settings": "pdf_practice",
            "questions:practice_question_edit": "pdf_practice",
            "questions:practice_bank": "question_bank",
            "questions:practice_bank_edit": "question_bank",
            "questions:practice_history": "practice_history",
            "questions:practice_result": "practice_history",
            "payments:plans": "plans",
            "exams:exam_catalog": "exams",
            "core:student_dashboard": "dashboard",
            "core:dashboard_router": "dashboard",
        }
        for view_name, expected in cases.items():
            self.assertEqual(sidebar_key(SimpleNamespace(view_name=view_name)), expected, view_name)

    def test_bank_and_history_are_not_mistaken_for_pdf_practice(self):
        # The old bug: substring 'practice_' matched bank/history too.
        self.assertNotEqual(sidebar_key(SimpleNamespace(view_name="questions:practice_bank")), "pdf_practice")
        self.assertNotEqual(sidebar_key(SimpleNamespace(view_name="questions:practice_history")), "pdf_practice")

    def test_unknown_or_missing_route_is_blank(self):
        self.assertEqual(sidebar_key(SimpleNamespace(view_name="questions:question_bank")), "")  # teacher bank
        self.assertEqual(sidebar_key(SimpleNamespace(view_name="")), "")
        self.assertEqual(sidebar_key(None), "")


class SidebarHighlightIntegrationTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Nav", slug="nav", is_active=True)
        self.student = User.objects.create_user(username="nav_student", email="nav@test.com",
                                                 password="Password123!", role=User.Roles.STUDENT, exam_track="jee")
        self.client.login(username="nav_student", password="Password123!")

    def _assert_single_active(self, response, expected_url):
        content = response.content.decode()
        # Exactly one sidebar link is the current page.
        self.assertEqual(content.count('aria-current="page"'), 1, "exactly one active item expected")
        # And it is the expected destination.
        self.assertIn(f'aria-current="page" href="{expected_url}"', content)

    def test_each_destination_highlights_its_own_item(self):
        destinations = [
            reverse("questions:practice_list"),
            reverse("questions:practice_bank"),
            reverse("questions:practice_history"),
            reverse("payments:plans"),
            reverse("exams:exam_catalog"),
        ]
        for url in destinations:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            self._assert_single_active(resp, url)

    def test_dashboard_highlights_dashboard(self):
        resp = self.client.get(reverse("core:dashboard_router"), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content.decode().count('aria-current="page"'), 1)
        self.assertIn('aria-current="page" href="' + reverse("core:dashboard_router") + '"', resp.content.decode())

    def test_query_params_and_pagination_keep_item_active(self):
        url = reverse("questions:practice_bank")
        resp = self.client.get(url + "?page=1&q=physics&sort=recent")
        self.assertEqual(resp.status_code, 200)
        self._assert_single_active(resp, url)  # bank stays active regardless of query string

    def test_editing_a_bank_question_keeps_question_bank_active(self):
        question = Question.objects.create(
            category=self.category, creator=self.student, is_private_practice=True,
            subject="Physics", question_text="Owned bank question",
            target_type="jee",
        )
        for i, key in enumerate("AB"):
            QuestionOption.objects.create(question=question, option_key=key, option_text=key, order=i)
        resp = self.client.get(reverse("questions:practice_bank_edit", args=[question.pk]))
        self.assertEqual(resp.status_code, 200)
        self._assert_single_active(resp, reverse("questions:practice_bank"))

    def test_pdf_practice_not_active_on_bank_page(self):
        resp = self.client.get(reverse("questions:practice_bank"))
        content = resp.content.decode()
        # The PDF practice link must NOT be the current page on the bank page.
        self.assertNotIn('aria-current="page" href="' + reverse("questions:practice_list") + '"', content)
