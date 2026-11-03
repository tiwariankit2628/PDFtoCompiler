from django.urls import path
from . import views, figure_views, practice_views, practice_bank, answer_views

app_name = "questions"

urlpatterns = [
    # Owner-only student PDF practice workflow
    path("practice/", practice_views.PracticeListView.as_view(), name="practice_list"),
    path("practice/upload/", practice_views.PracticeUploadView.as_view(), name="practice_upload"),
    path("practice/papers/<int:pk>/settings/", practice_views.PracticeSettingsView.as_view(), name="practice_settings"),
    path("practice/papers/<int:pk>/pdf/", practice_views.PracticePdfView.as_view(), name="practice_pdf"),
    path("practice/papers/<int:pk>/delete/", practice_views.PracticeDeleteView.as_view(), name="practice_delete"),
    path("practice/papers/<int:pk>/figures/", practice_views.PracticeFigurePoolView.as_view(), name="practice_figure_pool"),
    path("practice/papers/<int:pk>/questions/add/", practice_views.PracticeQuestionAddView.as_view(), name="practice_question_add"),
    path("practice/questions/<int:pk>/ai/", answer_views.StudentSuggestionWorkspace.as_view(), name="practice_answer_suggestion"),
    path("practice/questions/<int:pk>/ai/request/", answer_views.StudentSuggestionRequest.as_view(), name="practice_answer_request"),
    path("practice/questions/<int:pk>/ai/decision/", answer_views.StudentSuggestionDecision.as_view(), name="practice_answer_decision"),
    path("practice/questions/<int:pk>/edit/", practice_views.PracticeQuestionEditView.as_view(), name="practice_question_edit"),
    path("practice/questions/<int:pk>/include/", practice_views.PracticeQuestionIncludeView.as_view(), name="practice_question_include"),
    path("practice/questions/<int:pk>/figures/", practice_views.PracticeFigureManageView.as_view(), name="practice_figures"),
    path("practice/questions/<int:pk>/source-region/<int:index>/", practice_views.PracticeSourceRegionView.as_view(), name="practice_source_region"),
    path("practice/questions/<int:pk>/source-page/<int:page_number>/", practice_views.PracticeSourcePageView.as_view(), name="practice_source_page"),
    path("practice/questions/<int:pk>/image/", practice_views.PracticeExtractedImageView.as_view(), name="practice_extracted_image"),
    path("practice/figures/<int:pk>/file/", practice_views.PracticeFigureFileView.as_view(), name="practice_figure_file"),
    path("practice/bank/", practice_bank.BankListView.as_view(), name="practice_bank"),
    path("practice/bank/create-test/", practice_bank.BankTestCreateView.as_view(), name="practice_bank_test"),
    path("practice/bank/<int:pk>/edit/", practice_bank.BankEditView.as_view(), name="practice_bank_edit"),
    path("practice/bank/<int:pk>/delete/", practice_bank.BankDeleteView.as_view(), name="practice_bank_delete"),
    path("practice/bank/<int:pk>/image/", practice_views.PracticeBankImageView.as_view(), name="practice_bank_image"),
    path("practice/bank/figures/<int:pk>/file/", practice_views.PracticeBankFigureView.as_view(), name="practice_bank_figure"),
    path("practice/history/", practice_views.PracticeHistoryView.as_view(), name="practice_history"),
    path("practice/results/<uuid:attempt_uuid>/", practice_views.PracticeResultView.as_view(), name="practice_result"),
    path("practice/results/<uuid:attempt_uuid>/report.pdf", practice_views.PracticeReportView.as_view(), name="practice_report"),

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

    path("papers/<int:pk>/figures/", figure_views.PaperFigurePoolView.as_view(), name="figure_pool"),
    path("papers/<int:pk>/questions/add/", figure_views.ManualQuestionCreateView.as_view(), name="extracted_add"),
    path("extracted/<int:pk>/figures/", figure_views.QuestionFigureManageView.as_view(), name="figure_manage"),
    path("extracted/<int:pk>/source-region/<int:index>/", figure_views.SourceRegionImageView.as_view(), name="source_region_image"),
    path("extracted/<int:pk>/source-page/<int:page_number>/", figure_views.SourcePageImageView.as_view(), name="source_page_image"),
    path("figures/<int:pk>/file/", figure_views.FigureImageView.as_view(), name="figure_file"),

    path("extracted/<int:pk>/ai/", answer_views.TeacherSuggestionWorkspace.as_view(), name="answer_suggestion"),
    path("extracted/<int:pk>/ai/request/", answer_views.TeacherSuggestionRequest.as_view(), name="answer_request"),
    path("extracted/<int:pk>/ai/decision/", answer_views.TeacherSuggestionDecision.as_view(), name="answer_decision"),

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
    path("bank/<int:pk>/figures/", figure_views.BankFigureManageView.as_view(), name="bank_figure_manage"),
    path("bank/<int:pk>/delete/", views.QuestionDeleteView.as_view(), name="question_delete"),
    path("bank/<int:pk>/preview/", views.QuestionPreviewView.as_view(), name="question_preview"),
]
