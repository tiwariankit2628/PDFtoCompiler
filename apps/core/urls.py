from django.urls import path
from . import views, views_management

app_name = "core"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("about/", views.AboutView.as_view(), name="about"),
    path("contact/", views.ContactView.as_view(), name="contact"),

    # Role-based Dashboards
    path("dashboard/", views.DashboardRouterView.as_view(), name="dashboard_router"),
    path(
        "dashboard/super-admin/",
        views.SuperAdminDashboardView.as_view(),
        name="super_admin_dashboard",
    ),
    path(
        "dashboard/admin/",
        views.AdminDashboardView.as_view(),
        name="admin_dashboard",
    ),
    path(
        "dashboard/teacher/",
        views.TeacherDashboardView.as_view(),
        name="teacher_dashboard",
    ),
    path(
        "dashboard/student/",
        views.StudentDashboardView.as_view(),
        name="student_dashboard",
    ),

    # Super Admin Audit Logs
    path(
        "management/audit-logs/",
        views_management.AuditLogListView.as_view(),
        name="audit_log_list",
    ),
]
