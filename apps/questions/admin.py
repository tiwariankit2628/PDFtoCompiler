from django.contrib import admin
from .models import ExtractedQuestion, Question, QuestionOption, QuestionPaperUpload


class QuestionOptionInline(admin.TabularInline):
    model = QuestionOption
    extra = 4


@admin.register(QuestionPaperUpload)
class QuestionPaperUploadAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "subject", "status", "questions_found", "questions_approved", "uploaded_by", "created_at")
    list_filter = ("status", "category", "ocr_used")
    search_fields = ("title", "subject", "target_exam")


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("subject", "category", "difficulty", "language", "positive_marks", "created_at")
    list_filter = ("difficulty", "language", "category")
    search_fields = ("question_text", "subject", "topic")
    inlines = [QuestionOptionInline]


@admin.register(ExtractedQuestion)
class ExtractedQuestionAdmin(admin.ModelAdmin):
    list_display = ("paper", "question_number", "status", "correct_option", "is_flagged", "page_number")
    list_filter = ("status", "is_flagged")
    search_fields = ("question_text", "paper__title")
