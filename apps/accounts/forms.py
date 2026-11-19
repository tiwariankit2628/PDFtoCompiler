from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordResetForm,
    SetPasswordForm,
    UserCreationForm,
)
from apps.exams.models import Category
from apps.core.exam_targets import ExamTrack
from .models import User


STUDENT_TARGET_CHOICES = [("", "Select your exam"), ("JEE", "JEE (Main and Advanced)"), ("NEET", "NEET")]


def _configure_student_target(form):
    form.fields["target_exam"] = forms.ChoiceField(
        choices=STUDENT_TARGET_CHOICES, required=True, label="Target exam",
        help_text="JEE includes both Main and Advanced. You can change this after finishing active tests.",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    if form.instance.pk:
        form.initial["target_exam"] = (form.instance.exam_track or "").upper()


def _validate_track_change(form, target):
    if not form.instance.pk or not target or target.lower() == form.instance.exam_track:
        return target
    from django.db.models import Q
    from django.utils import timezone
    from apps.exams.models import ExamAttempt
    if ExamAttempt.objects.filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
        student=form.instance, status=ExamAttempt.AttemptStatus.IN_PROGRESS,
    ).exists():
        raise forms.ValidationError("Finish your active test before changing your target exam. Your timer will continue normally.")
    return target


class BootstrapFormMixin:
    """Adds Bootstrap 5 classes to form widgets."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.update({"class": "form-check-input"})
            elif isinstance(field.widget, forms.FileInput):
                field.widget.attrs.update({"class": "form-control"})
            elif isinstance(field.widget, forms.SelectMultiple):
                field.widget.attrs.update({"class": "form-select", "size": "5"})
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.update({"class": "form-select"})
            else:
                existing_class = field.widget.attrs.get("class", "")
                field.widget.attrs["class"] = f"form-control {existing_class}".strip()


class UserLoginForm(BootstrapFormMixin, AuthenticationForm):
    """
    Login form allowing authentication with either username or email.
    """

    username = forms.CharField(
        label="Username or Email",
        widget=forms.TextInput(
            attrs={"placeholder": "Enter username or email address", "autocomplete": "username"}
        ),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"placeholder": "Enter your password", "autocomplete": "current-password"}),
    )

    def clean(self):
        username_or_email = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")

        if username_or_email and password:
            if "@" in username_or_email:
                try:
                    user_obj = User.objects.get(email__iexact=username_or_email)
                    username_or_email = user_obj.username
                except User.DoesNotExist:
                    pass

            self.user_cache = authenticate(
                self.request, username=username_or_email, password=password
            )
            if self.user_cache is None:
                raise forms.ValidationError(
                    "Invalid username/email or password. Please verify your credentials."
                )
            else:
                self.confirm_login_allowed(self.user_cache)

        return self.cleaned_data


class StudentRegistrationForm(BootstrapFormMixin, forms.ModelForm):
    """
    Student registration with clean validation and password confirmation.
    """

    first_name = forms.CharField(
        label="First Name",
        max_length=50,
        required=True,
        widget=forms.TextInput(attrs={"placeholder": "e.g. John"}),
    )
    last_name = forms.CharField(
        label="Last Name",
        max_length=50,
        required=True,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Doe"}),
    )
    username = forms.CharField(
        label="Username",
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={"placeholder": "Choose a unique username"}),
    )
    email = forms.EmailField(
        label="Email Address",
        required=True,
        widget=forms.EmailInput(attrs={"placeholder": "name@example.com"}),
    )
    target_exam = forms.ChoiceField(label="Target examination", choices=STUDENT_TARGET_CHOICES, required=True)
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"placeholder": "Minimum 8 characters"}),
        min_length=8,
    )
    confirm_password = forms.CharField(
        label="Confirm Password",
        widget=forms.PasswordInput(attrs={"placeholder": "Re-enter your password"}),
    )

    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "username",
            "email",
            "target_exam",
            "password",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, hint in {
            "first_name": "given-name", "last_name": "family-name", "username": "username",
            "email": "email", "password": "new-password", "confirm_password": "new-password",
        }.items():
            self.fields[name].widget.attrs["autocomplete"] = hint

    def clean_username(self):
        username = self.cleaned_data.get("username").strip().lower()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("This username is already taken.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")

        if password and confirm_password and password != confirm_password:
            self.add_error("confirm_password", "Passwords do not match.")

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        user.role = User.Roles.STUDENT
        user.exam_track = self.cleaned_data["target_exam"].lower()
        if commit:
            user.save()
        return user


class PublicRegistrationForm(StudentRegistrationForm):
    """Public self-registration with a server-validated Student/Teacher choice.

    Only STUDENT and TEACHER can ever be created here — admin, staff, superuser,
    and category assignment are never settable from public registration. A missing
    role defaults to STUDENT so older clients that post the legacy form keep
    working unchanged.
    """

    # Only these two roles may be self-selected. Anything else is rejected.
    PUBLIC_ROLES = {User.Roles.STUDENT, User.Roles.TEACHER}

    role = forms.ChoiceField(
        label="I am registering as",
        choices=[
            (User.Roles.STUDENT, "Student"),
            (User.Roles.TEACHER, "Teacher"),
        ],
        required=False,  # legacy clients may omit it
        initial=User.Roles.STUDENT,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Teachers may register without a track; students must choose one even
        # if a legacy client omits the role field (which defaults to STUDENT).
        self.fields["target_exam"].required = False

    def clean_role(self):
        role = (self.cleaned_data.get("role") or User.Roles.STUDENT)
        if role not in self.PUBLIC_ROLES:
            raise forms.ValidationError("Choose either Student or Teacher.")
        return role

    def clean(self):
        cleaned = super().clean()
        if (cleaned.get("role") or User.Roles.STUDENT) == User.Roles.STUDENT and not cleaned.get("target_exam"):
            self.add_error("target_exam", "Students must choose JEE or NEET.")
        return cleaned

    def save(self, commit=True):
        # Build the user without committing, then apply only the vetted role.
        user = super().save(commit=False)
        role = self.cleaned_data.get("role") or User.Roles.STUDENT
        if role not in self.PUBLIC_ROLES:
            role = User.Roles.STUDENT
        user.role = role
        if role != User.Roles.STUDENT:
            user.exam_track = ""
        # Hard guarantees: public registration never grants elevated access.
        user.is_staff = False
        user.is_superuser = False
        if commit:
            user.save()
        return user


class UserProfileForm(BootstrapFormMixin, forms.ModelForm):
    """Student targets are explicit; teachers keep their existing profile text."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # target_exam predates structured tracks and may contain valuable free
        # text.  The selection field is only a UI adapter for exam_track.
        self.original_target_exam = self.instance.target_exam
        if self.instance.is_student:
            _configure_student_target(self)

    def clean_target_exam(self):
        value = self.cleaned_data.get("target_exam", "")
        return _validate_track_change(self, value) if self.instance.is_student else value

    def save(self, commit=True):
        user = super().save(commit=False)
        if user.is_student:
            user.target_exam = self.original_target_exam
            user.exam_track = self.cleaned_data["target_exam"].lower()
        if commit:
            user.save()
            self.save_m2m()
        return user

    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "email",
            "phone",
            "institute_name",
            "target_exam",
            "bio",
            "avatar",
        ]
        widgets = {
            "bio": forms.Textarea(attrs={"rows": 3, "placeholder": "Brief bio or notes..."}),
        }

    def clean_email(self):
        email = self.cleaned_data.get("email").strip().lower()
        if (
            User.objects.filter(email__iexact=email)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError("An account with this email already exists.")
        return email


# ==============================================================================
# ADMINISTRATIVE MANAGEMENT FORMS (STUDENTS, TEACHERS, ADMINS)
# ==============================================================================

class StudentAdminCreateForm(BootstrapFormMixin, forms.ModelForm):
    """Admin form to create a new student candidate."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _configure_student_target(self)

    password = forms.CharField(
        label="Initial Password",
        widget=forms.PasswordInput(attrs={"placeholder": "Minimum 8 characters"}),
        min_length=8,
    )

    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "username",
            "email",
            "password",
            "phone",
            "target_exam",
            "institute_name",
            "is_active",
            "is_verified",
        ]

    def clean_username(self):
        username = self.cleaned_data.get("username").strip().lower()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Username is already taken.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Email is already registered.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        user.role = User.Roles.STUDENT
        user.exam_track = self.cleaned_data["target_exam"].lower()
        if commit:
            user.save()
        return user


class StudentAdminUpdateForm(BootstrapFormMixin, forms.ModelForm):
    """Admin form to edit existing student candidate."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.original_target_exam = self.instance.target_exam
        _configure_student_target(self)

    def clean_target_exam(self):
        return _validate_track_change(self, self.cleaned_data.get("target_exam", ""))

    def save(self, commit=True):
        user = super().save(commit=False)
        user.target_exam = self.original_target_exam
        user.exam_track = self.cleaned_data["target_exam"].lower()
        if commit:
            user.save()
            self.save_m2m()
        return user

    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "email",
            "phone",
            "target_exam",
            "institute_name",
            "bio",
            "is_active",
            "is_verified",
            "free_test_attempted",
        ]

    def clean_email(self):
        email = self.cleaned_data.get("email").strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Email is already used by another account.")
        return email


class TeacherAdminCreateForm(BootstrapFormMixin, forms.ModelForm):
    """Admin form to create a new Teacher / Content Manager."""

    password = forms.CharField(
        label="Initial Password",
        widget=forms.PasswordInput(attrs={"placeholder": "Minimum 8 characters"}),
        min_length=8,
    )
    assigned_categories = forms.ModelMultipleChoiceField(
        queryset=Category.objects.all(),
        required=False,
        label="Permitted Examination Categories",
        help_text="Select categories this teacher is authorized to author and manage.",
    )

    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "username",
            "email",
            "password",
            "phone",
            "institute_name",
            "assigned_categories",
            "is_active",
        ]

    def clean_username(self):
        username = self.cleaned_data.get("username").strip().lower()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Username is already taken.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Email is already registered.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        user.role = User.Roles.TEACHER
        user.is_staff = True
        user.is_verified = True
        if commit:
            user.save()
            self.save_m2m()
        return user


class TeacherAdminUpdateForm(BootstrapFormMixin, forms.ModelForm):
    """Admin form to edit existing Teacher / Content Manager."""

    assigned_categories = forms.ModelMultipleChoiceField(
        queryset=Category.objects.all(),
        required=False,
        label="Permitted Examination Categories",
        help_text="Select categories this teacher is authorized to author and manage.",
    )

    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "email",
            "phone",
            "institute_name",
            "bio",
            "assigned_categories",
            "is_active",
        ]

    def clean_email(self):
        email = self.cleaned_data.get("email").strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Email is already used by another account.")
        return email


class AdminUserCreateForm(BootstrapFormMixin, forms.ModelForm):
    """Super Admin form to create a new Administrator."""

    password = forms.CharField(
        label="Initial Password",
        widget=forms.PasswordInput(attrs={"placeholder": "Minimum 8 characters"}),
        min_length=8,
    )

    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "username",
            "email",
            "password",
            "phone",
            "is_active",
        ]

    def clean_username(self):
        username = self.cleaned_data.get("username").strip().lower()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Username is already taken.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Email is already registered.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        user.role = User.Roles.ADMIN
        user.is_staff = True
        user.is_verified = True
        if commit:
            user.save()
        return user


class AdminUserUpdateForm(BootstrapFormMixin, forms.ModelForm):
    """Super Admin form to edit an existing Administrator."""

    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "email",
            "phone",
            "is_active",
        ]

    def clean_email(self):
        email = self.cleaned_data.get("email").strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Email is already used by another account.")
        return email
