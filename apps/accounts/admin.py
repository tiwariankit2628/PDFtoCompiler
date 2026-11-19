from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, connection, transaction
from django.http import Http404, HttpResponse
from django.contrib.admin.utils import unquote
from .models import User
from .record_safety import has_protected_account_records


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    def has_delete_permission(self, request, obj=None):
        if obj and has_protected_account_records(obj):
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        # A bulk-delete confirmation would otherwise reveal private related
        # objects and bypass per-user deletion protection.
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def delete_view(self, request, object_id, extra_context=None):
        # Django reads the deletion preview before writing its admin log. On
        # SQLite a concurrent start can make that read transaction too stale to
        # upgrade to a writer; the parent atomic block rolls back before this
        # retryable response is returned. Never turn lock contention into a 500.
        try:
            return super().delete_view(request, object_id, extra_context)
        except OperationalError as exc:
            if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                raise
            response = HttpResponse("This account is busy. Please retry.", status=503)
            response["Retry-After"] = "1"
            return response

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if request.method != "POST" or object_id is None:
            return super().changeform_view(request, object_id, form_url, extra_context)
        try:
            pk = int(unquote(object_id))
        except (TypeError, ValueError):
            return super().changeform_view(request, object_id, form_url, extra_context)
        try:
            with transaction.atomic():
                # Lock before the admin form reads its instance. Otherwise a
                # pending email resend/change can be overwritten by stale
                # verification fields even though save_model locks later.
                from apps.payments.services import lock_practice_user
                try:
                    lock_practice_user(User(pk=pk))
                except User.DoesNotExist:
                    raise Http404() from None
                return super().changeform_view(request, object_id, form_url, extra_context)
        except OperationalError as exc:
            if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                raise
            response = HttpResponse("This account is busy. Please retry.", status=503)
            response["Retry-After"] = "1"
            return response

    def delete_model(self, request, obj):
        # Recheck while holding the same account lock used by commercial starts
        # and publications; a record may have appeared since confirmation.
        from apps.payments.services import lock_practice_user
        locked = lock_practice_user(obj)
        if has_protected_account_records(locked):
            raise PermissionDenied("The account has protected assessment or commercial records.")
        super().delete_model(request, locked)

    def save_model(self, request, obj, form, change):
        email_changed = False
        if change and (obj.is_student or obj.is_teacher):
            from apps.payments.services import lock_practice_user
            from .verification import invalidate_changed_email
            original = lock_practice_user(obj)
            email_changed = invalidate_changed_email(obj, original.email)
        super().save_model(request, obj, form, change)
        if email_changed:
            from .verification import notify_admin_of_verification
            transaction.on_commit(lambda: notify_admin_of_verification(request, obj))

    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "role",
        "exam_track",
        "is_verified",
        "free_test_attempted",
        "is_staff",
        "date_joined",
    )
    list_filter = ("role", "exam_track", "is_verified", "free_test_attempted", "is_staff", "is_active")
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
                "description": "Student exam tracks are changed through the profile or student-management page, which protects active attempts. The legacy target label is retained separately.",
                "fields": (
                    "phone",
                    "avatar",
                    "exam_track",
                    "target_exam",
                    "institute_name",
                    "bio",
                    "verification_token",
                )
            },
        ),
    )

    readonly_fields = ("verification_token", "exam_track")

