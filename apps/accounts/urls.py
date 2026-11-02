from django.urls import path
from . import views, views_management

app_name = "accounts"

urlpatterns = [
    path("register/", views.UserRegisterView.as_view(), name="register"),
    path("login/", views.UserLoginView.as_view(), name="login"),
    path("logout/", views.UserLogoutView.as_view(), name="logout"),
    path("profile/", views.profile_view, name="profile"),
    path("verify-email/<uuid:token>/", views.verify_email_view, name="verify_email"),

    # Password reset flow
    path(
        "password-reset/",
        views.CustomPasswordResetView.as_view(),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        views.CustomPasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        views.CustomPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        views.CustomPasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),

    # Student Management
    path("students/", views_management.StudentListView.as_view(), name="student_list"),
    path("students/create/", views_management.StudentCreateView.as_view(), name="student_create"),
    path("students/<int:pk>/", views_management.StudentDetailView.as_view(), name="student_detail"),
    path("students/<int:pk>/edit/", views_management.StudentUpdateView.as_view(), name="student_edit"),
    path(
        "students/<int:pk>/toggle-status/",
        views_management.StudentToggleStatusView.as_view(),
        name="student_toggle_status",
    ),

    # Teacher Management
    path("teachers/", views_management.TeacherListView.as_view(), name="teacher_list"),
    path("teachers/create/", views_management.TeacherCreateView.as_view(), name="teacher_create"),
    path("teachers/<int:pk>/", views_management.TeacherDetailView.as_view(), name="teacher_detail"),
    path("teachers/<int:pk>/edit/", views_management.TeacherUpdateView.as_view(), name="teacher_edit"),
    path(
        "teachers/<int:pk>/toggle-status/",
        views_management.TeacherToggleStatusView.as_view(),
        name="teacher_toggle_status",
    ),

    # Administrator Management (Super Admin only)
    path("admins/", views_management.AdminUserListView.as_view(), name="admin_list"),
    path("admins/create/", views_management.AdminUserCreateView.as_view(), name="admin_create"),
    path("admins/<int:pk>/edit/", views_management.AdminUserUpdateView.as_view(), name="admin_edit"),
    path(
        "admins/<int:pk>/toggle-status/",
        views_management.AdminUserToggleStatusView.as_view(),
        name="admin_toggle_status",
    ),
    path(
        "admins/<int:pk>/delete/",
        views_management.AdminUserDeleteView.as_view(),
        name="admin_delete",
    ),
]
