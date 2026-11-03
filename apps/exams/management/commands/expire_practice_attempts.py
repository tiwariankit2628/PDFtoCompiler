from django.core.management.base import BaseCommand
from apps.exams.tasks import expire_private_practice_attempts


class Command(BaseCommand):
    help = "Submit expired private practice attempts (for cron or operational recovery)."

    def handle(self, *args, **options):
        count = expire_private_practice_attempts()
        self.stdout.write(f"Timed out {count} private practice attempt(s).")
