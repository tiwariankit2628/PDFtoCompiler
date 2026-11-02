from django.contrib import admin

from .models import Checkout, Payment, PracticeEntitlement, PracticePlan, PracticeUsage, Refund, WebhookEvent


class BillingAdminOnly:
    def has_module_permission(self, request):
        return bool(request.user.is_active and request.user.is_superuser)
    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_active and request.user.is_superuser)
    def has_change_permission(self, request, obj=None):
        return bool(request.user.is_active and request.user.is_superuser)
    def has_add_permission(self, request):
        return bool(request.user.is_active and request.user.is_superuser)
    def has_delete_permission(self, request, obj=None):
        return bool(request.user.is_active and request.user.is_superuser)


@admin.register(PracticePlan)
class PracticePlanAdmin(BillingAdminOnly, admin.ModelAdmin):
    list_display = ("name", "billing", "price_paise", "is_default", "is_available", "monthly_pdf_uploads", "monthly_test_creations", "monthly_attempts", "attempts_per_test")
    list_filter = ("billing", "is_default", "is_available")
    prepopulated_fields = {"slug": ("name",)}

    def has_delete_permission(self, request, obj=None):
        if obj and obj.is_default:
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


class ReadOnlyBillingAdmin(BillingAdminOnly, admin.ModelAdmin):
    def has_add_permission(self, request):
        return False
    def has_change_permission(self, request, obj=None):
        return False
    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


@admin.register(Checkout)
class CheckoutAdmin(ReadOnlyBillingAdmin):
    list_display = ("reference", "user", "plan", "billing", "amount_paise", "status", "created_at")
    list_filter = ("status", "billing")
    search_fields = ("reference", "user__username", "provider_order_id", "provider_subscription_id")


@admin.register(Payment)
class PaymentAdmin(ReadOnlyBillingAdmin):
    list_display = ("provider_payment_id", "checkout", "amount_paise", "status", "captured_at")


@admin.register(PracticeEntitlement)
class EntitlementAdmin(ReadOnlyBillingAdmin):
    list_display = ("user", "plan", "starts_at", "valid_until", "is_revoked")


@admin.register(PracticeUsage)
class UsageAdmin(ReadOnlyBillingAdmin):
    list_display = ("user", "period", "pdf_uploads", "test_creations", "attempts")


@admin.register(Refund)
class RefundAdmin(ReadOnlyBillingAdmin):
    list_display = ("provider_refund_id", "payment", "amount_paise", "processed_at")


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnlyBillingAdmin):
    list_display = ("event_id", "event_type", "processed_at")
