from django.test import TestCase
from django.urls import reverse
from apps.accounts.models import User
from apps.exams.models import Category, Exam, ExamAttempt


class CoreViewTests(TestCase):
    def setUp(self):
        self.super_admin = User.objects.create_superuser(
            username="super_user",
            email="super@test.com",
            password="Password123!",
            role=User.Roles.SUPER_ADMIN,
        )
        self.admin = User.objects.create_user(
            username="admin_user",
            email="adm@test.com",
            password="Password123!",
            role=User.Roles.ADMIN,
            is_staff=True,
        )
        self.teacher = User.objects.create_user(
            username="teacher_user",
            email="teach@test.com",
            password="Password123!",
            role=User.Roles.TEACHER,
            is_staff=True,
        )
        self.student = User.objects.create_user(
            username="student_user",
            email="stu@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
            exam_track="jee",
        )
        self.category = Category.objects.create(
            name="General Science",
            slug="general-science",
            description="Basic science test category",
            is_active=True,
            display_order=1,
        )

    def test_public_pages_render_successfully(self):
        for route_name in ["core:home", "core:about", "core:contact"]:
            resp = self.client.get(reverse(route_name))
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "TestForge CBT")

    def test_about_discloses_test_only_payments_and_extraction_limits(self):
        response = self.client.get(reverse("core:about"))
        self.assertContains(response, "Razorpay · test mode only")
        self.assertContains(response, "real payments are not yet available")
        self.assertContains(response, "unsent changes can be lost")
        self.assertContains(response, "extraction can miss or misread content")
        self.assertContains(response, "can themselves contain printed answers")
        self.assertNotContains(response, "recurring plans, coupons")

    def test_dashboard_router_redirects_by_role(self):
        # Student redirect
        self.client.login(username="student_user", password="Password123!")
        resp = self.client.get(reverse("core:dashboard_router"))
        self.assertRedirects(resp, reverse("core:student_dashboard"))
        self.client.logout()

        # Teacher redirect
        self.client.login(username="teacher_user", password="Password123!")
        resp = self.client.get(reverse("core:dashboard_router"))
        self.assertRedirects(resp, reverse("core:teacher_dashboard"))
        self.client.logout()

        # Regular Admin redirect
        self.client.login(username="admin_user", password="Password123!")
        resp = self.client.get(reverse("core:dashboard_router"))
        self.assertRedirects(resp, reverse("core:admin_dashboard"))
        self.client.logout()

        # Super Admin redirect
        self.client.login(username="super_user", password="Password123!")
        resp = self.client.get(reverse("core:dashboard_router"))
        self.assertRedirects(resp, reverse("core:super_admin_dashboard"))
        self.client.logout()

    def test_super_admin_dashboard_permissions(self):
        # Super admin has access
        self.client.login(username="super_user", password="Password123!")
        resp = self.client.get(reverse("core:super_admin_dashboard"))
        self.assertEqual(resp.status_code, 200)
        # An ordinary super-administrator retains the operations dashboard;
        # founder access is a separate explicit enrollment and step-up.
        self.assertContains(resp, "Administrator Operations Portal")
        self.assertNotContains(resp, "FOUNDER SPACE")
        self.client.logout()

        # Standard admin is denied (403 PermissionDenied)
        self.client.login(username="admin_user", password="Password123!")
        resp_admin = self.client.get(reverse("core:super_admin_dashboard"))
        self.assertEqual(resp_admin.status_code, 403)
        self.client.logout()

        # Student is denied (403 PermissionDenied)
        self.client.login(username="student_user", password="Password123!")
        resp_student = self.client.get(reverse("core:super_admin_dashboard"))
        self.assertEqual(resp_student.status_code, 403)

    def test_admin_dashboard_permissions(self):
        self.client.login(username="admin_user", password="Password123!")
        resp = self.client.get(reverse("core:admin_dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Administrator Operations Portal")
        self.client.logout()

        # Student denied
        self.client.login(username="student_user", password="Password123!")
        resp = self.client.get(reverse("core:admin_dashboard"))
        self.assertEqual(resp.status_code, 403)

    def test_teacher_dashboard_permissions(self):
        self.client.login(username="teacher_user", password="Password123!")
        resp = self.client.get(reverse("core:teacher_dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Instructor Workspace")
        self.client.logout()

        # Student denied
        self.client.login(username="student_user", password="Password123!")
        resp = self.client.get(reverse("core:teacher_dashboard"))
        self.assertEqual(resp.status_code, 403)

    def test_student_dashboard_renders_actual_metrics(self):
        # Create an exam and attempt
        exam = Exam.objects.create(
            title="Science Assessment 1",
            slug="science-assessment-1",
            category=self.category,
            creator=self.teacher,
            status=Exam.ExamStatus.PUBLISHED,
            target_type="jee",
        )
        ExamAttempt.objects.create(
            exam=exam,
            student=self.student,
            status=ExamAttempt.AttemptStatus.SUBMITTED,
            score_obtained=85.0,
            total_marks=100.0,
            percentage=85.0,
            is_passed=True,
        )

        self.client.login(username="student_user", password="Password123!")
        resp = self.client.get(reverse("core:student_dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Science Assessment 1")
        self.assertContains(resp, "85.0%")

    def test_student_dashboard_metrics_do_not_leak_unreleased_or_in_progress_scores(self):
        released = Exam.objects.create(title="Released", slug="released", category=self.category,
            creator=self.teacher, status=Exam.ExamStatus.PUBLISHED, show_result_immediately=True, target_type="jee")
        hidden = Exam.objects.create(title="Unreleased", slug="unreleased", category=self.category,
            creator=self.teacher, status=Exam.ExamStatus.PUBLISHED, show_result_immediately=False, target_type="jee")
        ExamAttempt.objects.create(exam=released, student=self.student, status=ExamAttempt.AttemptStatus.SUBMITTED,
            percentage=80, is_passed=True)
        ExamAttempt.objects.create(exam=hidden, student=self.student, status=ExamAttempt.AttemptStatus.SUBMITTED,
            percentage=99, is_passed=True)
        ExamAttempt.objects.create(exam=released, student=self.student, status=ExamAttempt.AttemptStatus.IN_PROGRESS,
            percentage=70, is_passed=True)
        self.client.force_login(self.student)
        response = self.client.get(reverse("core:student_dashboard"))
        self.assertEqual(response.context["total_attempts"], 3)
        self.assertEqual(response.context["passed_attempts"], 1)
        self.assertEqual(response.context["avg_percentage"], 80)
        self.assertNotContains(response, "99%")
        self.assertNotContains(response, "70%")

    def test_audit_log_view_restricted_to_super_admin(self):
        # Super admin can view
        self.client.login(username="super_user", password="Password123!")
        resp = self.client.get(reverse("core:audit_log_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "System Audit Logs")
        self.client.logout()

        # Admin denied
        self.client.login(username="admin_user", password="Password123!")
        resp = self.client.get(reverse("core:audit_log_list"))
        self.assertEqual(resp.status_code, 403)
