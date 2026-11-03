"""Link an uncertain provider create response after operator investigation.

This command never grants access. The operator must still replay the original
signed webhook or allow the customer to complete server-verified checkout.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from apps.core.utils import log_audit
from apps.payments import gateway
from apps.payments.models import Checkout, PracticePlan


class Command(BaseCommand):
    help = "Safely link a Razorpay test order/subscription to an unresolved checkout; never activates a plan."

    def add_arguments(self, parser):
        parser.add_argument("reference", help="Local checkout UUID")
        parser.add_argument("provider_id", help="Razorpay test order_... or sub_... ID")

    def handle(self, *args, **options):
        try:
            checkout = Checkout.objects.select_related("plan").get(reference=options["reference"], status=Checkout.Status.INITIATING)
        except (Checkout.DoesNotExist, ValueError):
            raise CommandError("No unresolved checkout matches that reference.") from None
        provider_id = options["provider_id"]
        try:
            api = gateway.client()
            if checkout.billing == PracticePlan.Billing.ONE_TIME and provider_id.startswith("order_"):
                entity = api.order.fetch(provider_id, timeout=15)
                valid = (isinstance(entity, dict) and entity.get("id") == provider_id
                         and entity.get("receipt") == str(checkout.reference)
                         and entity.get("amount") == checkout.amount_paise
                         and entity.get("currency") == checkout.currency)
                field = "provider_order_id"
            elif checkout.billing == PracticePlan.Billing.RECURRING and provider_id.startswith("sub_"):
                entity = api.subscription.fetch(provider_id, timeout=15)
                notes = entity.get("notes", {}) if isinstance(entity, dict) else {}
                valid = (isinstance(entity, dict) and entity.get("id") == provider_id
                         and entity.get("plan_id") == checkout.provider_plan_id
                         and isinstance(notes, dict) and notes.get("checkout_reference") == str(checkout.reference))
                field = "provider_subscription_id"
            else:
                raise CommandError("Provider ID does not match checkout type.")
        except CommandError:
            raise
        except Exception:
            raise CommandError("Unable to verify the provider record. No changes were made.") from None
        if not valid:
            raise CommandError("Provider record does not match the frozen checkout terms. No changes were made.")
        try:
            with transaction.atomic():
                checkout = Checkout.objects.select_for_update().get(pk=checkout.pk, status=Checkout.Status.INITIATING)
                setattr(checkout, field, provider_id)
                checkout.status = Checkout.Status.CREATED
                checkout.save(update_fields=[field, "status", "updated_at"])
                log_audit(None, "PRACTICE_CHECKOUT_RECONCILED", f"Test checkout {checkout.reference} linked; no access granted.")
        except (Checkout.DoesNotExist, IntegrityError):
            raise CommandError("Checkout changed or provider ID is already linked. No changes were made.") from None
        self.stdout.write(self.style.SUCCESS("Checkout linked. No entitlement was activated; replay the verified webhook if a payment was already captured."))
