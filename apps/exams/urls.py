from django.urls import path
from . import views
from . import views_candidate
from . import views_analytics

app_name = "exams"

urlpatterns = [
    # Candidate CBT Examination Interface
    path("catalog/", views_candidate.ExamCatalogView.as_view(), name="exam_catalog"),
    path("<slug:slug>/instructions/", views_candidate.ExamInstructionsView.as_view(), name="exam_instructions"),
    path("<slug:slug>/start/", views_candidate.ExamStartView.as_view(), name="exam_start"),
    path("session/<uuid:attempt_uuid>/", views_candidate.ExamSessionView.as_view(), name="exam_session"),
    path("session/<uuid:attempt_uuid>/save-answer/", views_candidate.ExamSaveAnswerView.as_view(), name="exam_save_answer"),
    path("session/<uuid:attempt_uuid>/sync/", views_candidate.ExamSyncStatusView.as_view(), name="exam_sync_status"),
    path("session/<uuid:attempt_uuid>/log-event/", views_candidate.ExamSecurityLogView.as_view(), name="exam_log_event"),
    path("session/<uuid:attempt_uuid>/submit/", views_candidate.ExamSubmitView.as_view(), name="exam_submit"),
    path("session/<uuid:attempt_uuid>/completed/", views_candidate.ExamCompletedView.as_view(), name="exam_completed"),

    # Result Evaluation & Performance Analytics (Phase 6)
    path("results/<uuid:attempt_uuid>/", views_analytics.StudentResultDetailView.as_view(), name="student_result_detail"),
    path("results/<uuid:attempt_uuid>/review/", views_analytics.StudentResultReviewView.as_view(), name="student_result_review"),
    path("results/<uuid:attempt_uuid>/pdf/", views_analytics.StudentResultPdfView.as_view(), name="student_result_pdf"),
    path("<slug:slug>/leaderboard/", views_analytics.ExamLeaderboardView.as_view(), name="exam_leaderboard"),
    path("<int:pk>/analytics/", views_analytics.TeacherExamAnalyticsView.as_view(), name="teacher_exam_analytics"),
    path("<int:pk>/export-results/", views_analytics.TeacherExportExamResultsCsvView.as_view(), name="teacher_export_results_csv"),

    # Category Management
    path("categories/", views.CategoryListView.as_view(), name="category_list"),
    path("categories/create/", views.CategoryCreateView.as_view(), name="category_create"),
    path("categories/<int:pk>/edit/", views.CategoryUpdateView.as_view(), name="category_edit"),
    path(
        "categories/<int:pk>/toggle-status/",
        views.CategoryToggleStatusView.as_view(),
        name="category_toggle_status",
    ),
    path(
        "categories/<int:pk>/delete/",
        views.CategoryDeleteView.as_view(),
        name="category_delete",
    ),

    # Examination Management
    path("", views.ExamListView.as_view(), name="exam_list"),
    path("create/", views.ExamCreateView.as_view(), name="exam_create"),
    path("<int:pk>/edit/", views.ExamUpdateView.as_view(), name="exam_edit"),
    path("<int:pk>/delete/", views.ExamDeleteView.as_view(), name="exam_delete"),
    path("<int:pk>/duplicate/", views.ExamDuplicateView.as_view(), name="exam_duplicate"),
    path("<int:pk>/toggle-publish/", views.ExamTogglePublishView.as_view(), name="exam_toggle_publish"),
    path("<int:pk>/preview/", views.ExamPreviewView.as_view(), name="exam_preview"),

    # Exam Question Builder
    path("<int:pk>/questions/", views.ExamQuestionManageView.as_view(), name="exam_questions"),
    path(
        "<int:pk>/questions/add-bulk/",
        views.ExamAddQuestionsBulkView.as_view(),
        name="exam_add_questions_bulk",
    ),
    path(
        "<int:pk>/questions/add-random/",
        views.ExamAddRandomQuestionsView.as_view(),
        name="exam_add_random_questions",
    ),
    path(
        "<int:pk>/questions/<int:question_pk>/remove/",
        views.ExamRemoveQuestionView.as_view(),
        name="exam_remove_question",
    ),
]

