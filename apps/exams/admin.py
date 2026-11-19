from django.contrib import admin
from django.db.models import Q
from apps.core.admin_privacy import PublicDataAdminMixin
from .models import Category, Exam, ExamAttempt, ExamResponse, ExamSecurityLog, TestSeries, SeriesSlot


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    def has_delete_permission(self, request, obj=None):
        if obj and (obj.exams.filter(Q(is_private_practice=True) | Q(is_classroom_only=True)).exists()
                    or obj.uploaded_papers.filter(Q(is_student_private=True) | Q(is_classroom_private=True)).exists()
                    or obj.questions.filter(Q(is_private_practice=True) | Q(is_classroom_snapshot=True)).exists()):
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        # Django's bulk deletion preview can enumerate private related objects.
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    list_display = ("name", "slug", "display_order", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("display_order", "name")


@admin.register(Exam)
class ExamAdmin(PublicDataAdminMixin, admin.ModelAdmin):
    exclude = ("is_private_practice", "is_classroom_only")
    # Use the application's exam editor for target changes: it locks the exam,
    # validates its questions and protects attempts. A forged admin POST must
    # not bypass those checks.
    readonly_fields = ("target_type",)

    def get_queryset(self, request):
        return super().get_queryset(request).filter(is_private_practice=False, is_classroom_only=False, series_slot__isnull=True)

    list_display = ("title", "target_type", "category", "creator", "status", "is_free", "duration_minutes", "created_at")
    list_filter = ("target_type", "status", "is_free", "category")
    search_fields = ("title", "slug")
    prepopulated_fields = {"slug": ("title",)}


@admin.register(ExamAttempt)
class ExamAttemptAdmin(PublicDataAdminMixin, admin.ModelAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).filter(exam__is_private_practice=False, exam__is_classroom_only=False, exam__series_slot__isnull=True)

    list_display = ("student", "exam", "status", "score_obtained", "percentage", "is_passed", "started_at")
    list_filter = ("status", "is_passed")
    search_fields = ("student__username", "exam__title")


@admin.register(ExamResponse)
class ExamResponseAdmin(PublicDataAdminMixin, admin.ModelAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).filter(attempt__exam__is_private_practice=False, attempt__exam__is_classroom_only=False, attempt__exam__series_slot__isnull=True)

    list_display = ("attempt", "exam_question", "selected_option", "is_marked_for_review", "is_visited", "updated_at")
    list_filter = ("is_marked_for_review", "is_visited")


@admin.register(ExamSecurityLog)
class ExamSecurityLogAdmin(PublicDataAdminMixin, admin.ModelAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).filter(attempt__exam__is_private_practice=False, attempt__exam__is_classroom_only=False, attempt__exam__series_slot__isnull=True)

    list_display = ("attempt", "event_type", "details", "timestamp")
    list_filter = ("event_type", "timestamp")
    search_fields = ("attempt__student__username", "attempt__exam__title")



class SeriesSlotInline(admin.TabularInline):
    model = SeriesSlot
    extra = 0
    can_delete = False
    # Slots are published from reviewed private PDFs in the application. A
    # public-media exam cannot be made private just by assigning it here.
    readonly_fields = ("position", "exam")
    fields = ("position", "exam")

    def has_add_permission(self, request, obj=None):
        return False

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "exam":
            kwargs["queryset"] = Exam.objects.filter(is_private_practice=False, is_classroom_only=False)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(TestSeries)
class TestSeriesAdmin(admin.ModelAdmin):
    list_display = ("title", "track", "is_active")
    readonly_fields = ("track",)
    inlines = (SeriesSlotInline,)

    def save_formset(self, request, form, formset, change):
        # Coordinate assigning a paid series test with public/classroom starts.
        # Recheck after locking: validation alone can race a session creation.
        from django.db import connection
        from django.db.models import F
        instances = formset.save(commit=False)
        for instance in instances:
            if instance.exam_id:
                if connection.vendor == "sqlite":
                    Exam.objects.filter(pk=instance.exam_id).update(id=F("id"))
                Exam.objects.select_for_update().get(pk=instance.exam_id)
                instance.full_clean()
            instance.save()
        formset.save_m2m()

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        from django.contrib import messages
        from django.core.exceptions import ValidationError
        from django.shortcuts import redirect
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ValidationError as exc:
            messages.error(request, "Series was not saved: " + "; ".join(exc.messages))
            return redirect(request.path)

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser
    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser
    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser
    def has_add_permission(self, request):
        return False
    def has_delete_permission(self, request, obj=None):
        return False
