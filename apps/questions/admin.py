from django.contrib import admin
from apps.core.admin_privacy import PublicDataAdminMixin
from .models import ExtractedQuestion, Question, QuestionOption, QuestionPaperUpload


class QuestionOptionInline(PublicDataAdminMixin, admin.TabularInline):
    model = QuestionOption
    extra = 4


@admin.register(QuestionPaperUpload)
class QuestionPaperUploadAdmin(PublicDataAdminMixin, admin.ModelAdmin):
    exclude = ("is_student_private", "practice_exam", "private_pdf_file")

    def get_queryset(self, request):
        return super().get_queryset(request).filter(is_student_private=False)

    list_display = ("title", "category", "subject", "status", "questions_found", "questions_approved", "uploaded_by", "created_at")
    list_filter = ("status", "category", "ocr_used")
    search_fields = ("title", "subject", "target_exam")


@admin.register(Question)
class QuestionAdmin(PublicDataAdminMixin, admin.ModelAdmin):
    exclude = ("is_private_practice", "private_image")

    def get_queryset(self, request):
        return super().get_queryset(request).filter(is_private_practice=False)

    list_display = ("subject", "category", "difficulty", "language", "positive_marks", "created_at")
    list_filter = ("difficulty", "language", "category")
    search_fields = ("question_text", "subject", "topic")
    inlines = [QuestionOptionInline]


@admin.register(ExtractedQuestion)
class ExtractedQuestionAdmin(PublicDataAdminMixin, admin.ModelAdmin):
    exclude = ("private_image", "include_in_practice")

    def get_queryset(self, request):
        return super().get_queryset(request).filter(paper__is_student_private=False)

    list_display = ("paper", "question_number", "status", "correct_option", "is_flagged", "page_number")
    list_filter = ("status", "is_flagged")
    search_fields = ("question_text", "paper__title")
