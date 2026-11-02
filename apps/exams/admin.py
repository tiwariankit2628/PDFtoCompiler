from django.contrib import admin
from .models import Category, Exam, ExamAttempt, ExamResponse, ExamSecurityLog


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "display_order", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("display_order", "name")


@admin.register(Exam)
class ExamAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "creator", "status", "is_free", "duration_minutes", "created_at")
    list_filter = ("status", "is_free", "category")
    search_fields = ("title", "slug")
    prepopulated_fields = {"slug": ("title",)}


@admin.register(ExamAttempt)
class ExamAttemptAdmin(admin.ModelAdmin):
    list_display = ("student", "exam", "status", "score_obtained", "percentage", "is_passed", "started_at")
    list_filter = ("status", "is_passed")
    search_fields = ("student__username", "exam__title")


@admin.register(ExamResponse)
class ExamResponseAdmin(admin.ModelAdmin):
    list_display = ("attempt", "exam_question", "selected_option", "is_marked_for_review", "is_visited", "updated_at")
    list_filter = ("is_marked_for_review", "is_visited")


@admin.register(ExamSecurityLog)
class ExamSecurityLogAdmin(admin.ModelAdmin):
    list_display = ("attempt", "event_type", "details", "timestamp")
    list_filter = ("event_type", "timestamp")
    search_fields = ("attempt__student__username", "attempt__exam__title")

