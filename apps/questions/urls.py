from django.urls import path
from . import views

app_name = "questions"

urlpatterns = [
    # Question Papers
    path("papers/", views.QuestionPaperListView.as_view(), name="paper_list"),
    path("papers/upload/", views.QuestionPaperUploadView.as_view(), name="paper_upload"),
    path("papers/<int:pk>/review/", views.QuestionPaperReviewView.as_view(), name="paper_review"),
    path("papers/<int:pk>/pdf/", views.QuestionPaperDownloadView.as_view(), name="paper_download"),
    path(
        "papers/<int:pk>/bulk-approve/",
        views.QuestionPaperBulkApproveView.as_view(),
        name="paper_bulk_approve",
    ),
    path(
        "papers/<int:pk>/import-answer-key/",
        views.AnswerKeyImportView.as_view(),
        name="paper_import_answer_key",
    ),

    # Staging Question Editing & Actions
    path(
        "extracted/<int:pk>/edit/",
        views.ExtractedQuestionEditView.as_view(),
        name="extracted_edit",
    ),
    path(
        "extracted/<int:pk>/approve/",
        views.ExtractedQuestionApproveView.as_view(),
        name="extracted_approve",
    ),
    path(
        "extracted/<int:pk>/delete/",
        views.ExtractedQuestionDeleteView.as_view(),
        name="extracted_delete",
    ),
    path(
        "extracted/<int:pk>/toggle-flag/",
        views.ExtractedQuestionFlagToggleView.as_view(),
        name="extracted_toggle_flag",
    ),

    # Permanent Question Bank
    path("bank/", views.QuestionBankListView.as_view(), name="question_bank"),
    path("bank/create/", views.QuestionCreateView.as_view(), name="question_create"),
    path("bank/<int:pk>/edit/", views.QuestionUpdateView.as_view(), name="question_edit"),
    path("bank/<int:pk>/delete/", views.QuestionDeleteView.as_view(), name="question_delete"),
    path("bank/<int:pk>/preview/", views.QuestionPreviewView.as_view(), name="question_preview"),
]
