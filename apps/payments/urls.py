from django.urls import path
from . import views, pack_views

app_name = "payments"
urlpatterns = [
    path("plans/", views.PlansView.as_view(), name="plans"),
    path("packs/<slug:product_code>/start/", pack_views.StartPackCheckout.as_view(), name="pack_start"),
    path("packs/checkout/<uuid:reference>/", pack_views.PackCheckoutView.as_view(), name="pack_checkout"),
    path("packs/checkout/<uuid:reference>/verify/", pack_views.VerifyPackCheckout.as_view(), name="pack_verify"),
    path("checkout/<slug:slug>/start/", views.CreateCheckoutView.as_view(), name="checkout_start"),
    path("checkout/<uuid:reference>/", views.CheckoutView.as_view(), name="checkout"),
    path("checkout/<uuid:reference>/verify/", views.VerifyCheckoutView.as_view(), name="verify"),
    path("subscription/<uuid:reference>/cancel/", views.CancelSubscriptionView.as_view(), name="cancel_subscription"),
    path("razorpay/webhook/", views.razorpay_webhook, name="webhook"),
]
