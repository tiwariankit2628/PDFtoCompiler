from django.core.management.base import BaseCommand
from apps.exams.tasks import expire_classroom_attempts


class Command(BaseCommand):
    help = "Submit expired classroom attempts (for cron or operational recovery)."

    def handle(self, *args, **options):
        count = expire_classroom_attempts()
        self.stdout.write(f"Timed out {count} classroom attempt(s).")
