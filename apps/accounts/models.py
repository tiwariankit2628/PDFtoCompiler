import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    Custom user model supporting multi-role commercial CBT examination workflows.
    Roles:
      - SUPER_ADMIN: System maintenance and global governance.
      - ADMIN: Exam creation, user management, subscriptions, audit.
      - TEACHER: Question management, paper uploads, content management.
      - STUDENT: Exam attempts, practice, result analysis.
    """

    class Roles(models.TextChoices):
        SUPER_ADMIN = "SUPER_ADMIN", "Super Administrator"
        ADMIN = "ADMIN", "Administrator"
        TEACHER = "TEACHER", "Teacher / Content Manager"
        STUDENT = "STUDENT", "Student"

    email = models.EmailField(
        "Email Address",
        unique=True,
        error_messages={
            "unique": "A user with this email address already exists.",
        },
    )
    role = models.CharField(
        "User Role",
        max_length=20,
        choices=Roles.choices,
        default=Roles.STUDENT,
        db_index=True,
    )
    phone = models.CharField("Phone Number", max_length=20, blank=True)
    is_verified = models.BooleanField("Email Verified", default=False)
    verification_token = models.UUIDField(
        "Email Verification Token", default=uuid.uuid4, editable=False
    )
    avatar = models.ImageField(
        "Profile Picture", upload_to="avatars/%Y/%m/", blank=True, null=True
    )
    institute_name = models.CharField(
        "School / College / Institute", max_length=200, blank=True
    )
    target_exam = models.CharField(
        "Target Examination",
        max_length=150,
        blank=True,
        help_text="E.g., JEE Advanced, NEET-UG, UPSC CSE, SSC CGL, GATE, etc.",
    )
    bio = models.TextField("Bio / Notes", blank=True, max_length=500)

    # Examination Eligibility / Freemium tracking
    free_test_attempted = models.BooleanField(
        "First Free Examination Used",
        default=False,
        help_text="Tracks freemium trial exam eligibility.",
    )

    # Permitted categories for Teachers
    assigned_categories = models.ManyToManyField(
        "exams.Category",
        blank=True,
        related_name="assigned_teachers",
        verbose_name="Permitted Categories",
        help_text="Categories this teacher is authorized to author and manage content for.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    REQUIRED_FIELDS = ["email"]

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["-date_joined"]

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"

    @property
    def is_student(self):
        return self.role == self.Roles.STUDENT

    @property
    def is_teacher(self):
        return self.role == self.Roles.TEACHER

    @property
    def is_admin_user(self):
        return self.role == self.Roles.ADMIN

    @property
    def is_super_admin(self):
        return self.role == self.Roles.SUPER_ADMIN or self.is_superuser

    @property
    def is_admin_role(self):
        return self.role in (self.Roles.ADMIN, self.Roles.SUPER_ADMIN) or self.is_superuser

    def save(self, *args, **kwargs):
        # Auto-synchronize superuser status with role
        if self.is_superuser and self.role != self.Roles.SUPER_ADMIN:
            self.role = self.Roles.SUPER_ADMIN
        super().save(*args, **kwargs)
