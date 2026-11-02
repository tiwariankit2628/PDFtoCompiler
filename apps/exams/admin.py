from django.contrib import admin
from apps.core.admin_privacy import PublicDataAdminMixin
from .models import Category, Exam, ExamAttempt, ExamResponse, ExamSecurityLog


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    def has_delete_permission(self, request, obj=None):
        if obj and (obj.exams.filter(is_private_practice=True).exists()
                    or obj.uploaded_papers.filter(is_student_private=True).exists()
                    or obj.questions.filter(is_private_practice=True).exists()):
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
    exclude = ("is_private_practice",)

    def get_queryset(self, request):
        return super().get_queryset(request).filter(is_private_practice=False)

    list_display = ("title", "category", "creator", "status", "is_free", "duration_minutes", "created_at")
    list_filter = ("status", "is_free", "category")
    search_fields = ("title", "slug")
    prepopulated_fields = {"slug": ("title",)}


@admin.register(ExamAttempt)
class ExamAttemptAdmin(PublicDataAdminMixin, admin.ModelAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).filter(exam__is_private_practice=False)

    list_display = ("student", "exam", "status", "score_obtained", "percentage", "is_passed", "started_at")
    list_filter = ("status", "is_passed")
    search_fields = ("student__username", "exam__title")


@admin.register(ExamResponse)
class ExamResponseAdmin(PublicDataAdminMixin, admin.ModelAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).filter(attempt__exam__is_private_practice=False)

    list_display = ("attempt", "exam_question", "selected_option", "is_marked_for_review", "is_visited", "updated_at")
    list_filter = ("is_marked_for_review", "is_visited")


@admin.register(ExamSecurityLog)
class ExamSecurityLogAdmin(PublicDataAdminMixin, admin.ModelAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).filter(attempt__exam__is_private_practice=False)

    list_display = ("attempt", "event_type", "details", "timestamp")
    list_filter = ("event_type", "timestamp")
    search_fields = ("attempt__student__username", "attempt__exam__title")

