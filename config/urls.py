from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

admin.site.site_header = f"{getattr(settings, 'PLATFORM_NAME', 'TestForge CBT')} Administration"
admin.site.site_title = f"{getattr(settings, 'PLATFORM_NAME', 'TestForge CBT')} Portal"
admin.site.index_title = "Commercial CBT Platform Management"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.core.urls", namespace="core")),
    path("auth/", include("apps.accounts.urls", namespace="accounts")),
    path("exams/", include("apps.exams.urls", namespace="exams")),
    path("questions/", include("apps.questions.urls", namespace="questions")),
    path("payments/", include("apps.payments.urls", namespace="payments")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
