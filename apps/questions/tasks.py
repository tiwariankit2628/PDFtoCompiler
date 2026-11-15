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
    """Local extraction only; student PDFs are never sent to cloud providers."""
    from .extractor import PDFQuestionExtractor
    paper = QuestionPaperUpload.objects.filter(pk=paper_id, is_student_private=True).first()
    if paper is not None:
        return PDFQuestionExtractor(paper).extract()
    return 0


@shared_task(ignore_result=True)
def extract_mineru_paper(paper_id):
    """Local MinerU extraction (runs the MinerU subprocess); nothing leaves the machine."""
    from .mineru_extractor import MinerUQuestionExtractor
    paper = QuestionPaperUpload.objects.filter(pk=paper_id).first()
    if paper is not None:
        return MinerUQuestionExtractor(paper).extract()
    return 0
