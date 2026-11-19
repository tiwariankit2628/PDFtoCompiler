"""Background task for explicitly consented, paid PDF extraction."""
from celery import shared_task
from .cloud_extractor import CloudPDFQuestionExtractor
from .models import QuestionPaperUpload


@shared_task(ignore_result=True)
def extract_cloud_paper(paper_id):
    paper = QuestionPaperUpload.objects.filter(pk=paper_id).first()
    if paper is not None:
        return CloudPDFQuestionExtractor(paper).extract()
    return 0


@shared_task(ignore_result=True)
def extract_private_practice_paper(paper_id):
    """Claim once, process locally, and reverse only a definite empty failure.

    A broker can accept a job even if publishing reports an error. Claiming via
    a conditional update lets the upload view safely cancel an unclaimed job;
    a duplicate or cancelled job must never start a second extraction.
    """
    from django.utils import timezone
    from .extractor import PDFQuestionExtractor
    from apps.payments.packs import reverse_failed_pdf

    papers = QuestionPaperUpload.objects.filter(pk=paper_id, is_student_private=True)
    claimed = papers.filter(status=QuestionPaperUpload.ProcessingStatus.PENDING).update(
        status=QuestionPaperUpload.ProcessingStatus.PROCESSING, updated_at=timezone.now(),
    )
    if not claimed:
        # A previous worker may have recorded the failure but died before
        # issuing its reversal. The helper is idempotent and checks the paper.
        if papers.filter(status=QuestionPaperUpload.ProcessingStatus.FAILED).exists():
            reverse_failed_pdf(f"pdf:{paper_id}")
        return 0
    paper = papers.get()
    try:
        count = PDFQuestionExtractor(paper, claimed_private_job=True).extract()
    except Exception:
        # Unexpected task errors outside the extractor's own exception handler.
        # Never overwrite or refund a result that another operation has saved.
        if not paper.extracted_questions.exists() and not paper.figures.exists():
            papers.filter(status=QuestionPaperUpload.ProcessingStatus.PROCESSING).update(
                status=QuestionPaperUpload.ProcessingStatus.FAILED,
                extraction_log="Local extraction failed unexpectedly. Your source is saved for review.",
            )
            reverse_failed_pdf(f"pdf:{paper_id}")
        raise
    paper.refresh_from_db(fields=["status"])
    if paper.status == QuestionPaperUpload.ProcessingStatus.FAILED:
        reverse_failed_pdf(f"pdf:{paper.pk}")
    return count


@shared_task(ignore_result=True)
def extract_mineru_paper(paper_id):
    """Local MinerU extraction (runs the MinerU subprocess); nothing leaves the machine."""
    from .mineru_extractor import MinerUQuestionExtractor
    paper = QuestionPaperUpload.objects.filter(pk=paper_id).first()
    if paper is not None:
        return MinerUQuestionExtractor(paper).extract()
    return 0
