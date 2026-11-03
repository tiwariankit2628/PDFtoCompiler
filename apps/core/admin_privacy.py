"""Keep private practice objects out of Django admin lists and relation widgets.

This is application-level access control, not protection against operators with
direct database, storage, or server access.
"""
from django.contrib import admin


class PublicDataAdminMixin:
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        from apps.exams.models import Exam, ExamAttempt, ExamQuestion
        from apps.questions.models import Question, QuestionOption, QuestionPaperUpload

        model = db_field.remote_field.model
        if model is Exam:
            kwargs["queryset"] = Exam.objects.filter(is_private_practice=False)
        elif model is ExamAttempt:
            kwargs["queryset"] = ExamAttempt.objects.filter(exam__is_private_practice=False)
        elif model is ExamQuestion:
            kwargs["queryset"] = ExamQuestion.objects.filter(exam__is_private_practice=False, question__is_private_practice=False)
        elif model is Question:
            kwargs["queryset"] = Question.objects.filter(is_private_practice=False)
        elif model is QuestionOption:
            kwargs["queryset"] = QuestionOption.objects.filter(question__is_private_practice=False)
        elif model is QuestionPaperUpload:
            kwargs["queryset"] = QuestionPaperUpload.objects.filter(is_student_private=False)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
