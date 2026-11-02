from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordResetForm,
    SetPasswordForm,
    UserCreationForm,
)
from apps.exams.models import Category
from .models import User


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
            attrs={"placeholder": "Enter username or email address", "autofocus": True}
        ),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"placeholder": "Enter your password"}),
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
    target_exam = forms.CharField(
        label="Target Examination",
        max_length=150,
        required=False,
        widget=forms.TextInput(
            attrs={"placeholder": "e.g. JEE Advanced, NEET, UPSC, SSC, GATE"}
        ),
    )
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
        if commit:
            user.save()
        return user


class UserProfileForm(BootstrapFormMixin, forms.ModelForm):
    """
    Profile editing form for users.
    """

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
        if commit:
            user.save()
        return user


class StudentAdminUpdateForm(BootstrapFormMixin, forms.ModelForm):
    """Admin form to edit existing student candidate."""

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
