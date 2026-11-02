from django.db.models.signals import post_migrate
from django.dispatch import receiver


@receiver(post_migrate, dispatch_uid="payments.ensure_default_practice_plan")
def ensure_default_practice_plan(sender, **kwargs):
    if sender.label != "payments":
        return
    from .models import PracticePlan
    if not PracticePlan.objects.filter(is_default=True).exists():
        PracticePlan.objects.get_or_create(slug="free-practice", defaults={
            "name": "Free practice", "is_default": True, "billing": "free",
            "description": "A private practice workspace to get started.",
        })
