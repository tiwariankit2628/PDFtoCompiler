"""Link an uncertain Razorpay TEST order without activating access."""
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from apps.core.utils import log_audit
from apps.payments import gateway
from apps.payments.models import Checkout, PackCheckout
from apps.payments.services import lock_practice_user


class Command(BaseCommand):
    help = "Link a matching Razorpay TEST order to an initiating pack checkout; never grants credits."

    def add_arguments(self, parser):
        parser.add_argument("reference", help="Local pack checkout UUID")
        parser.add_argument("provider_order_id", help="Matching Razorpay test order_... ID")

    def handle(self, *args, **options):
        try:
            checkout = PackCheckout.objects.select_related("user").get(
                reference=options["reference"], status=PackCheckout.Status.INITIATING)
        except (PackCheckout.DoesNotExist, ValueError):
            raise CommandError("No unresolved pack checkout matches that reference.") from None
        provider_id = options["provider_order_id"]
        if not re.fullmatch(r"order_[A-Za-z0-9]+", provider_id):
            raise CommandError("Enter a valid Razorpay test order ID.")
        try:
            order = gateway.client().order.fetch(provider_id, timeout=15)
        except Exception:
            raise CommandError("Unable to verify the test order with Razorpay. No changes were made.") from None
        if (not isinstance(order, dict) or order.get("id") != provider_id
                or order.get("receipt") != str(checkout.reference)
                or order.get("amount") != checkout.amount_paise or order.get("currency") != checkout.currency):
            raise CommandError("Test order does not match the frozen checkout terms. No changes were made.")
        try:
            with transaction.atomic():
                lock_practice_user(checkout.user)
                checkout = PackCheckout.objects.select_for_update().get(pk=checkout.pk, status=PackCheckout.Status.INITIATING)
                if Checkout.objects.filter(provider_order_id=provider_id).exists():
                    raise CommandError("This order is linked to a legacy checkout. No changes were made.")
                checkout.provider_order_id = provider_id
                checkout.status = PackCheckout.Status.CREATED
                checkout.save(update_fields=["provider_order_id", "status", "updated_at"])
                log_audit(None, "PACK_TEST_CHECKOUT_RECONCILED", f"Test pack checkout {checkout.reference} linked; no credits granted.")
        except (PackCheckout.DoesNotExist, IntegrityError):
            raise CommandError("Checkout changed or order is already linked. No changes were made.") from None
        self.stdout.write(self.style.SUCCESS("Test order linked. No credits activated; complete checkout or replay the signed capture webhook."))
