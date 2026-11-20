from django.urls import path
from . import founder_views

app_name = "founder"

urlpatterns = [
    path("", founder_views.FounderOverviewView.as_view(), name="overview"),
    path("verify/", founder_views.FounderVerifyView.as_view(), name="verify"),
    path("lock/", founder_views.FounderLockView.as_view(), name="lock"),
    path("accounts/", founder_views.FounderAccountsView.as_view(), name="accounts"),
    path("accounts/<int:pk>/", founder_views.FounderAccountDetailView.as_view(), name="account_detail"),
    path("series/", founder_views.FounderSeriesView.as_view(), name="series"),
    path("payments/", founder_views.FounderPaymentsView.as_view(), name="payments"),
    path("activity/", founder_views.FounderActivityView.as_view(), name="activity"),
]
