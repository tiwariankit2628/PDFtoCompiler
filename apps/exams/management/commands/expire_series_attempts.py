from django.core.management.base import BaseCommand
from apps.exams.tasks import expire_series_attempts


class Command(BaseCommand):
    help = "Submit expired series attempts (for cron or operational recovery)."

    def handle(self, *args, **options):
        count = expire_series_attempts()
        self.stdout.write(f"Timed out {count} series attempt(s).")
