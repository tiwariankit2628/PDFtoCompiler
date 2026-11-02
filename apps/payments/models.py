"""Configurable practice plans and auditable, provider-verified billing records."""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class PracticePlan(models.Model):
    class Billing(models.TextChoices):
        FREE = "free", "Free"
        ONE_TIME = "one_time", "Prepaid access (one-time)"
        RECURRING = "recurring", "Recurring subscription"

    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    billing = models.CharField(max_length=12, choices=Billing.choices, default=Billing.FREE)
    is_default = models.BooleanField(default=False, help_text="The single fallback free plan for students without paid access.")
    is_available = models.BooleanField(default=True, help_text="Show this plan to students. Existing paid access remains valid.")
    price_paise = models.PositiveIntegerField(default=0, help_text="INR in paise; 100 paise = ₹1.")
    duration_days = models.PositiveIntegerField(default=30, help_text="Prepaid access duration; ignored for recurring subscriptions.")
    razorpay_plan_id = models.CharField(max_length=80, blank=True, help_text="Test-mode Razorpay plan ID for recurring billing. Its amount must match this plan.")
    recurring_cycles = models.PositiveIntegerField(default=12, help_text="Maximum billing cycles for a new recurring subscription.")
    billing_period = models.CharField(max_length=10, default="monthly", choices=[("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly"), ("yearly", "Yearly")])
    billing_interval = models.PositiveIntegerField(default=1, help_text="Must match the Razorpay plan interval.")
    monthly_pdf_uploads = models.PositiveIntegerField(null=True, blank=True, default=3, help_text="Blank = unlimited. Resets each calendar month in Asia/Kolkata.")
    monthly_test_creations = models.PositiveIntegerField(null=True, blank=True, default=5, help_text="Blank = unlimited.")
    monthly_attempts = models.PositiveIntegerField(null=True, blank=True, default=10, help_text="Blank = unlimited; resuming does not consume another attempt.")
    attempts_per_test = models.PositiveIntegerField(null=True, blank=True, default=3, help_text="Blank = unlimited. Includes all previous attempts.")
    allow_bank = models.BooleanField(default=True)
    allow_pdf_reports = models.BooleanField(default=True)
    allow_cloud_extraction = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["price_paise", "name"]
        constraints = [
            models.UniqueConstraint(fields=["is_default"], condition=Q(is_default=True), name="one_default_practice_plan"),
            models.CheckConstraint(condition=Q(is_default=False) | Q(billing="free", price_paise=0), name="default_practice_plan_is_free"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.pk and not self.is_default and PracticePlan.objects.filter(pk=self.pk, is_default=True).exists():
            errors["is_default"] = "The default free plan must remain configured. Edit it in place instead."
        if self.billing == self.Billing.FREE and self.price_paise != 0:
            errors["price_paise"] = "A free plan must have a zero price."
        if self.billing != self.Billing.FREE and not self.price_paise:
            errors["price_paise"] = "A paid plan needs a positive price."
        if self.is_default and (self.billing != self.Billing.FREE or self.price_paise):
            errors["is_default"] = "The default plan must be free."
        if self.billing == self.Billing.ONE_TIME and not self.duration_days:
            errors["duration_days"] = "Prepaid access requires a positive duration."
        if self.billing == self.Billing.RECURRING:
            if not self.razorpay_plan_id.startswith("plan_"):
                errors["razorpay_plan_id"] = "Enter a Razorpay test plan ID."
            if not self.recurring_cycles:
                errors["recurring_cycles"] = "At least one billing cycle is required."
            if not self.billing_interval:
                errors["billing_interval"] = "The billing interval must be positive."
        if errors:
            raise ValidationError(errors)

    @property
    def price_rupees(self):
        return f"{self.price_paise / 100:.2f}"

    def __str__(self):
        return self.name


class PracticeUsage(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="practice_usage")
    period = models.DateField(help_text="First day of the local calendar month.")
    pdf_uploads = models.PositiveIntegerField(default=0)
    test_creations = models.PositiveIntegerField(default=0)
    attempts = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "period"], name="unique_practice_usage_month")]


class Checkout(models.Model):
    class Status(models.TextChoices):
        INITIATING = "initiating", "Creating checkout"
        CREATED = "created", "Awaiting payment"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"
        CANCELLED = "cancelled", "Cancelled"

    reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="practice_checkouts")
    plan = models.ForeignKey(PracticePlan, on_delete=models.PROTECT, related_name="checkouts")
    billing = models.CharField(max_length=12, choices=PracticePlan.Billing.choices)
    amount_paise = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    duration_days = models.PositiveIntegerField(default=0)
    provider_plan_id = models.CharField(max_length=80, blank=True)
    billing_period = models.CharField(max_length=10, blank=True)
    billing_interval = models.PositiveIntegerField(default=1)
    recurring_cycles = models.PositiveIntegerField(default=0)
    provider_order_id = models.CharField(max_length=80, unique=True, null=True, blank=True)
    provider_subscription_id = models.CharField(max_length=80, unique=True, null=True, blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.INITIATING, db_index=True)
    cancellation_requested = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def price_rupees(self):
        return f"{self.amount_paise // 100}.{self.amount_paise % 100:02d}"

    def __str__(self):
        return f"{self.reference} · {self.get_status_display()}"


class Payment(models.Model):
    class Status(models.TextChoices):
        CAPTURED = "captured", "Captured"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"
        PARTIAL_REFUND = "partial_refund", "Partially refunded"

    checkout = models.ForeignKey(Checkout, on_delete=models.PROTECT, related_name="payments")
    provider_payment_id = models.CharField(max_length=80, unique=True)
    amount_paise = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CAPTURED)
    captured_at = models.DateTimeField(auto_now_add=True)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-captured_at"]


class Refund(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    provider_refund_id = models.CharField(max_length=80, unique=True)
    amount_paise = models.PositiveIntegerField()
    processed_at = models.DateTimeField(auto_now_add=True)


class PracticeEntitlement(models.Model):
    """Access exists only after a validated captured payment; expiry is explicit."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="practice_entitlements")
    plan = models.ForeignKey(PracticePlan, on_delete=models.PROTECT)
    checkout = models.OneToOneField(Checkout, on_delete=models.PROTECT, related_name="entitlement")
    starts_at = models.DateTimeField()
    valid_until = models.DateTimeField()
    is_revoked = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["user", "valid_until", "is_revoked"])]


class WebhookEvent(models.Model):
    event_id = models.CharField(max_length=140, unique=True)
    event_type = models.CharField(max_length=80)
    processed_at = models.DateTimeField(auto_now_add=True)
