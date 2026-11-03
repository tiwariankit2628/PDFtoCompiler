from django.db import migrations


def create_default(apps, schema_editor):
    Plan = apps.get_model("payments", "PracticePlan")
    if not Plan.objects.filter(is_default=True).exists():
        Plan.objects.get_or_create(slug="free-practice", defaults={
            "name": "Free practice", "billing": "free", "is_default": True,
            "description": "A private practice workspace to get started.",
            "monthly_pdf_uploads": 3, "monthly_test_creations": 5,
            "monthly_attempts": 10, "attempts_per_test": 3,
            "allow_bank": True, "allow_pdf_reports": True, "allow_cloud_extraction": True,
        })


class Migration(migrations.Migration):
    dependencies = [("payments", "0001_initial")]
    operations = [migrations.RunPython(create_default, migrations.RunPython.noop)]
