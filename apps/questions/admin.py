from django.contrib import admin
from apps.core.admin_privacy import PublicDataAdminMixin
from .models import ExtractedQuestion, Question, QuestionOption, QuestionPaperUpload


class QuestionOptionInline(PublicDataAdminMixin, admin.TabularInline):
    model = QuestionOption
    extra = 4


@admin.register(QuestionPaperUpload)
class QuestionPaperUploadAdmin(PublicDataAdminMixin, admin.ModelAdmin):
    exclude = ("is_student_private", "is_classroom_private", "practice_exam", "private_pdf_file")
    # Classification must use the owner-facing review flow, which validates
    # linked questions and active attempts under the appropriate locks.
    readonly_fields = ("target_type",)

    def get_queryset(self, request):
        return super().get_queryset(request).filter(is_student_private=False, is_classroom_private=False)

    list_display = ("title", "target_type", "category", "subject", "status", "questions_found", "questions_approved", "uploaded_by", "created_at")
    list_filter = ("target_type", "status", "category", "ocr_used")
    search_fields = ("title", "subject", "target_exam")


@admin.register(Question)
class QuestionAdmin(PublicDataAdminMixin, admin.ModelAdmin):
    exclude = ("is_private_practice", "is_classroom_snapshot", "private_image")
    # The application editor checks every linked exam and coordinates with
    # concurrent builder requests before changing a question's target.
    readonly_fields = ("target_type",)

    def get_queryset(self, request):
        return super().get_queryset(request).filter(is_private_practice=False, is_classroom_snapshot=False)

    list_display = ("subject", "target_type", "category", "difficulty", "language", "positive_marks", "created_at")
    list_filter = ("target_type", "difficulty", "language", "category")
    search_fields = ("question_text", "subject", "topic")
    inlines = [QuestionOptionInline]


@admin.register(ExtractedQuestion)
class ExtractedQuestionAdmin(PublicDataAdminMixin, admin.ModelAdmin):
    exclude = ("private_image", "include_in_practice")

    def get_queryset(self, request):
        return super().get_queryset(request).filter(paper__is_student_private=False, paper__is_classroom_private=False)

    list_display = ("paper", "question_number", "status", "correct_option", "is_flagged", "page_number")
    list_filter = ("status", "is_flagged")
    search_fields = ("question_text", "paper__title")
