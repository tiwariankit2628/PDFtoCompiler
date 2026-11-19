"""Private-PDF billing, duplicate delivery and failure reconciliation safety."""
import io
import os
import tempfile
from unittest.mock import patch

import pymupdf as fitz
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.payments.models import CreditUse
from apps.payments.packs import reverse_failed_pdf
from apps.payments.test_helpers import sandbox_grant
from .extractor import PDFQuestionExtractor
from .models import ExtractedQuestion, QuestionPaperUpload
from .tasks import extract_private_practice_paper
from .test_pdf_layout import make_two_column_pdf


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   ALLOW_TEST_PAYMENT_CREDITS=True, CELERY_TASK_ALWAYS_EAGER=True)
class PrivatePdfCreditSafetyTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        settings = override_settings(MEDIA_ROOT=os.path.join(self.root.name, "public"),
            STATIC_ROOT=os.path.join(self.root.name, "static"),
            PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private"))
        settings.enable()
        self.addCleanup(settings.disable)
        self.student = User.objects.create_user("credit-pdf", email="credit-pdf@example.test", role="STUDENT", exam_track="jee")
        self.client.force_login(self.student)

    @staticmethod
    def blank_pdf():
        doc = fitz.open()
        doc.new_page()
        result = doc.tobytes()
        doc.close()
        return result

    def upload(self, data=None, *, title="Private paper"):
        return self.client.post(reverse("questions:practice_upload"), {
            "title": title, "subject": "Physics", "target_type": "jee",
            "pdf_file": SimpleUploadedFile("fixture.pdf", data if data is not None else make_two_column_pdf(),
                                           content_type="application/pdf"),
        })

    def paper(self):
        return QuestionPaperUpload.objects.filter(uploaded_by=self.student, is_student_private=True).latest("id")

    def use(self, paper):
        return CreditUse.objects.get(user=self.student, resource_key=f"pdf:{paper.pk}")

    def test_empty_failure_returns_free_credit_once_but_preserves_the_source(self):
        self.upload(self.blank_pdf())
        paper = self.paper()
        self.assertEqual(paper.status, "failed")
        self.assertTrue(os.path.isfile(paper.private_pdf_file.path))
        self.assertFalse(paper.extracted_questions.exists())
        self.assertIsNotNone(self.use(paper).reversed_at)
        self.assertFalse(reverse_failed_pdf(f"pdf:{paper.pk}"))
        self.assertEqual(extract_private_practice_paper(paper.pk), 0)
        self.upload(title="Second valid paper")
        valid = self.paper()
        self.assertEqual(valid.status, "extracted")
        self.assertGreater(valid.extracted_questions.count(), 0)
        self.assertTrue(self.use(valid).is_free)
        self.assertIsNone(self.use(valid).reversed_at)

    def test_duplicate_task_never_overwrites_success_or_refunds_it(self):
        self.upload()
        paper = self.paper()
        ids = list(paper.extracted_questions.values_list("pk", flat=True))
        original_log = paper.extraction_log
        self.assertEqual(extract_private_practice_paper(paper.pk), 0)
        paper.refresh_from_db()
        self.assertEqual(paper.status, "extracted")
        self.assertEqual(list(paper.extracted_questions.values_list("pk", flat=True)), ids)
        self.assertEqual(paper.extraction_log, original_log)
        self.assertIsNone(self.use(paper).reversed_at)
        self.assertFalse(reverse_failed_pdf(f"pdf:{paper.pk}"))

    def test_paid_unexpected_failure_is_returned_once_and_message_is_truthful(self):
        self.upload()  # spend the free extraction on real staged work
        grant = sandbox_grant(self.student, "pdf")
        with patch("apps.questions.extractor.PDFQuestionExtractor.extract", side_effect=RuntimeError("worker failed")):
            response = self.upload(title="Paid failure")
        paper = self.paper()
        self.assertEqual(paper.status, "failed")
        self.assertIsNotNone(self.use(paper).reversed_at)
        grant.refresh_from_db()
        self.assertEqual(grant.remaining, 20)
        self.assertContains(self.client.get(response.url), "credit was returned")
        self.assertEqual(extract_private_practice_paper(paper.pk), 0)
        self.assertFalse(reverse_failed_pdf(f"pdf:{paper.pk}"))
        grant.refresh_from_db()
        self.assertEqual(grant.remaining, 20)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_ambiguous_publish_cancelled_before_claim_blocks_late_delivery(self):
        with patch("apps.questions.practice_views.extract_private_practice_paper.delay", side_effect=TimeoutError("ambiguous broker")):
            self.upload()
        paper = self.paper()
        self.assertEqual(paper.status, "failed")
        self.assertIsNotNone(self.use(paper).reversed_at)
        self.assertEqual(extract_private_practice_paper(paper.pk), 0)
        self.assertFalse(paper.extracted_questions.exists())
        self.assertTrue(os.path.isfile(paper.private_pdf_file.path))

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_ambiguous_publish_after_claim_does_not_return_credit(self):
        def claimed_then_timeout(paper_id):
            QuestionPaperUpload.objects.filter(pk=paper_id, status="pending").update(status="processing")
            raise TimeoutError("worker may be running")
        with patch("apps.questions.practice_views.extract_private_practice_paper.delay", side_effect=claimed_then_timeout):
            response = self.upload()
        paper = self.paper()
        self.assertEqual(paper.status, "processing")
        self.assertIsNone(self.use(paper).reversed_at)
        self.assertFalse(reverse_failed_pdf(f"pdf:{paper.pk}"))
        self.assertContains(self.client.get(response.url), "queue response was uncertain")
        # Simulate the original claimed worker continuing normally.
        self.assertGreater(PDFQuestionExtractor(paper, claimed_private_job=True).extract(), 0)
        paper.refresh_from_db()
        self.assertEqual(paper.status, "extracted")
        self.assertIsNone(self.use(paper).reversed_at)

    def test_reconciliation_fences_a_worker_that_continues_after_cancellation(self):
        from .pdf_layout import DigitalQuestionLayout
        original = DigitalQuestionLayout.extract

        def cancel_after_parse(layout):
            items = original(layout)
            self.assertTrue(items)
            call_command("reconcile_private_extraction", layout.paper_id_for_test, "--cancel-empty", stdout=io.StringIO())
            return items

        # The worker has already claimed the paper but has not committed its
        # parsed questions. Reconciliation wins the paper lock at this point.
        def cancel_after_parse_with_id(layout):
            layout.paper_id_for_test = self.paper().pk
            return cancel_after_parse(layout)

        with patch.object(DigitalQuestionLayout, "extract", cancel_after_parse_with_id):
            self.upload()
        paper = self.paper()
        paper.refresh_from_db()
        self.assertEqual(paper.status, "failed")
        self.assertFalse(paper.extracted_questions.exists())
        self.assertFalse(paper.figures.exists())
        self.assertIsNotNone(self.use(paper).reversed_at)
        self.assertIn("cancelled during reconciliation", paper.extraction_log)
        self.assertEqual(extract_private_practice_paper(paper.pk), 0)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_interrupted_worker_work_is_recovered_not_refunded(self):
        with patch("apps.questions.practice_views.extract_private_practice_paper.delay"):
            self.upload()
        paper = self.paper()
        paper.status = "processing"
        paper.save(update_fields=["status"])
        q = ExtractedQuestion.objects.create(paper=paper, question_number=1, question_text="Preserved work")
        with self.assertRaises(CommandError):
            call_command("reconcile_private_extraction", paper.pk, "--cancel-empty", stdout=io.StringIO())
        self.assertIsNone(self.use(paper).reversed_at)
        out = io.StringIO()
        call_command("reconcile_private_extraction", paper.pk, "--restore-review", stdout=out)
        paper.refresh_from_db()
        self.assertEqual((paper.status, paper.questions_found), ("extracted", 1))
        self.assertEqual(paper.extracted_questions.get().pk, q.pk)
        self.assertTrue(os.path.isfile(paper.private_pdf_file.path))
        self.assertIsNone(self.use(paper).reversed_at)
        self.assertIn("No credit returned", out.getvalue())
        self.assertFalse(reverse_failed_pdf(f"pdf:{paper.pk}"))

    def test_database_rollback_removes_only_the_orphan_upload_and_debit(self):
        original_save = QuestionPaperUpload.save
        def fail_after_file_saved(paper, *args, **kwargs):
            original_save(paper, *args, **kwargs)
            raise RuntimeError("simulate DB error after file write")
        with patch.object(QuestionPaperUpload, "save", fail_after_file_saved):
            with self.assertRaises(RuntimeError):
                self.upload()
        self.assertFalse(QuestionPaperUpload.objects.filter(uploaded_by=self.student).exists())
        self.assertFalse(CreditUse.objects.filter(user=self.student).exists())
        files = [os.path.join(path, f) for path, _, names in os.walk(os.path.join(self.root.name, "private")) for f in names]
        self.assertEqual(files, [])
        self.upload(title="After rollback")
        self.assertTrue(self.use(self.paper()).is_free)
