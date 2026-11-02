from django.core.management.base import BaseCommand
from apps.accounts.models import User


class Command(BaseCommand):
    help = "Seeds initial demo accounts (Super Admin, Teacher, Student) for local testing."

    def handle(self, *args, **options):
        # 1. Super Admin
        admin_user, created = User.objects.get_or_create(
            username="admin",
            defaults={
                "email": "admin@cbtplatform.local",
                "first_name": "System",
                "last_name": "Administrator",
                "role": User.Roles.SUPER_ADMIN,
                "is_staff": True,
                "is_superuser": True,
                "is_verified": True,
            },
        )
        if created:
            admin_user.set_password("admin12345")
            admin_user.save()
            self.stdout.write(self.style.SUCCESS("Created Super Admin: admin / admin12345"))
        else:
            self.stdout.write("Super Admin 'admin' already exists.")

        # 2. Teacher
        teacher_user, created = User.objects.get_or_create(
            username="teacher",
            defaults={
                "email": "teacher@cbtplatform.local",
                "first_name": "Instructor",
                "last_name": "Sharma",
                "role": User.Roles.TEACHER,
                "institute_name": "Apex Science Academy",
                "is_staff": True,
                "is_verified": True,
            },
        )
        if created:
            teacher_user.set_password("teacher12345")
            teacher_user.save()
            self.stdout.write(self.style.SUCCESS("Created Teacher: teacher / teacher12345"))
        else:
            self.stdout.write("Teacher 'teacher' already exists.")

        # 3. Student
        student_user, created = User.objects.get_or_create(
            username="student",
            defaults={
                "email": "student@cbtplatform.local",
                "first_name": "Aarav",
                "last_name": "Patel",
                "role": User.Roles.STUDENT,
                "target_exam": "JEE Advanced",
                "is_verified": True,
            },
        )
        if created:
            student_user.set_password("student12345")
            student_user.save()
            self.stdout.write(self.style.SUCCESS("Created Student: student / student12345"))
        else:
            self.stdout.write("Student 'student' already exists.")

        self.stdout.write(self.style.SUCCESS("Initial demo accounts verification completed."))

