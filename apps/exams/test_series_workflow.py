"""Private series publication, typed results, asset access and release policy.

All PDFs and images below are synthetic and processed locally. Provider/payment
behavior is tested separately; these tests do not make a real purchase.
"""
import io
import json
import os
import tempfile
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from PIL import Image, ImageDraw, PngImagePlugin
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.payments.models import CreditUse
from apps.questions.models import ExtractedFigure, Question, QuestionOption, QuestionPaperUpload
from apps.questions.test_helpers import response_body
from apps.questions.test_jee_main_workflow import jee_style_pdf
from .models import Category, Exam, ExamAttempt, ExamQuestion, TestSeries
from .series import series_content_is_private, series_keys_confirmed, series_ready, slot_ready


def printed_answer_png():
    image = Image.new("RGB", (340, 80), "white")
    ImageDraw.Draw(image).text((8, 20), "PRINTED ANSWER: A", fill="black")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("test-marker", "PRINTED ANSWER: A")
    output = io.BytesIO()
    image.save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SeriesPrivateWorkflowTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=os.path.join(self.root.name, "public"),
            STATIC_ROOT=os.path.join(self.root.name, "static"), PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private")))
        self.admin = User.objects.create_user("series-admin", email="sa@example.test", role="ADMIN")
        self.teacher = User.objects.create_user("series-teacher", email="st@example.test", role="TEACHER")
        self.student = User.objects.create_user("series-student", email="ss@example.test", role="STUDENT", exam_track="jee")
        self.other = User.objects.create_user("series-other", email="so@example.test", role="STUDENT", exam_track="jee")
        self.neet = User.objects.create_user("series-neet", email="sn@example.test", role="STUDENT", exam_track="neet")
        self.category = Category.objects.create(name="Series private fixtures", slug="series-private-fixtures")
        self.teacher.assigned_categories.add(self.category)
        self.jee = TestSeries.objects.get(track="jee")

    def upload_reviewed_source(self):
        self.client.force_login(self.admin)
        with patch("requests.sessions.Session.request", side_effect=AssertionError("PDF must remain local")):
            response = self.client.post(reverse("questions:classroom_paper_upload"), {
                "title": "Synthetic JEE series paper", "subject": "Physics", "target_type": "jee_main",
                "category": self.category.pk, "image_first": "on",
                "pdf_file": SimpleUploadedFile("jee.pdf", jee_style_pdf(), content_type="application/pdf"),
            })
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        paper = QuestionPaperUpload.objects.get(uploaded_by=self.admin, is_classroom_private=True)
        self.assertEqual(paper.extracted_questions.count(), 5, paper.extraction_log)
        first = paper.extracted_questions.get(question_number=1)
        second = paper.extracted_questions.get(question_number=2)
        first.explanation = "SECRET SINGLE EXPLANATION"
        first.private_image.save("diagram.png", ContentFile(printed_answer_png()), save=False)
        first.save()
        second.explanation = "SECRET NUMERICAL EXPLANATION"
        second.save(update_fields=["explanation"])
        # Deliberately include a visible printed answer in an original image.
        # An access gate cannot remove marks already printed in a crop.
        first_crop = first.figures.filter(source="question_crop").first()
        self.assertIsNotNone(first_crop)
        first_crop.private_image.save("printed-answer.png", ContentFile(printed_answer_png()), save=True)
        option_figure = ExtractedFigure(paper=paper, question=first, option_key="A", source="manual", page_number=1)
        option_figure.private_image.save("option.png", ContentFile(printed_answer_png()), save=True)
        return paper

    def publish(self, paper, position=1, keys="1:A, 2:876.25, 3:-2.5, 4:0, 5:B"):
        self.client.force_login(self.admin)
        return self.client.post(reverse("questions:series_paper_publish", args=[paper.pk]), {
            "position": position, "duration_minutes": 25, "answer_key_text": keys,
            "confirm_review": "on", "acknowledge_images": "on", "acknowledge_review": "on",
            "acknowledge_unassigned": "on",
        })

    def test_publication_requires_admin_review_confirmed_keys_and_keeps_source_private(self):
        paper = self.upload_reviewed_source()
        page = self.client.get(reverse("questions:classroom_paper_review", args=[paper.pk]))
        self.assertContains(page, "Publish to series")
        publish_page = self.client.get(reverse("questions:series_paper_publish", args=[paper.pk]))
        self.assertContains(publish_page, "printed answers")
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(reverse("questions:series_paper_publish", args=[paper.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("exams:series_operator")).status_code, 403)
        self.client.force_login(self.admin)
        response = self.publish(paper, keys="1:A, 2:876.25, 3:-2.5, 4:0")
        self.assertContains(response, "Confirm a key for each series question first")
        self.assertFalse(self.jee.slots.get(position=1).exam_id)
        response = self.publish(paper)
        self.assertRedirects(response, reverse("exams:series_operator"), fetch_redirect_response=False)
        slot = self.jee.slots.select_related("exam", "series").get(position=1)
        exam = slot.exam
        self.assertTrue(slot_ready(slot) and series_keys_confirmed(exam))
        self.assertFalse(series_ready("jee"))  # other 15 are genuinely empty
        self.assertTrue(series_content_is_private(exam))
        self.assertEqual(exam.exam_questions.count(), 5)
        self.assertTrue(all(q.is_classroom_snapshot and not q.image for q in exam.questions.all()))
        self.assertFalse(CreditUse.objects.filter(user=self.admin).exists())
        self.assertEqual(paper.extracted_questions.count(), 5)
        self.assertTrue(paper.private_pdf_file)
        self.assertFalse(paper.pdf_file)
        duplicate = self.publish(paper)
        self.assertContains(duplicate, "already assigned")
        self.assertEqual(Exam.objects.filter(series_slot=slot).count(), 1)

    def test_series_results_images_and_keys_are_attempt_scoped(self):
        paper = self.upload_reviewed_source()
        self.publish(paper)
        self.publish(paper, position=2)
        free = self.jee.slots.select_related("exam").get(position=1).exam
        locked = self.jee.slots.select_related("exam").get(position=2).exam
        first_q = free.questions.order_by("exam_instances__order").first()
        other_crop = locked.questions.order_by("exam_instances__order").first().figures.filter(source="question_crop").first()
        self.client.force_login(self.student)
        self.assertNotContains(self.client.get(reverse("exams:exam_catalog")), free.title)
        self.assertRedirects(self.client.post(reverse("exams:exam_start", args=[locked.slug])), reverse("exams:series"), fetch_redirect_response=False)
        self.assertFalse(locked.attempts.exists())
        self.client.post(reverse("exams:exam_start", args=[free.slug]))
        attempt = free.attempts.get(student=self.student)
        self.student.refresh_from_db()
        self.assertFalse(self.student.free_test_attempted)
        self.assertFalse(CreditUse.objects.filter(user=self.student, kind="classroom").exists())
        expires = attempt.expires_at
        screen = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(screen.status_code, 200)
        self.assertIn("no-store", screen["Cache-Control"])
        self.assertNotContains(screen, "SECRET SINGLE EXPLANATION")
        self.assertNotContains(screen, "876.25")
        questions = screen.context["questions_data"]
        self.assertEqual(questions[1]["question_type"], "numerical")
        self.assertTrue(questions[0]["image_first"])
        crop_url = questions[0]["crop_urls"][0]
        image_url = reverse("exams:series_attempt_image", args=[attempt.attempt_uuid, first_q.pk])
        option_fig = first_q.figures.get(option__option_key="A")
        option_url = reverse("exams:series_attempt_figure", args=[attempt.attempt_uuid, option_fig.pk])
        for url in (crop_url, image_url, option_url):
            image = self.client.get(url)
            self.assertEqual(image.status_code, 200)
            self.assertIn("no-store", image["Cache-Control"])
            self.assertIn(b"PRINTED ANSWER: A", response_body(image))
        wrong_figure = reverse("exams:series_attempt_figure", args=[attempt.attempt_uuid, other_crop.pk])
        self.assertEqual(self.client.get(wrong_figure).status_code, 404)
        selected = next(o["id"] for o in questions[0]["options"] if o["key"] == "A")
        save_url = reverse("exams:exam_save_answer", args=[attempt.attempt_uuid])
        for item, values in ((questions[0], {"selected_option_id": selected}),
                             (questions[1], {"numerical_response": "5"})):
            response = self.client.post(save_url, json.dumps({"response_id": item["response_id"], "action": "navigate", **values}), content_type="application/json")
            self.assertEqual(response.status_code, 200)
        self.client.post(reverse("exams:exam_start", args=[free.slug]))
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, expires)
        self.client.post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]), "{}", content_type="application/json")
        attempt.refresh_from_db()
        self.assertEqual(attempt.score_obtained, Decimal("3.00"))
        self.assertEqual((attempt.correct_count, attempt.incorrect_count, attempt.unattempted_count), (1, 1, 3))
        result = self.client.get(reverse("exams:student_result_detail", args=[attempt.attempt_uuid]))
        self.assertContains(result, "3.00")
        self.assertContains(result, "Unattempted")
        own = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        self.assertContains(own, "Your answer review")
        self.assertContains(own, "Your choice")
        self.assertContains(own, "printed answers")
        self.assertContains(own, "5")
        self.assertNotContains(own, "876.25")
        self.assertNotContains(own, "SECRET NUMERICAL EXPLANATION")
        self.assertNotContains(own, "SECRET SINGLE EXPLANATION")
        self.assertEqual(self.client.get(crop_url).status_code, 200)  # original image can itself contain an answer
        retry = self.client.post(reverse("exams:exam_start", args=[free.slug]))
        self.assertRedirects(retry, reverse("exams:exam_completed", args=[attempt.attempt_uuid]), fetch_redirect_response=False)
        self.assertEqual(free.attempts.filter(student=self.student).count(), 1)
        for outsider in (self.other, self.neet, self.teacher):
            self.client.force_login(outsider)
            for url in (crop_url, image_url, option_url, reverse("exams:student_result_review", args=[attempt.attempt_uuid])):
                self.assertIn(self.client.get(url).status_code, (403, 404))
        self.client.force_login(self.neet)
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[free.slug])).status_code, 404)
        self.client.force_login(self.teacher)
        for name in ("exam_preview", "exam_questions", "exam_edit", "teacher_exam_analytics", "teacher_export_results_csv"):
            self.assertIn(self.client.get(reverse(f"exams:{name}", args=[free.pk])).status_code, (403, 404, 405))
        self.assertEqual(self.client.post(reverse("exams:exam_duplicate", args=[free.pk])).status_code, 404)
        self.client.force_login(self.admin)
        slot = self.jee.slots.get(position=1)
        self.client.post(reverse("exams:series_release_keys", args=[slot.pk]))
        self.client.force_login(self.student)
        released = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        self.assertContains(released, "876.25")
        self.assertContains(released, "SECRET NUMERICAL EXPLANATION")
        self.assertContains(released, "SECRET SINGLE EXPLANATION")
        attempt.refresh_from_db()
        self.assertEqual(attempt.score_obtained, Decimal("3.00"))

    def test_public_or_shared_media_cannot_become_a_paid_series(self):
        public = Exam.objects.create(title="Already public", slug="already-public", category=self.category,
            creator=self.teacher, target_type="jee_main", status="published")
        q = Question.objects.create(category=self.category, creator=self.teacher, target_type="jee_main",
                                    subject="Math", question_text="public", answer_status="manually_verified")
        QuestionOption.objects.create(question=q, option_key="A", option_text="yes", is_correct=True)
        QuestionOption.objects.create(question=q, option_key="B", option_text="no")
        ExamQuestion.objects.create(exam=public, question=q, order=1, positive_marks=4, negative_marks=1)
        slot = self.jee.slots.get(position=1)
        slot.exam = public
        with self.assertRaises(ValidationError):
            slot.full_clean()
        # Even an out-of-band ORM assignment cannot bypass the start gate.
        slot.save(update_fields=["exam"])
        self.assertFalse(slot_ready(slot))
        self.client.force_login(self.student)
        self.assertRedirects(self.client.post(reverse("exams:exam_start", args=[public.slug])), reverse("exams:series"), fetch_redirect_response=False)
        self.assertFalse(public.attempts.exists())

    def single_question_series(self):
        exam = Exam.objects.create(title="Series save safety", slug="series-save-safety", category=self.category,
            creator=self.admin, status="published", target_type="jee_main", duration_minutes=20,
            shuffle_questions=False, shuffle_options=False)
        question = Question.objects.create(category=self.category, creator=self.admin, target_type="jee_main",
            question_text="Choose four", subject="Math", is_classroom_snapshot=True,
            answer_status="manually_verified")
        answer = QuestionOption.objects.create(question=question, option_key="A", option_text="Four", is_correct=True)
        QuestionOption.objects.create(question=question, option_key="B", option_text="Five")
        ExamQuestion.objects.create(exam=exam, question=question, order=1, positive_marks=4, negative_marks=1)
        exam.recalculate_marks()
        slot = self.jee.slots.get(position=1)
        slot.exam = exam
        slot.full_clean()
        slot.save(update_fields=["exam"])
        return exam, answer

    def test_single_choice_series_uses_reliable_final_submit_and_server_deadline(self):
        exam, answer = self.single_question_series()
        self.client.force_login(self.student)
        self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        attempt = exam.attempts.get(student=self.student)
        page = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertTrue(page.context["is_series"])
        self.assertContains(page, "const RELIABLE_SAVE = IS_PRIVATE_PRACTICE || true")
        submit = reverse("exams:exam_submit", args=[attempt.attempt_uuid])
        self.assertEqual(self.client.post(submit, "[]", content_type="application/json").status_code, 400)
        early = self.client.post(submit, json.dumps({"is_timeout": True}), content_type="application/json")
        self.assertEqual(early.json()["status"], "active")
        response = attempt.responses.get()
        bad = {"last_response": {"response_id": response.pk, "selected_option_id": 999999}}
        self.assertEqual(self.client.post(submit, json.dumps(bad), content_type="application/json").status_code, 400)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.IN_PROGRESS)
        # A final choice is saved atomically even when no earlier autosave arrived.
        final = {"last_response": {"response_id": response.pk, "selected_option_id": answer.pk}}
        self.assertEqual(self.client.post(submit, json.dumps(final), content_type="application/json").json()["status"], "submitted")
        attempt.refresh_from_db()
        response.refresh_from_db()
        self.assertEqual(response.selected_option_id, answer.pk)
        self.assertEqual(attempt.score_obtained, Decimal("4.00"))
        self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        self.assertEqual(exam.attempts.filter(student=self.student).count(), 1)

    def test_series_submission_and_termination_leave_institutional_ranks_unchanged(self):
        ordinary = Exam.objects.create(title="Institutional rankings", slug="institutional-rankings",
            category=self.category, creator=self.teacher, target_type="jee_main", status="published",
            duration_minutes=20, is_free=True, shuffle_questions=False)
        question = Question.objects.create(category=self.category, creator=self.teacher, target_type="jee_main",
            question_text="Institutional question", subject="Math", answer_status="manually_verified")
        correct = QuestionOption.objects.create(question=question, option_key="A", option_text="A", is_correct=True)
        wrong = QuestionOption.objects.create(question=question, option_key="B", option_text="B")
        ExamQuestion.objects.create(exam=ordinary, question=question, order=1, positive_marks=4, negative_marks=1)
        ordinary.recalculate_marks()
        institutional = []
        for user, answer in ((self.student, correct), (self.other, wrong)):
            self.client.force_login(user)
            self.client.post(reverse("exams:exam_start", args=[ordinary.slug]))
            attempt = ordinary.attempts.get(student=user)
            attempt.responses.update(selected_option=answer)
            attempt.submit_attempt()
            institutional.append(attempt.pk)
        before = list(ExamAttempt.objects.filter(pk__in=institutional).order_by("rank")
                      .values_list("pk", "rank", "percentile", "score_obtained"))
        self.assertEqual([row[1] for row in before], [1, 2])
        self.assertEqual([row[3] for row in before], [Decimal("4.00"), Decimal("-1.00")])

        series, answer = self.single_question_series()
        with patch.object(ExamAttempt, "recalculate_ranks", side_effect=AssertionError("series must not recalculate ranks")):
            self.client.force_login(self.student)
            self.client.post(reverse("exams:exam_start", args=[series.slug]))
            submitted = series.attempts.get(student=self.student)
            final = {"last_response": {"response_id": submitted.responses.get().pk, "selected_option_id": answer.pk}}
            result = self.client.post(reverse("exams:exam_submit", args=[submitted.attempt_uuid]),
                                      json.dumps(final), content_type="application/json")
            self.assertEqual(result.json()["status"], "submitted")
            self.client.force_login(self.other)
            self.client.post(reverse("exams:exam_start", args=[series.slug]))
            terminated = series.attempts.get(student=self.other)
            terminated.responses.update(selected_option=answer)
            self.assertTrue(terminated.terminate_for_security("Synthetic test"))
        for attempt in (submitted, terminated):
            attempt.refresh_from_db()
            self.assertEqual((attempt.score_obtained, attempt.rank, attempt.percentile), (Decimal("4.00"), None, None))
        after = list(ExamAttempt.objects.filter(pk__in=institutional).order_by("rank")
                     .values_list("pk", "rank", "percentile", "score_obtained"))
        self.assertEqual(after, before)

    def test_offline_series_sweep_is_idempotent_and_caps_elapsed_time(self):
        from .tasks import expire_series_attempts
        exam, _ = self.single_question_series()
        ordinary = Exam.objects.create(title="Ordinary exam", slug="ordinary-sweep", category=self.category,
            creator=self.teacher, target_type="jee", status="published")
        self.client.force_login(self.student)
        start = timezone.now().replace(microsecond=0)
        with patch("django.utils.timezone.now", return_value=start):
            self.client.post(reverse("exams:exam_start", args=[exam.slug]))
            attempt = exam.attempts.get(student=self.student)
            ordinary_attempt = ExamAttempt.objects.create(exam=ordinary, student=self.student,
                expires_at=start + timedelta(minutes=20))
        expiry = attempt.expires_at
        with patch("django.utils.timezone.now", return_value=expiry + timedelta(seconds=90)):
            self.assertEqual(expire_series_attempts(), 1)
            self.assertEqual(expire_series_attempts(), 0)
        attempt.refresh_from_db()
        ordinary_attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TIMED_OUT)
        self.assertEqual(attempt.expires_at, expiry)
        self.assertEqual(attempt.time_taken_seconds, 20 * 60)
        self.assertEqual(ordinary_attempt.status, ExamAttempt.AttemptStatus.IN_PROGRESS)
        retry = self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        self.assertRedirects(retry, reverse("exams:exam_completed", args=[attempt.attempt_uuid]), fetch_redirect_response=False)
        self.assertEqual(exam.attempts.count(), 1)

    def test_series_is_excluded_from_generic_management_and_account_deletion(self):
        from django.contrib import admin as django_admin
        from django.test import RequestFactory
        from apps.accounts.record_safety import has_protected_account_records
        from apps.questions.models import Question as BankQuestion

        exam, _ = self.single_question_series()
        question = exam.questions.get()
        self.client.force_login(self.student)
        self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        attempt = exam.attempts.get(student=self.student)
        attempt.submit_attempt()
        dashboard = self.client.get(reverse("core:student_dashboard"))
        self.assertContains(dashboard, "Test series")
        self.assertNotContains(dashboard, "Passed ·")
        self.assertEqual(dashboard.context["total_attempts"], 0)  # institutional metric only
        self.assertTrue(has_protected_account_records(self.student))  # even a free series attempt
        self.assertTrue(has_protected_account_records(self.admin))
        self.assertFalse(CreditUse.objects.filter(user=self.student).exists())

        self.client.force_login(self.teacher)  # teacher is assigned this same category
        self.assertNotContains(self.client.get(reverse("exams:exam_list")), exam.title)
        self.assertNotContains(self.client.get(reverse("questions:question_bank")), question.question_text)
        for name in ("exam_preview", "exam_questions", "exam_edit", "teacher_exam_analytics", "teacher_export_results_csv"):
            self.assertEqual(self.client.get(reverse(f"exams:{name}", args=[exam.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("exams:exam_leaderboard", args=[exam.slug])).status_code, 404)
        for name in ("question_preview", "question_edit", "bank_figure_manage"):
            self.assertEqual(self.client.get(reverse(f"questions:{name}", args=[question.pk])).status_code, 404)
        for name in ("question_delete",):
            self.assertEqual(self.client.post(reverse(f"questions:{name}", args=[question.pk])).status_code, 404)
        for name in ("exam_delete", "exam_duplicate", "exam_toggle_publish", "exam_add_questions_bulk", "exam_add_random_questions"):
            self.assertEqual(self.client.post(reverse(f"exams:{name}", args=[exam.pk])).status_code, 404)
        for name in ("student_result_detail", "student_result_review", "student_result_pdf"):
            self.assertIn(self.client.get(reverse(f"exams:{name}", args=[attempt.attempt_uuid])).status_code, (403, 404))
        self.assertEqual(exam.questions.count(), 1)
        self.assertTrue(Exam.objects.filter(pk=exam.pk).exists())

        superuser = User.objects.create_superuser("privacy-super", "privacy-super@example.test", "SafePass987!")
        request = RequestFactory().get("/admin/")
        request.user = superuser
        self.assertFalse(django_admin.site._registry[User].has_delete_permission(request, self.student))
        self.assertFalse(django_admin.site._registry[User].has_delete_permission(request, self.admin))
        self.assertFalse(django_admin.site._registry[Exam].get_queryset(request).filter(pk=exam.pk).exists())
        self.assertFalse(django_admin.site._registry[BankQuestion].get_queryset(request).filter(pk=question.pk).exists())
        self.assertFalse(django_admin.site._registry[ExamAttempt].get_queryset(request).filter(pk=attempt.pk).exists())
        self.client.force_login(superuser)
        deletion = self.client.post(reverse("accounts:admin_delete", args=[self.admin.pk]))
        self.assertRedirects(deletion, reverse("accounts:admin_list"), fetch_redirect_response=False)
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())
        self.assertTrue(ExamAttempt.objects.filter(pk=attempt.pk).exists())

    def test_downgraded_series_creator_cannot_see_series_in_teacher_dashboard(self):
        exam, _ = self.single_question_series()
        self.client.force_login(self.student)
        self.client.post(reverse("exams:exam_start", args=[exam.slug]))
        self.admin.role = User.Roles.TEACHER
        self.admin.save(update_fields=["role"])
        self.client.force_login(self.admin)
        dashboard = self.client.get(reverse("core:teacher_dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotContains(dashboard, exam.title)
        self.assertEqual(dashboard.context["total_student_attempts"], 0)
        self.assertEqual(dashboard.context["total_exams_created"], 0)

    def test_failed_snapshot_rolls_back_rows_and_new_files(self):
        paper = self.upload_reviewed_source()
        def files():
            root = os.path.join(self.root.name, "private")
            return {os.path.relpath(os.path.join(base, name), root)
                    for base, _, names in os.walk(root) for name in names}
        before = files()
        with patch("apps.exams.models.SeriesSlot.full_clean", side_effect=ValidationError("Simulated validation failure")):
            result = self.publish(paper)
        self.assertContains(result, "Simulated validation failure")
        self.assertEqual(files(), before)
        self.assertFalse(self.jee.slots.get(position=1).exam_id)
        self.assertFalse(Exam.objects.filter(slug__startswith="series-jee-").exists())
        self.assertFalse(Question.objects.filter(is_classroom_snapshot=True).exists())
