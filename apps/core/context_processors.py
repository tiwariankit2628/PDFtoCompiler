from datetime import datetime
from django.conf import settings


def platform_context(request):
    """
    Exposes platform brand configuration and utility context
    to all rendered templates.
    """
    return {
        "PLATFORM_NAME": getattr(settings, "PLATFORM_NAME", "TestForge CBT"),
        "PLATFORM_TAGLINE": getattr(
            settings,
            "PLATFORM_TAGLINE",
            "Commercial-Grade Online Examination & Assessment Engine",
        ),
        "PLATFORM_SUPPORT_EMAIL": getattr(
            settings, "PLATFORM_SUPPORT_EMAIL", "support@testforge.local"
        ),
        "CURRENT_YEAR": datetime.now().year,
    }

