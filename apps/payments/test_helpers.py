"""Synthetic sandbox grants for application tests, never real provider payments."""
from .models import CreditGrant, PackCheckout
from .packs import PRODUCTS


def sandbox_grant(user, product_code, *, remaining=None):
    product = PRODUCTS[product_code]
    checkout = PackCheckout.objects.create(
        user=user, product=product_code, amount_paise=product.amount_paise,
        units=product.units, currency="INR", status=PackCheckout.Status.PAID,
    )
    return CreditGrant.objects.create(
        checkout=checkout, user=user, product=product_code, units=product.units,
        remaining=product.units if remaining is None else remaining, is_test=True,
    )
