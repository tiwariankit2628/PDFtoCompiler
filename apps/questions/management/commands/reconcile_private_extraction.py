"""Inspect and safely reconcile a stuck *private* local PDF extraction.

No source file or reviewed item is deleted. A claimed worker fences its final
write on the paper status, so cancelling an empty job also prevents a late
worker from committing extraction work after its credit was returned.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.core.utils import log_audit
from apps.payments.models import CreditUse
from apps.payments.packs import reverse_failed_pdf
from apps.payments.services import lock_practice_user
from apps.questions.models import Question, QuestionPaperUpload


class Command(BaseCommand):
    help = "Inspect a private PDF job, cancel an empty job, or restore preserved review work."

    def add_arguments(self, parser):
        parser.add_argument("paper_id", type=int)
        actions = parser.add_mutually_exclusive_group()
        actions.add_argument("--cancel-empty", action="store_true", help="Cancel an empty job and attempt a guarded credit reversal.")
        actions.add_argument("--restore-review", action="store_true", help="Make preserved staged items available for review without returning a credit.")

    def handle(self, *args, **options):
        try:
            original = QuestionPaperUpload.objects.select_related("uploaded_by").get(
                pk=options["paper_id"], is_student_private=True, is_classroom_private=False)
        except QuestionPaperUpload.DoesNotExist:
            raise CommandError("No private student paper matches that ID.") from None
        resource = f"pdf:{original.pk}"
        if not options["cancel_empty"] and not options["restore_review"]:
            use = CreditUse.objects.filter(resource_key=resource, kind="pdf").first()
            self.stdout.write(f"Paper {original.pk}: status={original.status}, updated={original.updated_at.isoformat()}, "
                              f"questions={original.extracted_questions.count()}, figures={original.figures.count()}, "
                              f"has_test={bool(original.practice_exam_id)}, "
                              f"credit={'returned' if use and use.reversed_at else 'charged' if use else 'no ledger entry'}.")
            self.stdout.write("Inspection only; no records or files changed.")
            return

        with transaction.atomic():
            lock_practice_user(original.uploaded_by)
            paper = QuestionPaperUpload.objects.select_for_update().get(pk=original.pk, is_student_private=True)
            questions = paper.extracted_questions.count()
            figures = paper.figures.count()
            derived = bool(paper.practice_exam_id or Question.objects.filter(source_paper=paper).exists())
            status = paper.ProcessingStatus
            if options["cancel_empty"]:
                if paper.status not in (status.PENDING, status.PROCESSING, status.FAILED):
                    raise CommandError("Only pending, processing or failed jobs can be cancelled; no changes were made.")
                if questions or figures or derived:
                    raise CommandError("Preserved extraction/review work exists; no credit was returned and no work was changed.")
                if paper.status != status.FAILED:
                    paper.status = status.FAILED
                    paper.extraction_log = (paper.extraction_log + "\n" if paper.extraction_log else "") + (
                        "Empty local extraction cancelled during reconciliation. The source PDF was preserved.")
                    paper.save(update_fields=["status", "extraction_log", "updated_at"])
                    log_audit(None, "PRIVATE_PDF_EXTRACTION_CANCELLED", f"Empty private extraction #{paper.pk} cancelled; source preserved.")
                returned = reverse_failed_pdf(resource)
                text = ("Credit returned." if returned else
                        "No credit was returned (already reversed, legacy debit or no eligible ledger entry); investigate if needed.")
                self.stdout.write(self.style.SUCCESS(f"Empty extraction stopped. Source preserved. {text}"))
            else:
                if paper.status != status.PROCESSING or not (questions or figures):
                    raise CommandError("Only a processing job with preserved staged work can be restored; no changes were made.")
                paper.status = status.EXTRACTED
                paper.questions_found = questions
                paper.extraction_log = (paper.extraction_log + "\n" if paper.extraction_log else "") + (
                    "Recovered existing extraction work after an interrupted job. Review it against the source PDF.")
                paper.save(update_fields=["status", "questions_found", "extraction_log", "updated_at"])
                log_audit(None, "PRIVATE_PDF_EXTRACTION_RECOVERED", f"Private extraction #{paper.pk} made available for review; no credit returned.")
                self.stdout.write(self.style.SUCCESS(f"Preserved {questions} questions and {figures} figures for review. No credit returned."))
