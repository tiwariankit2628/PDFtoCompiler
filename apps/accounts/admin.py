from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    def has_delete_permission(self, request, obj=None):
        if obj and (obj.uploaded_papers.filter(is_student_private=True).exists()
                    or obj.created_exams.filter(is_private_practice=True).exists()
                    or obj.exam_attempts.filter(exam__is_private_practice=True).exists()
                    or obj.practice_checkouts.exists()):
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        # A bulk-delete confirmation would otherwise reveal private related
        # objects and bypass per-user deletion protection.
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

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

