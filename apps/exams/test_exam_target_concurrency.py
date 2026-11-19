"""Real concurrent target changes and test starts on a disposable database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import tempfile
from threading import Event, local
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import Category, ClassroomParticipant, ClassroomSession, Exam, ExamAttempt, ExamQuestion
from apps.questions.models import Question, QuestionOption, QuestionPaperUpload


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ExamTargetConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.student = User.objects.create_user("race_target_student", email="race-target@example.test",
                                                role="STUDENT", exam_track="jee", target_exam="Previous label")
        self.teacher = User.objects.create_user("race_target_teacher", email="race-target-t@example.test", role="TEACHER")
        self.category = Category.objects.create(name="Race target category", slug="race-target-category")

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def make_target(self, mode):
        private = mode == "private"
        classroom = mode == "classroom"
        owner = self.student if private else self.teacher
        exam = Exam.objects.create(title=f"Race {mode}", slug=f"race-{mode}-target", category=self.category,
            creator=owner, target_type="jee_main", is_private_practice=private, is_classroom_only=classroom,
            status="published", duration_minutes=30, is_free=True, shuffle_questions=False)
        question = Question.objects.create(category=self.category, creator=owner, subject="Math", question_text="Race?",
            target_type="jee_main", is_private_practice=private, is_classroom_snapshot=classroom)
        QuestionOption.objects.create(question=question, option_key="A", option_text="A", is_correct=True)
        QuestionOption.objects.create(question=question, option_key="B", option_text="B")
        ExamQuestion.objects.create(exam=exam, question=question, order=1, positive_marks=4, negative_marks=1)
        exam.recalculate_marks()
        if classroom:
            session = ClassroomSession.create_for(exam, self.teacher)
            ClassroomParticipant.objects.create(session=session, student=self.student)
            url = reverse("exams:classroom_take", args=[session.pk])
        else:
            url = reverse("exams:exam_start", args=[exam.slug])
        return exam, url

    def race(self, mode, first):
        from apps.payments.services import lock_practice_user as original_lock

        exam, start_url = self.make_target(mode)
        start_client = self.client_for(self.student)
        profile_client = self.client_for(self.student)
        profile_data = {"first_name": "Race", "last_name": "Student", "email": self.student.email,
                        "target_exam": "NEET", "phone": "", "institute_name": "", "bio": ""}
        acquired, attempted, release = Event(), Event(), Event()
        actor = local()

        def controlled_lock(user):
            name = getattr(actor, "name", "")
            if name == first and not getattr(actor, "held", False):
                locked = original_lock(user)
                actor.held = True
                acquired.set()
                if not release.wait(timeout=12):
                    raise AssertionError("The competing target request was not released")
                return locked
            if name and name != first:
                attempted.set()
            return original_lock(user)

        def start():
            close_old_connections()
            actor.name = "start"
            try:
                return start_client.post(start_url)
            finally:
                close_old_connections()

        def change():
            close_old_connections()
            actor.name = "profile"
            try:
                return profile_client.post(reverse("accounts:profile"), profile_data)
            finally:
                close_old_connections()

        operations = {"start": start, "profile": change}
        second = "profile" if first == "start" else "start"
        with patch("apps.payments.services.lock_practice_user", controlled_lock):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(operations[first])
                try:
                    self.assertTrue(acquired.wait(timeout=10), "first request never acquired the user lock")
                    second_future = pool.submit(operations[second])
                    self.assertTrue(attempted.wait(timeout=10), "second request never attempted the same lock")
                finally:
                    release.set()
                first_result = first_future.result(timeout=20)
                second_result = second_future.result(timeout=20)
        results = {first: first_result, second: second_result}
        self.student.refresh_from_db()
        attempts = list(exam.attempts.filter(student=self.student, status=ExamAttempt.AttemptStatus.IN_PROGRESS))
        self.assertEqual(self.student.target_exam, "Previous label")
        self.assertFalse(self.student.exam_track == "neet" and attempts, "opposite-track attempt survived the race")
        if first == "start":
            self.assertEqual(results["start"].status_code, 302)
            self.assertEqual(results["profile"].status_code, 200)
            self.assertEqual(self.student.exam_track, "jee")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].expires_at - attempts[0].started_at, timedelta(minutes=30))
        else:
            self.assertEqual(results["profile"].status_code, 302)
            self.assertEqual(results["start"].status_code, 404)
            self.assertEqual(self.student.exam_track, "neet")
            self.assertEqual(attempts, [])

    def test_public_start_wins_before_profile_change(self):
        self.race("public", "start")

    def test_profile_change_wins_before_public_start(self):
        self.race("public", "profile")

    def test_private_start_wins_before_profile_change(self):
        self.race("private", "start")

    def test_profile_change_wins_before_private_start(self):
        self.race("private", "profile")

    def test_classroom_start_wins_before_profile_change(self):
        self.race("classroom", "start")

    def test_profile_change_wins_before_classroom_start(self):
        self.race("classroom", "profile")

    def race_teacher_target_edit_against_start(self, first):
        from apps.exams.forms import ExamForm
        from apps.exams.views import ExamUpdateView
        from apps.payments.services import lock_practice_user as original_lock

        exam, start_url = self.make_target("public")
        start_client, edit_client = self.client_for(self.student), self.client_for(self.teacher)
        edit_data = {"title": exam.title, "category": self.category.pk, "target_type": "jee_advanced",
                     "duration_minutes": 30, "total_marks": 4, "passing_percentage": 40,
                     "default_positive_marks": 4, "default_negative_marks": 1,
                     "max_attempts": 1, "status": "published", "is_free": "on"}
        acquired, attempted, release = Event(), Event(), Event()
        actor = local()
        original_get = ExamUpdateView.get_object
        original_clean = ExamForm.clean_target_type

        def controlled_lock(user):
            if getattr(actor, "name", "") == "start":
                if first == "start" and not getattr(actor, "held", False):
                    locked = original_lock(user); actor.held = True; acquired.set()
                    if not release.wait(timeout=12):
                        raise AssertionError("Competing exam edit was not released")
                    return locked
                if first == "edit":
                    attempted.set()
            return original_lock(user)

        def controlled_get(view, *args, **kwargs):
            if getattr(actor, "name", "") == "edit" and first == "start":
                attempted.set()
            return original_get(view, *args, **kwargs)

        def controlled_clean(form):
            if getattr(actor, "name", "") == "edit" and first == "edit" and not getattr(actor, "held", False):
                # Validation runs while the edit holds the exam row/writer lock.
                actor.held = True; acquired.set()
                if not release.wait(timeout=12):
                    raise AssertionError("Competing candidate start was not released")
            return original_clean(form)

        def post(name, client, url, data):
            close_old_connections(); actor.name = name
            try:
                return client.post(url, data)
            finally:
                close_old_connections()

        operations = {"start": (start_client, start_url, {}),
                      "edit": (edit_client, reverse("exams:exam_edit", args=[exam.pk]), edit_data)}
        second = "edit" if first == "start" else "start"
        with patch("apps.payments.services.lock_practice_user", controlled_lock), \
             patch.object(ExamUpdateView, "get_object", controlled_get), \
             patch.object(ExamForm, "clean_target_type", controlled_clean):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(post, first, *operations[first])
                try:
                    self.assertTrue(acquired.wait(timeout=10), "first request never acquired its lock")
                    second_future = pool.submit(post, second, *operations[second])
                    self.assertTrue(attempted.wait(timeout=10), "second request never reached the competing lock")
                finally:
                    release.set()
                results = {first: first_future.result(timeout=20), second: second_future.result(timeout=20)}
        exam.refresh_from_db()
        self.assertEqual(results["start"].status_code, 302)
        attempt = exam.attempts.get(student=self.student)
        self.assertEqual(attempt.expires_at - attempt.started_at, timedelta(minutes=30))
        if first == "start":
            self.assertEqual(results["edit"].status_code, 200)
            self.assertIn("target_type", results["edit"].context["form"].errors)
            self.assertEqual(exam.target_type, "jee_main")
        else:
            self.assertEqual(results["edit"].status_code, 302)
            self.assertEqual(exam.target_type, "jee_advanced")

    def test_start_wins_before_teacher_target_edit(self):
        self.race_teacher_target_edit_against_start("start")

    def test_teacher_target_edit_wins_before_start(self):
        self.race_teacher_target_edit_against_start("edit")

    def race_builder_against_question_target_edit(self, mode, first):
        from apps.exams import views as exam_views
        from apps.questions import views as question_views

        exam, start_url = self.make_target("public")
        question = Question.objects.create(category=self.category, creator=self.teacher, subject="Math",
            question_text="Competing question", target_type="jee_main")
        QuestionOption.objects.create(question=question, option_key="A", option_text="A", is_correct=True)
        QuestionOption.objects.create(question=question, option_key="B", option_text="B")
        add_client, edit_client = self.client_for(self.teacher), self.client_for(self.teacher)
        add_url = reverse("exams:exam_add_questions_bulk" if mode == "bulk" else "exams:exam_add_random_questions", args=[exam.pk])
        add_data = ({"selected_questions": [question.pk]} if mode == "bulk" else
                    {"count": 1, "positive_marks": 4, "negative_marks": 1})
        edit_data = {"category": self.category.pk, "target_type": "neet", "subject": "Math", "difficulty": "medium",
                     "language": "en", "question_type": "mcq_single", "question_text": "Competing question",
                     "option_text_A": "A", "option_text_B": "B", "correct_option": "A",
                     "positive_marks": 4, "negative_marks": 1}
        acquired, attempted, release = Event(), Event(), Event()
        actor = local()
        original_exam = exam_views._lock_public_exam
        original_selection = exam_views._lock_exam_question_selection
        original_question = question_views._lock_bank_question

        def controlled_exam(pk):
            if getattr(actor, "name", "") == "add" and first == "edit":
                attempted.set()
            return original_exam(pk)

        def controlled_selection(locked_exam, ids):
            result = original_selection(locked_exam, ids)
            if getattr(actor, "name", "") == "add" and first == "add" and not getattr(actor, "held", False):
                actor.held = True; acquired.set()
                if not release.wait(timeout=12):
                    raise AssertionError("Competing question edit was not released")
            return result

        def controlled_question(pk):
            if getattr(actor, "name", "") == "edit":
                if first == "add":
                    attempted.set()
                elif not getattr(actor, "held", False):
                    result = original_question(pk); actor.held = True; acquired.set()
                    if not release.wait(timeout=12):
                        raise AssertionError("Competing builder request was not released")
                    return result
            return original_question(pk)

        def post(name, client, url, data):
            close_old_connections(); actor.name = name
            try:
                return client.post(url, data)
            finally:
                close_old_connections()

        operations = {"add": (add_client, add_url, add_data),
                      "edit": (edit_client, reverse("questions:question_edit", args=[question.pk]), edit_data)}
        second = "edit" if first == "add" else "add"
        with patch.object(exam_views, "_lock_public_exam", controlled_exam), \
             patch.object(exam_views, "_lock_exam_question_selection", controlled_selection), \
             patch.object(question_views, "_lock_bank_question", controlled_question):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(post, first, *operations[first])
                try:
                    self.assertTrue(acquired.wait(timeout=10), "first request never acquired its question lock")
                    second_future = pool.submit(post, second, *operations[second])
                    self.assertTrue(attempted.wait(timeout=10), "second request never attempted a competing lock")
                finally:
                    release.set()
                results = {first: first_future.result(timeout=20), second: second_future.result(timeout=20)}
        question.refresh_from_db()
        self.assertEqual(results["add"].status_code, 302)
        if first == "add":
            self.assertEqual(results["edit"].status_code, 200)
            self.assertIn("target_type", results["edit"].context["form"].errors)
            self.assertEqual(question.target_type, "jee_main")
            self.assertTrue(exam.questions.filter(pk=question.pk).exists())
        else:
            self.assertEqual(results["edit"].status_code, 302)
            self.assertEqual(question.target_type, "neet")
            self.assertFalse(exam.questions.filter(pk=question.pk).exists())
        self.client.force_login(self.student)
        self.assertEqual(self.client.post(start_url).status_code, 302)
        attempt = exam.attempts.get(student=self.student)
        self.assertFalse(attempt.responses.exclude(exam_question__question__target_type__in=["jee", "jee_main", "jee_advanced"]).exists())

    def test_bulk_builder_wins_before_question_target_edit(self):
        self.race_builder_against_question_target_edit("bulk", "add")

    def test_question_target_edit_wins_before_bulk_builder(self):
        self.race_builder_against_question_target_edit("bulk", "edit")

    def test_random_builder_wins_before_question_target_edit(self):
        self.race_builder_against_question_target_edit("random", "add")

    def test_question_target_edit_wins_before_random_builder(self):
        self.race_builder_against_question_target_edit("random", "edit")

    def race_exam_mutation_against_start(self, mode, first):
        from apps.exams import views as exam_views, views_candidate
        from apps.payments.services import lock_practice_user as original_lock

        exam, start_url = self.make_target("public")
        extra = Question.objects.create(category=self.category, creator=self.teacher, subject="Math",
            question_text="Second question", target_type="jee_main")
        QuestionOption.objects.create(question=extra, option_key="A", option_text="A", is_correct=True)
        QuestionOption.objects.create(question=extra, option_key="B", option_text="B")
        if mode == "remove":
            ExamQuestion.objects.create(exam=exam, question=extra, order=2, positive_marks=4, negative_marks=1)
            exam.recalculate_marks()
        work_url = {
            "add": reverse("exams:exam_add_questions_bulk", args=[exam.pk]),
            "remove": reverse("exams:exam_remove_question", args=[exam.pk, extra.pk]),
            "delete": reverse("exams:exam_delete", args=[exam.pk]),
        }[mode]
        work_data = {"selected_questions": [extra.pk]} if mode == "add" else {}
        work_client, start_client = self.client_for(self.teacher), self.client_for(self.student)
        acquired, attempted, release = Event(), Event(), Event()
        actor = local()
        original_exam = exam_views._lock_public_exam
        original_match = views_candidate.exam_matches_student

        def controlled_exam(pk):
            if getattr(actor, "name", "") == "work":
                if first == "start":
                    attempted.set()
                elif not getattr(actor, "held", False):
                    locked = original_exam(pk); actor.held = True; acquired.set()
                    if not release.wait(timeout=12):
                        raise AssertionError("Competing start was not released")
                    return locked
            return original_exam(pk)

        def controlled_match(user, locked_exam):
            if getattr(actor, "name", "") == "start" and first == "start" and not getattr(actor, "held", False):
                # Called inside the candidate's exam lock, before the attempt.
                actor.held = True; acquired.set()
                if not release.wait(timeout=12):
                    raise AssertionError("Competing exam mutation was not released")
            return original_match(user, locked_exam)

        def controlled_user(user):
            if getattr(actor, "name", "") == "start" and first == "work":
                attempted.set()
            return original_lock(user)

        def post(name, client, url, data):
            close_old_connections(); actor.name = name
            try:
                return client.post(url, data)
            finally:
                close_old_connections()

        operations = {"work": (work_client, work_url, work_data), "start": (start_client, start_url, {})}
        second = "start" if first == "work" else "work"
        with patch.object(exam_views, "_lock_public_exam", controlled_exam), \
             patch.object(views_candidate, "exam_matches_student", controlled_match), \
             patch("apps.payments.services.lock_practice_user", controlled_user):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(post, first, *operations[first])
                try:
                    self.assertTrue(acquired.wait(timeout=10), "first request never acquired the exam lock")
                    second_future = pool.submit(post, second, *operations[second])
                    self.assertTrue(attempted.wait(timeout=10), "second request never attempted the competing lock")
                finally:
                    release.set()
                results = {first: first_future.result(timeout=20), second: second_future.result(timeout=20)}
        self.assertEqual(results["work"].status_code, 302)
        if first == "work" and mode == "delete":
            self.assertEqual(results["start"].status_code, 404)
            self.assertFalse(Exam.objects.filter(pk=exam.pk).exists())
            return
        self.assertEqual(results["start"].status_code, 302)
        self.assertTrue(Exam.objects.filter(pk=exam.pk).exists())
        attempt = exam.attempts.get(student=self.student)
        self.assertEqual(attempt.expires_at - attempt.started_at, timedelta(minutes=30))
        expected = 2 if (mode == "add" and first == "work") or (mode == "remove" and first == "start") else 1
        self.assertEqual(attempt.responses.count(), expected)
        self.assertEqual(exam.exam_questions.count(), expected)

    def test_start_wins_before_question_addition(self):
        self.race_exam_mutation_against_start("add", "start")

    def test_question_addition_wins_before_start(self):
        self.race_exam_mutation_against_start("add", "work")

    def test_start_wins_before_question_removal(self):
        self.race_exam_mutation_against_start("remove", "start")

    def test_question_removal_wins_before_start(self):
        self.race_exam_mutation_against_start("remove", "work")

    def test_start_wins_before_exam_deletion(self):
        self.race_exam_mutation_against_start("delete", "start")

    def test_exam_deletion_wins_before_start(self):
        self.race_exam_mutation_against_start("delete", "work")

    def race_owned_paper_against_profile(self, mode, first):
        from apps.payments.services import lock_practice_user as original_lock

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=os.path.join(temporary.name, "media"),
                                           PRIVATE_PRACTICE_ROOT=os.path.join(temporary.name, "private")))
        paper = QuestionPaperUpload.objects.create(title="Old private source", category=self.category,
            uploaded_by=self.student, is_student_private=True, status="extracted",
            target_type="jee_main" if mode == "delete" else "",
            private_pdf_file=SimpleUploadedFile("old.pdf", b"original private source"))
        filename = paper.private_pdf_file.path
        work_client, profile_client = self.client_for(self.student), self.client_for(self.student)
        work_url = reverse("questions:practice_delete" if mode == "delete" else "questions:paper_set_target", args=[paper.pk])
        work_data = {} if mode == "delete" else {"target_type": "jee_main"}
        profile_data = {"first_name": "Race", "last_name": "Student", "email": self.student.email,
                        "target_exam": "NEET", "phone": "", "institute_name": "", "bio": ""}
        acquired, attempted, release = Event(), Event(), Event()
        actor = local()

        def controlled_lock(user):
            name = getattr(actor, "name", "")
            if name == first and not getattr(actor, "held", False):
                locked = original_lock(user)
                actor.held = True
                acquired.set()
                if not release.wait(timeout=12):
                    raise AssertionError("Competing paper/profile request was not released")
                return locked
            if name and name != first:
                attempted.set()
            return original_lock(user)

        def post(name, client, url, data):
            close_old_connections(); actor.name = name
            try:
                return client.post(url, data)
            finally:
                close_old_connections()

        operations = {"work": (work_client, work_url, work_data),
                      "profile": (profile_client, reverse("accounts:profile"), profile_data)}
        second = "profile" if first == "work" else "work"
        with patch("apps.payments.services.lock_practice_user", controlled_lock), \
             patch("apps.questions.practice_views.lock_practice_user", controlled_lock):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(post, first, *operations[first])
                try:
                    self.assertTrue(acquired.wait(timeout=10), "first request never acquired the user lock")
                    second_future = pool.submit(post, second, *operations[second])
                    self.assertTrue(attempted.wait(timeout=10), "second request never attempted the user lock")
                finally:
                    release.set()
                results = {first: first_future.result(timeout=20), second: second_future.result(timeout=20)}
        self.student.refresh_from_db()
        self.assertEqual((self.student.exam_track, self.student.target_exam), ("neet", "Previous label"))
        self.assertEqual(results["profile"].status_code, 302)
        if first == "profile":
            self.assertEqual(results["work"].status_code, 404)
            paper.refresh_from_db()
            self.assertEqual(paper.target_type, "jee_main" if mode == "delete" else "")
            self.assertTrue(os.path.exists(filename))
        elif mode == "delete":
            self.assertEqual(results["work"].status_code, 302)
            self.assertFalse(QuestionPaperUpload.objects.filter(pk=paper.pk).exists())
            self.assertFalse(os.path.exists(filename))
        else:
            self.assertEqual(results["work"].status_code, 302)
            paper.refresh_from_db(); self.assertEqual(paper.target_type, "jee_main")
            self.assertTrue(os.path.exists(filename))
            self.client.force_login(self.student)
            self.assertNotContains(self.client.get(reverse("questions:practice_list")), "Old private source")

    def race_public_paper_classification_against_exam_edit(self, first):
        from apps.exams import views as exam_views
        from apps.exams.forms import ExamForm
        from apps.questions import target_views

        paper = QuestionPaperUpload.objects.create(title="Public race source", category=self.category,
                                                    uploaded_by=self.teacher, status="extracted")
        question = Question.objects.create(category=self.category, creator=self.teacher, source_paper=paper,
                                           subject="Math", question_text="Old unclassified question")
        exam = Exam.objects.create(title="Old public exam", slug="old-public-exam", category=self.category,
                                   creator=self.teacher, target_type="", status="draft", duration_minutes=30)
        ExamQuestion.objects.create(exam=exam, question=question, order=1)
        edit_data = {"title": exam.title, "category": self.category.pk, "target_type": "neet",
                     "duration_minutes": 30, "total_marks": 4, "passing_percentage": 40,
                     "default_positive_marks": 4, "default_negative_marks": 1,
                     "max_attempts": 1, "status": "draft"}
        classify_client, edit_client = self.client_for(self.teacher), self.client_for(self.teacher)
        acquired, attempted, release = Event(), Event(), Event()
        actor = local()
        original_classify = target_views.PaperTargetView._classify
        original_active = target_views._live_attempts
        original_exam = exam_views._lock_public_exam
        original_author = exam_views.lock_active_teacher
        original_clean = ExamForm.clean_target_type

        def controlled_classify(view, request, pk):
            if getattr(actor, "name", "") == "classify" and first == "edit":
                attempted.set()
            return original_classify(view, request, pk)

        def controlled_active(queryset):
            if getattr(actor, "name", "") == "classify" and first == "classify":
                acquired.set()
                if not release.wait(timeout=12):
                    raise AssertionError("Competing exam edit was not released")
            return original_active(queryset)

        def controlled_author(user):
            if getattr(actor, "name", "") == "edit" and first == "classify":
                # SQLite may wait for the writer here, before reaching the
                # exam lock. PostgreSQL continues to the shared exam lock.
                attempted.set()
            return original_author(user)

        def controlled_exam(pk):
            return original_exam(pk)

        def controlled_clean(form):
            if getattr(actor, "name", "") == "edit" and first == "edit":
                acquired.set()
                if not release.wait(timeout=12):
                    raise AssertionError("Competing paper classification was not released")
            return original_clean(form)

        def post(name, client, url, data):
            close_old_connections(); actor.name = name
            try:
                return client.post(url, data)
            finally:
                close_old_connections()

        operations = {
            "classify": (classify_client, reverse("questions:paper_set_target", args=[paper.pk]), {"target_type": "jee_main"}),
            "edit": (edit_client, reverse("exams:exam_edit", args=[exam.pk]), edit_data),
        }
        second = "edit" if first == "classify" else "classify"
        with patch.object(target_views.PaperTargetView, "_classify", controlled_classify), \
             patch.object(target_views, "_live_attempts", controlled_active), \
             patch.object(exam_views, "lock_active_teacher", controlled_author), \
             patch.object(exam_views, "_lock_public_exam", controlled_exam), \
             patch.object(ExamForm, "clean_target_type", controlled_clean):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(post, first, *operations[first])
                try:
                    self.assertTrue(acquired.wait(timeout=10), "first request never acquired its locks")
                    second_future = pool.submit(post, second, *operations[second])
                    self.assertTrue(attempted.wait(timeout=10), "second request never attempted its lock")
                finally:
                    release.set()
                results = {first: first_future.result(timeout=20), second: second_future.result(timeout=20)}
        exam.refresh_from_db(); paper.refresh_from_db(); question.refresh_from_db()
        self.assertEqual(results["classify"].status_code, 302)
        self.assertEqual(paper.target_type, "jee_main")
        if first == "classify":
            self.assertEqual(results["edit"].status_code, 200)
            self.assertIn("target_type", results["edit"].context["form"].errors)
            self.assertEqual((exam.target_type, question.target_type), ("", "jee_main"))
        else:
            self.assertEqual(results["edit"].status_code, 302)
            self.assertEqual((exam.target_type, question.target_type), ("neet", ""))

    def test_public_paper_classification_wins_before_exam_target_edit(self):
        self.race_public_paper_classification_against_exam_edit("classify")

    def test_exam_target_edit_wins_before_public_paper_classification(self):
        self.race_public_paper_classification_against_exam_edit("edit")

    def test_paper_delete_wins_before_track_change(self):
        self.race_owned_paper_against_profile("delete", "work")

    def test_track_change_wins_before_paper_delete(self):
        self.race_owned_paper_against_profile("delete", "profile")

    def test_paper_classification_wins_before_track_change(self):
        self.race_owned_paper_against_profile("classify", "work")

    def test_track_change_wins_before_paper_classification(self):
        self.race_owned_paper_against_profile("classify", "profile")
