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
    # Existing accounts are grandfathered; public registrations require a
    # verified address before starting new work. Active attempts remain usable.
    verification_required = models.BooleanField(default=False, editable=False)
    verification_sent_at = models.DateTimeField(null=True, blank=True, editable=False)
    verification_token = models.UUIDField(
        "Email Verification Token", default=uuid.uuid4, editable=False
    )
    avatar = models.ImageField(
        "Profile Picture", upload_to="avatars/%Y/%m/", blank=True, null=True
    )
    institute_name = models.CharField(
        "School / College / Institute", max_length=200, blank=True
    )
    # Retain the original free-text value for existing accounts. The new
    # classification is separate so an ambiguous old value is never guessed.
    target_exam = models.CharField("Target Examination", max_length=150, blank=True)
    from apps.core.exam_targets import ExamTrack
    exam_track = models.CharField("Exam track", max_length=12, choices=ExamTrack.choices, blank=True, db_index=True)
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
    def needs_email_verification(self):
        return self.verification_required and not self.is_verified

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


class GoogleIdentity(models.Model):
    """One explicitly connected Google subject per site account.

    Google's stable ``sub`` is the identifier, not the changeable email. No
    provider access token, refresh token, client secret or password is stored.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="google_identity")
    subject = models.CharField(max_length=255, unique=True)
    linked_at = models.DateTimeField(auto_now_add=True)


class GoogleOAuthState(models.Model):
    """Short-lived, atomically consumed anti-replay marker for one OAuth flow."""
    digest = models.CharField(max_length=64, primary_key=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
