from django.test import TestCase
from django.urls import reverse
from apps.exams.models import Category
from .forms import StudentRegistrationForm, UserLoginForm
from .models import User


class UserModelTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="teststudent",
            email="student@example.com",
            password="SecurePassword123!",
            role=User.Roles.STUDENT,
            target_exam="JEE Advanced",
        )
        self.teacher = User.objects.create_user(
            username="testteacher",
            email="teacher@example.com",
            password="SecurePassword123!",
            role=User.Roles.TEACHER,
            institute_name="Apex Academy",
        )
        self.admin = User.objects.create_superuser(
            username="testadmin",
            email="admin@example.com",
            password="SecurePassword123!",
        )

    def test_user_roles_and_properties(self):
        self.assertTrue(self.student.is_student)
        self.assertFalse(self.student.is_teacher)
        self.assertFalse(self.student.is_admin_role)

        self.assertTrue(self.teacher.is_teacher)
        self.assertFalse(self.teacher.is_student)

        self.assertTrue(self.admin.is_admin_role)
        self.assertEqual(self.admin.role, User.Roles.SUPER_ADMIN)

    def test_student_registration_form(self):
        form_data = {
            "first_name": "Aarav",
            "last_name": "Patel",
            "username": "aaravp",
            "email": "aarav@example.com",
            "target_exam": "NEET-UG",
            "password": "StrongPassword99!",
            "confirm_password": "StrongPassword99!",
        }
        form = StudentRegistrationForm(data=form_data)
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual(user.role, User.Roles.STUDENT)
        self.assertEqual(user.target_exam, "NEET-UG")

    def test_student_registration_view(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "first_name": "Priya",
                "last_name": "Sharma",
                "username": "priyas",
                "email": "priya@example.com",
                "target_exam": "UPSC CSE",
                "password": "StrongPassword99!",
                "confirm_password": "StrongPassword99!",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username="priyas").exists())

    def test_login_with_email_and_username(self):
        # Login with username
        resp_user = self.client.post(
            reverse("accounts:login"),
            {"username": "teststudent", "password": "SecurePassword123!"},
        )
        self.assertEqual(resp_user.status_code, 302)

        self.client.logout()

        # Login with email
        resp_email = self.client.post(
            reverse("accounts:login"),
            {"username": "student@example.com", "password": "SecurePassword123!"},
        )
        self.assertEqual(resp_email.status_code, 302)


class StudentManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_mgr",
            email="admin_mgr@test.com",
            password="Password123!",
            role=User.Roles.ADMIN,
            is_staff=True,
        )
        self.student = User.objects.create_user(
            username="stu1",
            email="stu1@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
            target_exam="JEE Main",
        )
        self.other_student = User.objects.create_user(
            username="stu2",
            email="stu2@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
        )

    def test_student_list_search_and_filter(self):
        self.client.login(username="admin_mgr", password="Password123!")
        res = self.client.get(reverse("accounts:student_list") + "?q=JEE")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "stu1")
        self.assertNotContains(res, "stu2")

    def test_admin_can_create_student(self):
        self.client.login(username="admin_mgr", password="Password123!")
        res = self.client.post(
            reverse("accounts:student_create"),
            {
                "first_name": "Rohan",
                "last_name": "Verma",
                "username": "rohanv",
                "email": "rohan@test.com",
                "password": "StrongPassword99!",
                "target_exam": "NEET-UG",
                "is_active": True,
                "is_verified": True,
            },
        )
        self.assertEqual(res.status_code, 302)
        self.assertTrue(User.objects.filter(username="rohanv").exists())

    def test_admin_can_toggle_student_status(self):
        self.client.login(username="admin_mgr", password="Password123!")
        self.assertTrue(self.student.is_active)

        res = self.client.post(reverse("accounts:student_toggle_status", args=[self.student.pk]))
        self.assertEqual(res.status_code, 302)
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_active)

    def test_student_cannot_access_student_management(self):
        self.client.login(username="stu1", password="Password123!")
        res = self.client.get(reverse("accounts:student_list"))
        self.assertEqual(res.status_code, 403)


class TeacherManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_mgr2",
            email="admin_mgr2@test.com",
            password="Password123!",
            role=User.Roles.ADMIN,
            is_staff=True,
        )
        self.category = Category.objects.create(
            name="Medical Entrance", slug="medical-entrance"
        )
        self.teacher = User.objects.create_user(
            username="teacher_prof",
            email="teacher_prof@test.com",
            password="Password123!",
            role=User.Roles.TEACHER,
            is_staff=True,
        )

    def test_admin_can_create_teacher_with_category(self):
        self.client.login(username="admin_mgr2", password="Password123!")
        res = self.client.post(
            reverse("accounts:teacher_create"),
            {
                "first_name": "Dr. Sunita",
                "last_name": "Rao",
                "username": "sunitarao",
                "email": "sunita@test.com",
                "password": "StrongPassword99!",
                "institute_name": "Pre-Med Academy",
                "assigned_categories": [self.category.pk],
                "is_active": True,
            },
        )
        self.assertEqual(res.status_code, 302)
        teacher_created = User.objects.get(username="sunitarao")
        self.assertEqual(teacher_created.role, User.Roles.TEACHER)
        self.assertIn(self.category, teacher_created.assigned_categories.all())


class AdminUserManagementTests(TestCase):
    def setUp(self):
        self.super_admin = User.objects.create_superuser(
            username="super_gov",
            email="super_gov@test.com",
            password="Password123!",
            role=User.Roles.SUPER_ADMIN,
        )
        self.admin_user = User.objects.create_user(
            username="std_admin",
            email="std_admin@test.com",
            password="Password123!",
            role=User.Roles.ADMIN,
            is_staff=True,
        )

    def test_super_admin_can_access_admin_list(self):
        self.client.login(username="super_gov", password="Password123!")
        res = self.client.get(reverse("accounts:admin_list"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "std_admin")

    def test_standard_admin_cannot_access_admin_list(self):
        self.client.login(username="std_admin", password="Password123!")
        res = self.client.get(reverse("accounts:admin_list"))
        self.assertEqual(res.status_code, 403)

    def test_cannot_deactivate_or_delete_super_admin(self):
        self.client.login(username="super_gov", password="Password123!")
        # Attempt to delete super_admin account
        res = self.client.post(reverse("accounts:admin_delete", args=[self.super_admin.pk]))
        self.assertEqual(res.status_code, 302)
        # Account must still exist
        self.assertTrue(User.objects.filter(pk=self.super_admin.pk).exists())


class InactiveAccountRestrictionsTests(TestCase):
    def setUp(self):
        self.inactive_student = User.objects.create_user(
            username="inactive_stu",
            email="inactive@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
            is_active=False,
        )

    def test_inactive_student_cannot_login(self):
        res = self.client.post(
            reverse("accounts:login"),
            {"username": "inactive_stu", "password": "Password123!"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Invalid username/email or password")
