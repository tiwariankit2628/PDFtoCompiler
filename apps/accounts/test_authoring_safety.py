"""Fresh authoring, verification and deletion races on a disposable database."""
import io
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event, local
from unittest.mock import patch

import pymupdf as fitz
from PIL import Image
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, close_old_connections, connection
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.exams.models import Category, Exam
from apps.payments.services import lock_practice_user
from apps.questions.models import Question, QuestionPaperUpload
from .admin import UserAdmin
from .models import User
from .verification import SALT, send_verification, token_user


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   PUBLIC_BASE_URL="https://example.test")
class AuthoringSafetyTests(TransactionTestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="authoring-safety-")
        self.addCleanup(self.root.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=os.path.join(self.root.name, "media"),
            STATIC_ROOT=os.path.join(self.root.name, "static"), PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private")))
        self.category = Category.objects.create(name="Safe authoring", slug="safe-authoring")
        self.teacher = User.objects.create_user("safe-author", email="safe-author@example.test", role="TEACHER",
                                                is_verified=True, verification_required=True)
        self.superuser = User.objects.create_superuser("safe-root", "safe-root@example.test", "StrongPassword123!")
        self.work_client, self.admin_client = Client(), Client()
        self.work_client.force_login(self.teacher)
        self.admin_client.force_login(self.superuser)

    @staticmethod
    def pdf():
        document = fitz.open()
        document.new_page()
        content = document.tobytes()
        document.close()
        return SimpleUploadedFile("safe.pdf", content, content_type="application/pdf")

    @staticmethod
    def image():
        data = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(data, format="PNG")
        return SimpleUploadedFile("safe.png", data.getvalue(), content_type="image/png")

    def work(self, kind):
        if kind == "exam":
            return reverse("exams:exam_create"), {
                "title": "Saved public test", "category": self.category.pk, "target_type": "jee_main",
                "duration_minutes": 20, "total_marks": 4, "passing_percentage": 40,
                "default_positive_marks": 4, "default_negative_marks": 1,
                "max_attempts": 1, "status": "draft", "is_free": "on",
            }
        if kind == "question":
            return reverse("questions:question_create"), {
                "category": self.category.pk, "target_type": "jee_main", "subject": "Math",
                "difficulty": "medium", "language": "en", "question_type": "mcq_single",
                "question_text": "Saved public question", "option_text_A": "A", "option_text_B": "B",
                "correct_option": "A", "positive_marks": 4, "negative_marks": 1,
            }
        return reverse("questions:paper_upload"), {
            "title": "Saved public paper", "category": self.category.pk, "target_type": "jee_main",
            "subject": "Math", "pdf_file": self.pdf(),
        }

    def media_files(self):
        root = os.path.join(self.root.name, "media")
        return {os.path.relpath(os.path.join(base, name), root)
                for base, _, names in os.walk(root) for name in names}

    def race_deletion(self, kind, first):
        work_url, data = self.work(kind)
        acquired, attempted, release = Event(), Event(), Event()
        actor = local()
        original_permission = UserAdmin.has_delete_permission

        def controlled_lock(user):
            name = getattr(actor, "name", "")
            if name == first and not getattr(actor, "held", False):
                locked = lock_practice_user(user)
                actor.held = True
                acquired.set()
                if not release.wait(timeout=15):
                    raise AssertionError("The competing account request was not released")
                return locked
            if name == "work" and first == "delete":
                attempted.set()
            return lock_practice_user(user)

        def controlled_permission(admin, request, obj=None):
            result = original_permission(admin, request, obj)
            if getattr(actor, "name", "") == "delete" and first == "work" and obj and obj.pk == self.teacher.pk:
                # The deletion preview can be stale while the author is writing.
                attempted.set()
            return result

        def run(name):
            close_old_connections()
            actor.name = name
            try:
                if name == "work":
                    return self.work_client.post(work_url, data)
                return self.admin_client.post(reverse("admin:accounts_user_delete", args=[self.teacher.pk]), {"post": "yes"})
            finally:
                close_old_connections()

        second = "delete" if first == "work" else "work"
        with patch("apps.payments.services.lock_practice_user", controlled_lock), \
             patch.object(UserAdmin, "has_delete_permission", controlled_permission), \
             patch("apps.questions.views.PDFQuestionExtractor.extract", return_value=0):
            with ThreadPoolExecutor(max_workers=2) as pool:
                winner = pool.submit(run, first)
                try:
                    self.assertTrue(acquired.wait(timeout=15), "first request did not acquire the account lock")
                    competitor = pool.submit(run, second)
                    self.assertTrue(attempted.wait(timeout=15), "second request did not reach the account")
                finally:
                    release.set()
                results = {first: winner.result(timeout=30), second: competitor.result(timeout=30)}

        created = {"exam": lambda: Exam.objects.filter(creator_id=self.teacher.pk),
                   "question": lambda: Question.objects.filter(creator_id=self.teacher.pk),
                   "paper": lambda: QuestionPaperUpload.objects.filter(uploaded_by_id=self.teacher.pk)}[kind]()
        if first == "work":
            self.assertEqual(results["work"].status_code, 302)
            self.assertIn(results["delete"].status_code, (403, 503))
            if results["delete"].status_code == 503:
                self.assertEqual(self.admin_client.post(reverse("admin:accounts_user_delete", args=[self.teacher.pk]),
                                                        {"post": "yes"}).status_code, 403)
            self.assertTrue(User.objects.filter(pk=self.teacher.pk).exists())
            self.assertEqual(created.count(), 1)
            if kind == "paper":
                self.assertTrue(os.path.isfile(created.get().pdf_file.path))
        else:
            self.assertEqual(results["delete"].status_code, 302)
            self.assertIn(results["work"].status_code, (302, 403, 404, 503))
            self.assertFalse(User.objects.filter(pk=self.teacher.pk).exists())
            self.assertFalse(created.exists())
            self.assertFalse(self.media_files())

    def test_public_exam_creation_wins_before_deletion(self):
        self.race_deletion("exam", "work")

    def test_deletion_wins_before_public_exam_creation(self):
        self.race_deletion("exam", "delete")

    def test_public_question_creation_wins_before_deletion(self):
        self.race_deletion("question", "work")

    def test_deletion_wins_before_public_question_creation(self):
        self.race_deletion("question", "delete")

    def test_public_upload_wins_before_deletion(self):
        self.race_deletion("paper", "work")

    def test_deletion_wins_before_public_upload(self):
        self.race_deletion("paper", "delete")

    def test_rolled_back_public_upload_cleans_only_its_new_file(self):
        keep = os.path.join(self.root.name, "media", "existing.pdf")
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        with open(keep, "wb") as stream:
            stream.write(b"existing source")
        url, data = self.work("paper")
        original_save = QuestionPaperUpload.save

        def fail_after_save(paper, *args, **kwargs):
            original_save(paper, *args, **kwargs)
            raise OperationalError("database is locked")

        with patch.object(QuestionPaperUpload, "save", fail_after_save), \
             patch("apps.questions.views.PDFQuestionExtractor.extract") as extract:
            response = self.work_client.post(url, data)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "1")
        extract.assert_not_called()
        self.assertFalse(QuestionPaperUpload.objects.filter(uploaded_by=self.teacher).exists())
        self.assertEqual(self.media_files(), {"existing.pdf"})
        with open(keep, "rb") as stream:
            self.assertEqual(stream.read(), b"existing source")

    def test_rolled_back_public_question_cleans_only_its_new_image(self):
        keep = os.path.join(self.root.name, "media", "existing.png")
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        with open(keep, "wb") as stream:
            stream.write(b"existing image")
        url, data = self.work("question")
        data["image"] = self.image()
        with patch("apps.questions.views.QuestionOption.objects.create", side_effect=OperationalError("database is locked")):
            response = self.work_client.post(url, data)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Question.objects.filter(creator=self.teacher).exists())
        self.assertEqual(self.media_files(), {"existing.png"})
        with open(keep, "rb") as stream:
            self.assertEqual(stream.read(), b"existing image")

    def test_public_pdf_extraction_runs_after_account_transaction_commits(self):
        url, data = self.work("paper")

        def extract_outside_transaction(extractor):
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(QuestionPaperUpload.objects.filter(pk=extractor.paper.pk).exists())
            return 0

        with patch("apps.questions.views.PDFQuestionExtractor.extract", autospec=True,
                   side_effect=extract_outside_transaction) as extract:
            response = self.work_client.post(url, data)
        self.assertEqual(response.status_code, 302)
        extract.assert_called_once()
        paper = QuestionPaperUpload.objects.get(uploaded_by=self.teacher)
        self.assertTrue(os.path.isfile(paper.pdf_file.path))

    def test_public_upload_rechecks_changed_category_permissions_after_validation(self):
        from apps.core.permissions import lock_active_teacher

        other = Category.objects.create(name="Other permission", slug="other-permission")
        self.teacher.assigned_categories.add(self.category)
        url, data = self.work("paper")
        reached, release = Event(), Event()

        def wait_before_lock(user):
            # The upload form has validated the old category by this point.
            reached.set()
            if not release.wait(timeout=15):
                raise AssertionError("The permissions update was not released")
            return lock_active_teacher(user)

        def upload():
            close_old_connections()
            try:
                return self.work_client.post(url, data)
            finally:
                close_old_connections()

        with patch("apps.questions.views.lock_active_teacher", wait_before_lock), \
             patch("apps.questions.views.PDFQuestionExtractor.extract") as extract:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(upload)
                try:
                    self.assertTrue(reached.wait(timeout=15), "upload did not validate its form")
                    edit = self.admin_client.post(reverse("accounts:teacher_edit", args=[self.teacher.pk]), {
                        "first_name": "", "last_name": "", "email": self.teacher.email, "phone": "",
                        "institute_name": "", "bio": "", "is_active": "on", "assigned_categories": [other.pk],
                    })
                    self.assertEqual(edit.status_code, 302)
                finally:
                    release.set()
                response = pending.result(timeout=30)
        self.assertEqual(response.status_code, 403)
        extract.assert_not_called()
        self.assertFalse(QuestionPaperUpload.objects.filter(uploaded_by=self.teacher).exists())
        self.assertEqual(set(self.teacher.assigned_categories.values_list("pk", flat=True)), {other.pk})
        self.assertFalse(self.media_files())

    def race_verification_resend_against_admin_edit(self, kind, first):
        self.teacher.is_verified = False
        self.teacher.save(update_fields=["is_verified"])
        original_nonce = self.teacher.verification_token
        old_token = signing.dumps({"uid": self.teacher.pk, "email": self.teacher.email,
                                   "nonce": str(original_nonce)}, salt=SALT, compress=True)
        self.assertIsNotNone(token_user(old_token))
        if kind == "management":
            url = reverse("accounts:teacher_edit", args=[self.teacher.pk])
            edit_data = {"first_name": "Admin edit", "last_name": "", "email": self.teacher.email,
                         "phone": "", "institute_name": "", "bio": "", "is_active": "on"}
        else:
            url = reverse("admin:accounts_user_change", args=[self.teacher.pk])
            joined = timezone.localtime(self.teacher.date_joined)
            edit_data = {"username": self.teacher.username, "email": self.teacher.email, "role": "TEACHER",
                         "date_joined_0": joined.strftime("%Y-%m-%d"), "date_joined_1": joined.strftime("%H:%M:%S"),
                         "is_active": "on", "first_name": "Admin edit", "last_name": "", "phone": "",
                         "target_exam": "", "institute_name": "", "bio": "", "_save": "Save"}
        acquired, attempted, release = Event(), Event(), Event()
        actor = local()

        def controlled_lock(user):
            name = getattr(actor, "name", "")
            if name == first and not getattr(actor, "held", False):
                locked = lock_practice_user(user)
                actor.held = True
                acquired.set()
                if not release.wait(timeout=15):
                    raise AssertionError("The competing verification request was not released")
                return locked
            if name and name != first:
                attempted.set()
            return lock_practice_user(user)

        def run(name):
            close_old_connections()
            actor.name = name
            try:
                if name == "send":
                    return send_verification(self.teacher)
                return self.admin_client.post(url, edit_data)
            finally:
                close_old_connections()

        second = "edit" if first == "send" else "send"
        with patch("apps.payments.services.lock_practice_user", controlled_lock), \
             patch("apps.accounts.verification.send_mail", return_value=1) as send:
            with ThreadPoolExecutor(max_workers=2) as pool:
                winner = pool.submit(run, first)
                try:
                    self.assertTrue(acquired.wait(timeout=15), "first request did not acquire the account lock")
                    competitor = pool.submit(run, second)
                    self.assertTrue(attempted.wait(timeout=15), "second request did not reach the account")
                finally:
                    release.set()
                results = {first: winner.result(timeout=30), second: competitor.result(timeout=30)}
        self.assertEqual(results["send"], "sent")
        self.assertEqual(results["edit"].status_code, 302)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.first_name, "Admin edit")
        self.assertFalse(self.teacher.is_verified)
        self.assertTrue(self.teacher.verification_required)
        self.assertIsNotNone(self.teacher.verification_sent_at)
        self.assertNotEqual(self.teacher.verification_token, original_nonce)
        self.assertIsNone(token_user(old_token))
        body = send.call_args.args[1]
        link = re.search(r"https://example\.test/\S+", body).group()
        token = link.rstrip("/").split("/")[-1]
        self.assertEqual(token_user(token).pk, self.teacher.pk)

    def test_resend_wins_before_teacher_management_edit(self):
        self.race_verification_resend_against_admin_edit("management", "send")

    def test_teacher_management_edit_wins_before_resend(self):
        self.race_verification_resend_against_admin_edit("management", "edit")

    def test_resend_wins_before_django_admin_edit(self):
        self.race_verification_resend_against_admin_edit("django", "send")

    def test_django_admin_edit_wins_before_resend(self):
        self.race_verification_resend_against_admin_edit("django", "edit")
