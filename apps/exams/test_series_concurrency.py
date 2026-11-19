"""Competing publishers must never replace a claimed series slot."""
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, local
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.db import close_old_connections
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.payments.services import lock_practice_user
from apps.questions.models import ExtractedFigure, ExtractedQuestion, Question, QuestionOption, QuestionPaperUpload
from .models import Category, Exam, ExamAttempt, ExamQuestion, SeriesSlot, TestSeries
from .test_series_workflow import printed_answer_png


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SeriesSlotConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="series-claim-")
        self.addCleanup(self.root.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=os.path.join(self.root.name, "public"),
            STATIC_ROOT=os.path.join(self.root.name, "static"), PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private")))
        self.category = Category.objects.create(name="Slot race", slug="slot-race")
        self.series, _ = TestSeries.objects.get_or_create(track="jee", defaults={"title": "JEE series"})
        self.slot, _ = SeriesSlot.objects.get_or_create(series=self.series, position=1)
        self.admins = [User.objects.create_user(f"slot-admin-{i}", email=f"slot-{i}@example.test", role="ADMIN") for i in range(2)]
        self.papers = []
        self.clients = []
        for i, admin in enumerate(self.admins):
            paper = QuestionPaperUpload.objects.create(title=f"Race paper {i}", category=self.category, uploaded_by=admin,
                status="extracted", target_type="jee_main", subject="Math", is_classroom_private=True, image_first=True)
            question = ExtractedQuestion.objects.create(paper=paper, question_number=1, question_text=f"Race question {i}",
                is_flagged=False, options=[{"key": "A", "text": "Four"}, {"key": "B", "text": "Five"}],
                positive_marks=4, negative_marks=1)
            figure = ExtractedFigure(paper=paper, question=question, source="question_crop", page_number=1)
            figure.private_image.save(f"race-source-{i}.png", ContentFile(printed_answer_png()), save=True)
            self.papers.append(paper)
            client = Client()
            client.force_login(admin)
            self.clients.append(client)

    def private_files(self):
        root = os.path.join(self.root.name, "private")
        return {os.path.relpath(os.path.join(base, name), root)
                for base, _, names in os.walk(root) for name in names}

    def test_single_choice_save_racing_submit_never_overwrites_the_final_answer(self):
        published = self.clients[0].post(reverse("questions:series_paper_publish", args=[self.papers[0].pk]), {
            "position": 1, "duration_minutes": 20, "answer_key_text": "1:A",
            "confirm_review": "on", "acknowledge_images": "on",
        })
        self.assertRedirects(published, reverse("exams:series_operator"), fetch_redirect_response=False)
        self.slot.refresh_from_db()
        student = User.objects.create_user("series-race-student", email="series-race@example.test",
                                           role="STUDENT", exam_track="jee")
        start_client = Client(); start_client.force_login(student)
        start_client.post(reverse("exams:exam_start", args=[self.slot.exam.slug]))
        attempt = self.slot.exam.attempts.get(student=student)
        deadline = attempt.expires_at
        answer = attempt.responses.select_related("exam_question__question").get()
        correct = answer.exam_question.question.options.get(option_key="A")
        stale = answer.exam_question.question.options.get(option_key="B")
        save_client, submit_client = Client(), Client()
        save_client.force_login(student); submit_client.force_login(student)
        barrier = Barrier(2)
        save_url = reverse("exams:exam_save_answer", args=[attempt.attempt_uuid])
        submit_url = reverse("exams:exam_submit", args=[attempt.attempt_uuid])

        def call(client, url, payload):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = client.post(url, data=json.dumps(payload), content_type="application/json")
                return response.status_code, response.json()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            old_save = pool.submit(call, save_client, save_url,
                                   {"response_id": answer.pk, "selected_option_id": stale.pk, "action": "navigate"})
            final = pool.submit(call, submit_client, submit_url,
                                {"last_response": {"response_id": answer.pk, "selected_option_id": correct.pk}})
            saved, submitted = old_save.result(timeout=30), final.result(timeout=30)
        self.assertEqual(submitted[0], 200, submitted)
        self.assertEqual(submitted[1]["status"], "submitted")
        self.assertEqual(saved[0], 200, saved)
        self.assertIn(saved[1]["status"], ("success", "expired"))
        attempt.refresh_from_db(); answer.refresh_from_db()
        self.assertEqual((attempt.status, attempt.expires_at, attempt.score_obtained),
                         (ExamAttempt.AttemptStatus.SUBMITTED, deadline, 4))
        self.assertEqual(answer.selected_option_id, correct.pk)
        self.assertTrue(answer.is_correct)
        late = save_client.post(save_url, data=json.dumps({"response_id": answer.pk, "action": "clear"}),
                                content_type="application/json")
        self.assertEqual(late.json()["status"], "expired")
        answer.refresh_from_db(); self.assertEqual(answer.selected_option_id, correct.pk)

    def test_two_students_can_submit_without_rewriting_each_others_series_result(self):
        published = self.clients[0].post(reverse("questions:series_paper_publish", args=[self.papers[0].pk]), {
            "position": 1, "duration_minutes": 20, "answer_key_text": "1:A",
            "confirm_review": "on", "acknowledge_images": "on",
        })
        self.assertRedirects(published, reverse("exams:series_operator"), fetch_redirect_response=False)
        self.slot.refresh_from_db()
        exam = self.slot.exam
        correct = exam.questions.get().options.get(option_key="A")
        clients, attempts = [], []
        for i in range(2):
            user = User.objects.create_user(f"series-two-{i}", email=f"series-two-{i}@example.test",
                                            role="STUDENT", exam_track="jee")
            client = Client(); client.force_login(user)
            client.post(reverse("exams:exam_start", args=[exam.slug]))
            clients.append(client); attempts.append(exam.attempts.get(student=user))
        barrier = Barrier(2)

        def submit(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                attempt = attempts[index]
                response = clients[index].post(reverse("exams:exam_submit", args=[attempt.attempt_uuid]),
                    json.dumps({"last_response": {"response_id": attempt.responses.get().pk,
                                                   "selected_option_id": correct.pk}}),
                    content_type="application/json")
                return response.status_code, response.json()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual([status for status, _ in results], [200, 200], results)
        for attempt in attempts:
            attempt.refresh_from_db()
            self.assertEqual((attempt.status, attempt.score_obtained, attempt.rank, attempt.percentile),
                             (ExamAttempt.AttemptStatus.SUBMITTED, 4, None, None))
            self.assertTrue(attempt.responses.get().is_correct)

    def race_account_deletion_against_start(self, first, kind):
        """A pre-delete preview must not race past a newly created attempt."""
        from apps.accounts.admin import UserAdmin

        if kind == "series":
            published = self.clients[0].post(reverse("questions:series_paper_publish", args=[self.papers[0].pk]), {
                "position": 1, "duration_minutes": 20, "answer_key_text": "1:A",
                "confirm_review": "on", "acknowledge_images": "on",
            })
            self.assertRedirects(published, reverse("exams:series_operator"), fetch_redirect_response=False)
            self.slot.refresh_from_db()
            exam = self.slot.exam
        else:
            exam = Exam.objects.create(title="Institutional deletion race", slug="institutional-deletion-race",
                category=self.category, creator=self.admins[0], status="published", target_type="jee_main",
                duration_minutes=20, is_free=True, shuffle_questions=False)
            question = Question.objects.create(category=self.category, creator=self.admins[0], target_type="jee_main",
                question_text="Race question", subject="Math", answer_status="manually_verified")
            QuestionOption.objects.create(question=question, option_key="A", option_text="A", is_correct=True)
            QuestionOption.objects.create(question=question, option_key="B", option_text="B")
            ExamQuestion.objects.create(exam=exam, question=question, order=1, positive_marks=4, negative_marks=1)
            exam.recalculate_marks()
        student = User.objects.create_user("deletion-race-student", email="deletion-race@example.test",
                                           role="STUDENT", exam_track="jee")
        root = User.objects.create_superuser("deletion-race-root", "deletion-root@example.test", "SafePass987!")
        start_client, delete_client = Client(), Client()
        start_client.force_login(student)
        delete_client.force_login(root)
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
            if name == "start" and first == "delete":
                attempted.set()
            return lock_practice_user(user)

        def controlled_permission(admin, request, obj=None):
            result = original_permission(admin, request, obj)
            # The admin has read the old, empty account while the new start is
            # uncommitted. This is exactly the stale-preview case to recheck.
            if getattr(actor, "name", "") == "delete" and first == "start" and obj and obj.pk == student.pk:
                attempted.set()
            return result

        def run(name):
            close_old_connections()
            actor.name = name
            try:
                if name == "start":
                    return start_client.post(reverse("exams:exam_start", args=[exam.slug]))
                return delete_client.post(reverse("admin:accounts_user_delete", args=[student.pk]), {"post": "yes"})
            finally:
                close_old_connections()

        second = "delete" if first == "start" else "start"
        with patch("apps.payments.services.lock_practice_user", controlled_lock), \
             patch.object(UserAdmin, "has_delete_permission", controlled_permission):
            with ThreadPoolExecutor(max_workers=2) as pool:
                winning = pool.submit(run, first)
                try:
                    self.assertTrue(acquired.wait(timeout=15), "first request did not acquire the account lock")
                    competing = pool.submit(run, second)
                    self.assertTrue(attempted.wait(timeout=15), "competing request did not reach the account")
                finally:
                    release.set()
                results = {first: winning.result(timeout=30), second: competing.result(timeout=30)}
        self.assertTrue(Exam.objects.filter(pk=exam.pk).exists())
        if first == "start":
            self.assertEqual(results["start"].status_code, 302)
            self.assertIn(results["delete"].status_code, (403, 503))
            if results["delete"].status_code == 503:
                self.assertEqual(delete_client.post(reverse("admin:accounts_user_delete", args=[student.pk]),
                                                    {"post": "yes"}).status_code, 403)
            self.assertTrue(User.objects.filter(pk=student.pk).exists())
            attempt = exam.attempts.get(student=student)
            self.assertEqual(attempt.expires_at - attempt.started_at, timedelta(minutes=20))
            self.assertEqual(attempt.responses.count(), 1)
            self.assertEqual(start_client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).status_code, 200)
        else:
            self.assertEqual(results["delete"].status_code, 302)
            self.assertIn(results["start"].status_code, (302, 403, 404, 503))
            self.assertFalse(User.objects.filter(pk=student.pk).exists())
            self.assertFalse(exam.attempts.filter(student_id=student.pk).exists())

    def test_series_start_wins_before_account_deletion(self):
        self.race_account_deletion_against_start("start", "series")

    def test_account_deletion_wins_before_series_start(self):
        self.race_account_deletion_against_start("delete", "series")

    def test_institutional_start_wins_before_account_deletion(self):
        self.race_account_deletion_against_start("start", "institutional")

    def test_account_deletion_wins_before_institutional_start(self):
        self.race_account_deletion_against_start("delete", "institutional")

    def test_two_admins_compete_for_one_slot_without_replacement_or_orphans(self):
        before = self.private_files()
        self.assertEqual(len(before), 2)
        actor = local()
        slot_locked, competitor_started, release = Event(), Event(), Event()
        original_clean = SeriesSlot.full_clean

        def controlled_lock(user):
            if getattr(actor, "name", "") == "competitor":
                competitor_started.set()
            return lock_practice_user(user)

        def hold_slot(instance, *args, **kwargs):
            if getattr(actor, "name", "") == "first":
                slot_locked.set()
                if not release.wait(timeout=15):
                    raise AssertionError("competing publisher did not start")
            return original_clean(instance, *args, **kwargs)

        def publish(index, name):
            close_old_connections()
            actor.name = name
            try:
                return self.clients[index].post(reverse("questions:series_paper_publish", args=[self.papers[index].pk]), {
                    "position": 1, "duration_minutes": 20, "answer_key_text": "1:A",
                    "confirm_review": "on", "acknowledge_images": "on",
                })
            finally:
                close_old_connections()

        with patch("apps.payments.services.lock_practice_user", controlled_lock), patch.object(SeriesSlot, "full_clean", hold_slot):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(publish, 0, "first")
                try:
                    self.assertTrue(slot_locked.wait(timeout=15), "first publisher did not lock the slot")
                    second = pool.submit(publish, 1, "competitor")
                    self.assertTrue(competitor_started.wait(timeout=15), "competitor did not enter the transaction")
                finally:
                    release.set()
                winner, loser = first.result(timeout=30), second.result(timeout=30)
        self.assertRedirects(winner, reverse("exams:series_operator"), fetch_redirect_response=False)
        # SQLite may safely ask for a retry under lock contention; PostgreSQL
        # waits on the slot row. Either way the loser cannot create a snapshot.
        self.assertIn(loser.status_code, (200, 503))
        if loser.status_code == 503:
            loser = self.clients[1].post(reverse("questions:series_paper_publish", args=[self.papers[1].pk]), {
                "position": 1, "duration_minutes": 20, "answer_key_text": "1:A",
                "confirm_review": "on", "acknowledge_images": "on",
            })
        self.assertContains(loser, "already assigned")
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.exam.creator_id, self.admins[0].pk)
        self.assertEqual(Exam.objects.filter(series_slot=self.slot).count(), 1)
        self.assertEqual(Question.objects.filter(is_classroom_snapshot=True).count(), 1)
        self.assertEqual(len(self.private_files() - before), 1)
        self.assertTrue(before.issubset(self.private_files()))
