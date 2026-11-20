import re

from django import template
from apps.payments.packs import PRODUCTS

register = template.Library()


@register.filter
def paise(value):
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return "—"
    sign = "−" if n < 0 else ""
    n = abs(n)
    return f"{sign}{n // 100:,}.{n % 100:02d}"


@register.filter
def recorded_amount(value, currency):
    """Format INR in rupees; do not assume every currency has 100 subunits."""
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return "Amount unavailable"
    if currency == "INR":
        return f"INR {paise(amount)}"
    return f"{currency or 'Unknown currency'} {amount:,} minor units"


@register.filter
def product_label(code):
    product = PRODUCTS.get(code)
    return product.name if product else (code or "Unknown product")


# Historical audit descriptions were not designed for this privacy boundary:
# some contain titles, file paths or scores. Only these deliberately generated
# founder descriptions can be rendered, and only with an exact match. Unknown
# or modified descriptions are withheld without altering the original logs.
_FIXED_AUDIT_DETAILS = {
    "FOUNDER_UNLOCK_FAILED": "Founder verification was not accepted.",
    "FOUNDER_UNLOCKED": "Founder workspace unlocked after password and authenticator verification.",
    "FOUNDER_LOCKED": "Founder workspace was locked in this session.",
    "FOUNDER_OVERVIEW_VIEWED": "Opened the founder overview.",
    "FOUNDER_SERIES_VIEWED": "Opened test series readiness and participation.",
    "FOUNDER_ACCOUNTS_VIEWED": "Opened the account directory.",
    "FOUNDER_ACCOUNT_SEARCH": "Searched the account directory; search terms are not recorded.",
    "FOUNDER_FINANCE_VIEWED": "Opened the payment and credit ledger.",
    "FOUNDER_ACTIVITY_VIEWED": "Opened the recorded activity and audit history.",
}
_NUMBERED_AUDIT_DETAILS = {
    "FOUNDER_ACCESS_GRANTED": (r"Server operator enrolled account #([0-9]{1,19}); its existing role was not changed\.",
                               "Server operator enrolled account #{}; its existing role was not changed."),
    "FOUNDER_ACCESS_REVOKED": (r"Server operator revoked founder access for account #([0-9]{1,19})\.",
                               "Server operator revoked founder access for account #{}."),
    "FOUNDER_ACCOUNT_VIEWED": (r"Opened support metadata for account #([0-9]{1,19})\.",
                               "Opened support metadata for account #{}."),
    "FOUNDER_FINANCE_VIEWED": (r"Opened payment and credit records for account #([0-9]{1,19})\.",
                               "Opened payment and credit records for account #{}."),
}


@register.filter
def safe_audit_detail(event):
    fixed = _FIXED_AUDIT_DETAILS.get(event.action)
    if fixed is not None and event.description == fixed:
        return fixed
    numbered = _NUMBERED_AUDIT_DETAILS.get(event.action)
    if numbered:
        match = re.fullmatch(numbered[0], event.description or "")
        if match:
            return numbered[1].format(match.group(1))
    return "Details withheld to protect private content."
