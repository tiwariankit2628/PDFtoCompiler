from django.urls import path
from . import views

app_name = "payments"
urlpatterns = [
    path("plans/", views.PlansView.as_view(), name="plans"),
    path("checkout/<slug:slug>/start/", views.CreateCheckoutView.as_view(), name="checkout_start"),
    path("checkout/<uuid:reference>/", views.CheckoutView.as_view(), name="checkout"),
    path("checkout/<uuid:reference>/verify/", views.VerifyCheckoutView.as_view(), name="verify"),
    path("subscription/<uuid:reference>/cancel/", views.CancelSubscriptionView.as_view(), name="cancel_subscription"),
    path("razorpay/webhook/", views.razorpay_webhook, name="webhook"),
]
