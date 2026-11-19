"""JEE/NEET target enforcement, direct access and legacy recovery."""
import os
import tempfile
from datetime import timedelta

from django.contrib import admin
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.models import (Category, ClassroomParticipant, ClassroomSession, Exam,
                              ExamAttempt, ExamQuestion, ExamResponse)
from apps.questions.models import (ExtractedFigure, ExtractedQuestion, Question, QuestionFigure,
                                   QuestionOption, QuestionPaperUpload)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ExamTargetAccessTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=os.path.join(self.root.name, "media"),
                                           PRIVATE_PRACTICE_ROOT=os.path.join(self.root.name, "private")))
        self.teacher = User.objects.create_user("target_teacher", email="target-t@example.test", role="TEACHER")
        self.other_teacher = User.objects.create_user("other_target_teacher", email="target-t2@example.test", role="TEACHER")
        self.jee = User.objects.create_user("target_jee", email="target-j@example.test", role="STUDENT", exam_track="jee", target_exam="Old free-text label")
        self.neet = User.objects.create_user("target_neet", email="target-n@example.test", role="STUDENT", exam_track="neet")
        self.unknown = User.objects.create_user("target_unknown", email="target-u@example.test", role="STUDENT", target_exam="Ambiguous old label")
        self.admin = User.objects.create_superuser("target_admin", "target-a@example.test", "x")
        self.category = Category.objects.create(name="Target test category", slug="target-test-category")
        self.teacher.assigned_categories.add(self.category)

    def make_question(self, label, target, **kwargs):
        q = Question.objects.create(category=self.category, creator=kwargs.pop("creator", self.teacher),
                                    subject="Physics", question_text=f"{label} question", target_type=target, **kwargs)
        QuestionOption.objects.create(question=q, option_key="A", option_text="Yes", is_correct=True)
        QuestionOption.objects.create(question=q, option_key="B", option_text="No")
        return q

    def make_exam(self, label, target, question_target=None, **kwargs):
        exam = Exam.objects.create(title=label, slug=label.lower().replace(" ", "-"), category=self.category,
                                   creator=kwargs.pop("creator", self.teacher), target_type=target,
                                   status=kwargs.pop("status", "published"), is_free=True, duration_minutes=30,
                                   shuffle_questions=False, **kwargs)
        q_target = target if question_target is None else question_target
        q = self.make_question(label, q_target, creator=exam.creator,
                               is_private_practice=exam.is_private_practice,
                               is_classroom_snapshot=exam.is_classroom_only)
        eq = ExamQuestion.objects.create(exam=exam, question=q, order=1, positive_marks=4, negative_marks=1)
        exam.recalculate_marks()
        return exam, q, eq

    def active_attempt(self, exam, user, eq):
        attempt = ExamAttempt.objects.create(exam=exam, student=user,
            expires_at=timezone.now() + timedelta(minutes=23), total_marks=4, last_active_question=eq)
        ExamResponse.objects.create(attempt=attempt, exam_question=eq, order=1, is_visited=True)
        return attempt

    def test_registration_requires_jee_or_neet_for_students_but_not_teachers(self):
        base = {"first_name": "A", "last_name": "Student", "username": "registration_target",
                "email": "registration-target@example.test", "password": "StrongPass123!", "confirm_password": "StrongPass123!"}
        for role in ("STUDENT", ""):
            for target in ("", "UPSC", "jee_main"):
                with self.subTest(role=role, target=target):
                    response = self.client.post(reverse("accounts:register"), dict(base, role=role, target_exam=target))
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("target_exam", response.context["form"].errors)
                    self.assertFalse(User.objects.filter(username=base["username"]).exists())
        response = self.client.post(reverse("accounts:register"), dict(base, role="STUDENT", target_exam="NEET"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.get(username=base["username"]).exam_track, "neet")
        self.client.logout()
        teacher = dict(base, username="registration_teacher", email="registration-teacher@example.test", role="TEACHER")
        self.assertEqual(self.client.post(reverse("accounts:register"), teacher).status_code, 302)
        self.assertEqual(User.objects.get(username="registration_teacher").exam_track, "")

    def test_catalog_dashboard_instructions_start_and_leaderboard_fail_closed(self):
        main, _, _ = self.make_exam("Main access", "jee_main")
        advanced, _, _ = self.make_exam("Advanced access", "jee_advanced")
        general, _, _ = self.make_exam("JEE access", "jee")
        neet, _, _ = self.make_exam("NEET access", "neet")
        unknown, _, _ = self.make_exam("Unknown access", "")
        mixed, _, _ = self.make_exam("Mixed access", "jee", "neet")
        missing, _, _ = self.make_exam("Missing classification", "jee", "")
        for user, visible, hidden in (
            (self.jee, [main, advanced, general], [neet, unknown, mixed, missing]),
            (self.neet, [neet], [main, advanced, general, unknown, mixed, missing]),
            (self.unknown, [], [main, advanced, general, neet, unknown, mixed, missing]),
        ):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                catalog = self.client.get(reverse("exams:exam_catalog"))
                self.assertEqual(set(catalog.context["exams"]), set(visible))
                dashboard = self.client.get(reverse("core:student_dashboard"))
                self.assertEqual(set(dashboard.context["available_exams"]), set(visible))
                for exam in hidden:
                    for view in ("exams:exam_instructions", "exams:exam_leaderboard"):
                        self.assertEqual(self.client.get(reverse(view, args=[exam.slug])).status_code, 404, (user, exam, view))
                    self.assertEqual(self.client.post(reverse("exams:exam_start", args=[exam.slug])).status_code, 404)
                for exam in visible:
                    self.assertEqual(self.client.get(reverse("exams:exam_instructions", args=[exam.slug])).status_code, 200)
                    self.assertEqual(self.client.get(reverse("exams:exam_leaderboard", args=[exam.slug])).status_code, 200)
        self.assertFalse(ExamAttempt.objects.exists())

    def test_classroom_join_code_and_take_cannot_cross_tracks(self):
        main, _, _ = self.make_exam("JEE classroom", "jee_main", is_classroom_only=True)
        neet, _, _ = self.make_exam("NEET classroom", "neet", is_classroom_only=True)
        session = ClassroomSession.create_for(main, self.teacher, show_leaderboard_to_students=True)
        neet_session = ClassroomSession.create_for(neet, self.teacher)
        self.client.force_login(self.neet)
        response = self.client.post(reverse("exams:classroom_join"), {"code": session.code}, follow=True)
        self.assertContains(response, "not available for your selected exam")
        self.assertFalse(session.participants.filter(student=self.neet).exists())
        # A legacy membership must not bypass direct take or leaderboard URLs.
        ClassroomParticipant.objects.create(session=session, student=self.neet)
        self.assertEqual(self.client.get(reverse("exams:classroom_take", args=[session.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("exams:classroom_take", args=[session.pk])).status_code, 404)
        self.assertIn(self.client.get(reverse("exams:classroom_leaderboard", args=[session.pk])).status_code, (403, 404))
        self.assertRedirects(self.client.post(reverse("exams:classroom_join"), {"code": neet_session.code}),
                             reverse("exams:classroom_take", args=[neet_session.pk]), fetch_redirect_response=False)
        self.assertEqual(self.client.post(reverse("exams:classroom_take", args=[neet_session.pk])).status_code, 302)
        self.assertEqual(neet_session.attempts.filter(student=self.neet).count(), 1)

    def test_teacher_builder_rejects_forged_opposite_or_unclassified_questions(self):
        exam, _, _ = self.make_exam("Teacher draft", "jee_main", status="draft")
        matching = self.make_question("Matching teacher", "jee_advanced")
        wrong = self.make_question("Wrong teacher", "neet")
        unknown = self.make_question("Unknown teacher", "")
        self.client.force_login(self.teacher)
        builder = self.client.get(reverse("exams:exam_questions", args=[exam.pk]))
        available = set(builder.context["available_questions"])
        self.assertIn(matching, available)
        self.assertNotIn(wrong, available)
        self.assertNotIn(unknown, available)
        self.client.post(reverse("exams:exam_add_questions_bulk", args=[exam.pk]),
                         {"selected_questions": [matching.pk, wrong.pk, unknown.pk]})
        self.assertTrue(exam.questions.filter(pk=matching.pk).exists())
        self.assertFalse(exam.questions.filter(pk__in=[wrong.pk, unknown.pk]).exists())
        # Even an old question already attached to a draft cannot be published.
        ExamQuestion.objects.create(exam=exam, question=unknown, order=3, positive_marks=4, negative_marks=1)
        self.client.post(reverse("exams:exam_toggle_publish", args=[exam.pk]))
        exam.refresh_from_db()
        self.assertEqual(exam.status, "draft")

    def test_owned_opposite_papers_bank_and_direct_assets_are_hidden(self):
        self.client.force_login(self.jee)
        paper = QuestionPaperUpload.objects.create(title="Secret NEET source", uploaded_by=self.jee,
            category=self.category, is_student_private=True, target_type="neet", status="extracted",
            private_pdf_file=SimpleUploadedFile("secret.pdf", b"private"))
        staged = ExtractedQuestion.objects.create(paper=paper, question_number=1, question_text="Old NEET")
        figure = ExtractedFigure.objects.create(paper=paper, question=staged,
            private_image=SimpleUploadedFile("secret.png", b"image"))
        q = self.make_question("Private NEET", "neet", creator=self.jee, is_private_practice=True,
                               private_image=SimpleUploadedFile("q.png", b"image"))
        bank_figure = QuestionFigure.objects.create(question=q, private_image=SimpleUploadedFile("fig.png", b"image"))
        self.assertNotContains(self.client.get(reverse("questions:practice_list")), "Secret NEET source")
        self.assertNotContains(self.client.get(reverse("questions:practice_bank")), "Private NEET question")
        for view, pk in (("questions:practice_settings", paper.pk), ("questions:practice_pdf", paper.pk),
                         ("questions:practice_question_edit", staged.pk), ("questions:practice_extracted_image", staged.pk),
                         ("questions:practice_figure_file", figure.pk), ("questions:practice_bank_edit", q.pk),
                         ("questions:practice_bank_image", q.pk), ("questions:practice_bank_figure", bank_figure.pk)):
            with self.subTest(view=view):
                self.assertEqual(self.client.get(reverse(view, args=[pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("questions:practice_delete", args=[paper.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("questions:practice_answer_image_preview", args=[paper.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("questions:practice_bank_test"),
            {"title": "Forged test", "duration_minutes": 20, "question_ids": [q.pk]}).status_code, 404)
        self.assertTrue(QuestionPaperUpload.objects.filter(pk=paper.pk).exists())

    def test_old_result_images_require_an_owned_attempt_after_track_change(self):
        exam, question, eq = self.make_exam("Private old NEET", "neet", creator=self.jee, is_private_practice=True)
        question.private_image.save("old.png", SimpleUploadedFile("old.png", b"old image"))
        attempt = ExamAttempt.objects.create(exam=exam, student=self.jee, status="submitted", score_obtained=4)
        ExamResponse.objects.create(attempt=attempt, exam_question=eq, order=1, is_correct=True, marks_awarded=4)
        self.client.force_login(self.jee)
        image = reverse("questions:practice_bank_image", args=[question.pk])
        self.assertEqual(self.client.get(image).status_code, 404)
        response = self.client.get(image, {"attempt": str(attempt.attempt_uuid)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"old image")
        page = self.client.get(reverse("questions:practice_result", args=[attempt.attempt_uuid]))
        self.assertContains(page, f"?attempt={attempt.attempt_uuid}")
        self.client.force_login(self.neet)
        self.assertEqual(self.client.get(image, {"attempt": str(attempt.attempt_uuid)}).status_code, 404)

    def test_legacy_active_attempt_can_resume_without_reset_but_cannot_restart(self):
        exam, _, eq = self.make_exam("Legacy active", "neet", "")
        attempt = self.active_attempt(exam, self.jee, eq)
        expiry = attempt.expires_at
        self.client.force_login(self.jee)
        self.assertEqual(self.client.get(reverse("exams:exam_instructions", args=[exam.slug])).status_code, 200)
        self.assertRedirects(self.client.post(reverse("exams:exam_start", args=[exam.slug])),
                             reverse("exams:exam_session", args=[attempt.attempt_uuid]), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid])).status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.expires_at, expiry)
        attempt.status = "submitted"
        attempt.save(update_fields=["status"])
        self.assertEqual(self.client.post(reverse("exams:exam_start", args=[exam.slug])).status_code, 404)
        self.assertEqual(exam.attempts.count(), 1)

    def test_legacy_paper_and_test_classification_requires_owner_and_confirmation(self):
        paper = QuestionPaperUpload.objects.create(title="Unknown old paper", uploaded_by=self.jee,
            category=self.category, target_exam="Old custom value", is_student_private=True, status="extracted")
        exam, question, _ = self.make_exam("Old bank test", "", creator=self.jee, is_private_practice=True)
        question.source_paper = paper
        question.save(update_fields=["source_paper"])
        url = reverse("questions:paper_set_target", args=[paper.pk])
        self.client.force_login(self.neet)
        self.assertEqual(self.client.post(url, {"target_type": "neet"}).status_code, 404)
        self.client.force_login(self.jee)
        self.assertNotContains(self.client.get(reverse("questions:practice_list")), "Start test")
        self.assertEqual(self.client.post(url, {"target_type": "jee_main"}).status_code, 302)
        paper.refresh_from_db(); question.refresh_from_db(); exam.refresh_from_db()
        self.assertEqual((paper.target_type, paper.target_exam, question.target_type, exam.target_type),
                         ("jee_main", "Old custom value", "jee_main", ""))
        test_url = reverse("questions:practice_test_set_target", args=[exam.pk])
        self.client.post(test_url, {"target_type": "jee_main"})
        exam.refresh_from_db(); self.assertEqual(exam.target_type, "")
        self.client.post(test_url, {"target_type": "jee_main", "confirm_questions": "on"})
        exam.refresh_from_db(); self.assertEqual(exam.target_type, "jee_main")
        self.assertEqual(self.client.get(reverse("exams:exam_instructions", args=[exam.slug])).status_code, 200)
        # Already-classified paper cannot be overwritten even by its owner.
        self.client.post(url, {"target_type": "jee_advanced"})
        paper.refresh_from_db(); self.assertEqual(paper.target_type, "jee_main")

    def test_legacy_test_conflicts_do_not_relabel_shared_questions(self):
        exam, question, _ = self.make_exam("Old mixed bank", "", creator=self.jee, is_private_practice=True)
        other = Exam.objects.create(title="Different tagged test", slug="different-tagged-test", category=self.category,
                                    creator=self.jee, target_type="neet", is_private_practice=True)
        ExamQuestion.objects.create(exam=other, question=question, order=1)
        self.client.force_login(self.jee)
        response = self.client.post(reverse("questions:practice_test_set_target", args=[exam.pk]),
                                    {"target_type": "jee", "confirm_questions": "on"}, follow=True)
        self.assertContains(response, "conflict with another test")
        question.refresh_from_db(); exam.refresh_from_db()
        self.assertEqual((question.target_type, exam.target_type), ("", ""))

    def test_legacy_question_target_cannot_change_during_a_live_attempt(self):
        exam, question, eq = self.make_exam("Legacy question active", "", "")
        attempt = self.active_attempt(exam, self.jee, eq)
        deadline = attempt.expires_at
        self.client.force_login(self.teacher)
        data = {"category": self.category.pk, "target_type": "neet", "subject": "Physics",
                "difficulty": "medium", "language": "en", "question_type": "mcq_single",
                "question_text": question.question_text, "option_text_A": "Yes", "option_text_B": "No",
                "correct_option": "A", "positive_marks": 4, "negative_marks": 1}
        url = reverse("questions:question_edit", args=[question.pk])
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("target_type", response.context["form"].errors)
        question.refresh_from_db(); attempt.refresh_from_db()
        self.assertEqual((question.target_type, attempt.expires_at), ("", deadline))
        attempt.status = "submitted"; attempt.save(update_fields=["status"])
        self.assertEqual(self.client.post(url, dict(data, target_type="jee_main")).status_code, 302)
        question.refresh_from_db(); self.assertEqual(question.target_type, "jee_main")

    def test_django_admin_cannot_bypass_target_edit_guards(self):
        exam, question, _ = self.make_exam("Admin guarded exam", "jee_main")
        paper = QuestionPaperUpload.objects.create(title="Admin guarded paper", uploaded_by=self.teacher,
                                                   category=self.category, target_type="jee_main", status="extracted")
        request = RequestFactory().get("/admin/")
        request.user = self.admin
        for instance, field in ((self.jee, "exam_track"), (exam, "target_type"),
                                (question, "target_type"), (paper, "target_type")):
            with self.subTest(model=instance.__class__.__name__):
                model_admin = admin.site._registry[instance.__class__]
                self.assertIn(field, model_admin.get_readonly_fields(request, instance))
                self.assertNotIn(field, model_admin.get_form(request, instance).base_fields)

    def test_track_edits_preserve_legacy_text_and_are_blocked_during_active_tests(self):
        exam, _, eq = self.make_exam("Active during edit", "jee")
        attempt = self.active_attempt(exam, self.jee, eq)
        expiry = attempt.expires_at
        profile = {"first_name": "Edited", "last_name": "Student", "email": self.jee.email,
                   "target_exam": "NEET", "phone": "", "institute_name": "", "bio": ""}
        self.client.force_login(self.jee)
        result = self.client.post(reverse("accounts:profile"), profile)
        self.assertEqual(result.status_code, 200)
        self.assertIn("target_exam", result.context["form"].errors)
        self.jee.refresh_from_db(); attempt.refresh_from_db()
        self.assertEqual((self.jee.exam_track, self.jee.target_exam, attempt.expires_at), ("jee", "Old free-text label", expiry))
        self.client.force_login(self.admin)
        admin_data = dict(profile, is_active="on", is_verified="on")
        result = self.client.post(reverse("accounts:student_edit", args=[self.jee.pk]), admin_data)
        self.assertEqual(result.status_code, 200)
        self.assertIn("target_exam", result.context["form"].errors)
        attempt.status = "submitted"; attempt.save(update_fields=["status"])
        self.assertEqual(self.client.post(reverse("accounts:student_edit", args=[self.jee.pk]), admin_data).status_code, 302)
        self.jee.refresh_from_db()
        self.assertEqual((self.jee.exam_track, self.jee.target_exam), ("neet", "Old free-text label"))
        self.client.force_login(self.jee)
        self.assertEqual(self.client.post(reverse("accounts:profile"), dict(profile, target_exam="JEE")).status_code, 302)
        self.jee.refresh_from_db()
        self.assertEqual((self.jee.exam_track, self.jee.target_exam), ("jee", "Old free-text label"))
