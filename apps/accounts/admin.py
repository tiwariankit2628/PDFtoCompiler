from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "role",
        "is_verified",
        "free_test_attempted",
        "is_staff",
        "date_joined",
    )
    list_filter = ("role", "is_verified", "free_test_attempted", "is_staff", "is_active")
    search_fields = ("username", "email", "first_name", "last_name", "phone", "target_exam")
    ordering = ("-date_joined",)

    fieldsets = BaseUserAdmin.fieldsets + (
        (
            "CBT Role & Permissions",
            {
                "fields": (
                    "role",
                    "is_verified",
                    "free_test_attempted",
                )
            },
        ),
        (
            "Candidate & Institutional Profile",
            {
                "fields": (
                    "phone",
                    "avatar",
                    "target_exam",
                    "institute_name",
                    "bio",
                    "verification_token",
                )
            },
        ),
    )

    readonly_fields = ("verification_token",)

