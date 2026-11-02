import json
from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from apps.accounts.models import User
from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion, ExamResponse, ExamSecurityLog
from apps.questions.models import Question, QuestionOption


class CategoryManagementTests(TestCase):
    def setUp(self):
        self.super_admin = User.objects.create_superuser(
            username="test_super",
            email="super@test.com",
            password="Password123!",
            role=User.Roles.SUPER_ADMIN,
        )
        self.admin = User.objects.create_user(
            username="test_admin",
            email="admin@test.com",
            password="Password123!",
            role=User.Roles.ADMIN,
            is_staff=True,
        )
        self.teacher = User.objects.create_user(
            username="test_teacher",
            email="teacher@test.com",
            password="Password123!",
            role=User.Roles.TEACHER,
            is_staff=True,
        )
        self.student = User.objects.create_user(
            username="test_student",
            email="student@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
        )
        self.cat1 = Category.objects.create(
            name="UPSC Civil Services",
            slug="upsc-civil-services",
            description="National competitive exam",
            is_active=True,
            display_order=1,
        )

    def test_category_list_accessible_by_admin_and_super_admin(self):
        # Admin access
        self.client.login(username="test_admin", password="Password123!")
        res = self.client.get(reverse("exams:category_list"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "UPSC Civil Services")
        self.client.logout()

        # Super admin access
        self.client.login(username="test_super", password="Password123!")
        res_super = self.client.get(reverse("exams:category_list"))
        self.assertEqual(res_super.status_code, 200)
        self.client.logout()

    def test_category_list_forbidden_for_student_and_teacher(self):
        # Student access rejected (403 PermissionDenied)
        self.client.login(username="test_student", password="Password123!")
        res_student = self.client.get(reverse("exams:category_list"))
        self.assertEqual(res_student.status_code, 403)
        self.client.logout()

        # Teacher access rejected (403 PermissionDenied)
        self.client.login(username="test_teacher", password="Password123!")
        res_teacher = self.client.get(reverse("exams:category_list"))
        self.assertEqual(res_teacher.status_code, 403)
        self.client.logout()

    def test_category_creation_by_admin(self):
        self.client.login(username="test_admin", password="Password123!")
        res = self.client.post(
            reverse("exams:category_create"),
            {
                "name": "State PSC",
                "slug": "state-psc",
                "description": "State commission exams",
                "icon": "bi-trophy",
                "display_order": 2,
                "is_active": True,
            },
        )
        self.assertEqual(res.status_code, 302)
        self.assertTrue(Category.objects.filter(slug="state-psc").exists())

    def test_category_duplicate_slug_rejected(self):
        self.client.login(username="test_admin", password="Password123!")
        res = self.client.post(
            reverse("exams:category_create"),
            {
                "name": "Another UPSC",
                "slug": "upsc-civil-services",  # Duplicate
                "description": "Another",
                "icon": "bi-trophy",
                "display_order": 5,
                "is_active": True,
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertFormError(
            res.context["form"], "slug", "A category with this URL slug already exists."
        )

    def test_category_toggle_status(self):
        self.client.login(username="test_admin", password="Password123!")
        self.assertTrue(self.cat1.is_active)

        res = self.client.post(reverse("exams:category_toggle_status", args=[self.cat1.pk]))
        self.assertEqual(res.status_code, 302)
        self.cat1.refresh_from_db()
        self.assertFalse(self.cat1.is_active)

    def test_category_deletion_prevented_when_exams_exist(self):
        # Associate an exam with cat1
        Exam.objects.create(
            title="UPSC Prelims Paper 1",
            slug="upsc-prelims-paper-1",
            category=self.cat1,
            creator=self.teacher,
            status=Exam.ExamStatus.PUBLISHED,
        )

        self.client.login(username="test_admin", password="Password123!")
        res = self.client.post(reverse("exams:category_delete", args=[self.cat1.pk]))
        self.assertEqual(res.status_code, 302)
        # Category must NOT be deleted
        self.assertTrue(Category.objects.filter(pk=self.cat1.pk).exists())

    def test_category_safe_deletion_when_no_exams(self):
        empty_cat = Category.objects.create(
            name="Empty Subject",
            slug="empty-subject",
            is_active=True,
        )
        self.client.login(username="test_admin", password="Password123!")
        res = self.client.post(reverse("exams:category_delete", args=[empty_cat.pk]))
        self.assertEqual(res.status_code, 302)
        self.assertFalse(Category.objects.filter(pk=empty_cat.pk).exists())


class ExamCreationAndManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="admin_examiner",
            email="admin_ex@test.com",
            password="Password123!",
            role=User.Roles.SUPER_ADMIN,
        )
        self.teacher = User.objects.create_user(
            username="math_teacher",
            email="math@test.com",
            password="Password123!",
            role=User.Roles.TEACHER,
            is_staff=True,
        )
        self.other_teacher = User.objects.create_user(
            username="history_teacher",
            email="history@test.com",
            password="Password123!",
            role=User.Roles.TEACHER,
            is_staff=True,
        )
        self.student = User.objects.create_user(
            username="exam_candidate",
            email="candidate@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
        )

        self.category = Category.objects.create(
            name="JEE Advanced Science",
            slug="jee-advanced-science",
            is_active=True,
        )
        self.teacher.assigned_categories.add(self.category)

        # Create sample Question Bank items
        from apps.questions.models import Question, QuestionOption

        self.questions = []
        for i in range(1, 6):
            q = Question.objects.create(
                category=self.category,
                subject="Physics",
                topic="Mechanics",
                difficulty=Question.DifficultyLevel.MEDIUM,
                question_text=f"Question statement #{i}: Find kinetic energy $K = \\frac{{1}}{{2}}mv^2$.",
                positive_marks=4.00,
                negative_marks=1.00,
                creator=self.teacher,
            )
            for key, is_corr in [("A", True), ("B", False), ("C", False), ("D", False)]:
                QuestionOption.objects.create(
                    question=q,
                    option_key=key,
                    option_text=f"Option {key} for question {i}",
                    is_correct=is_corr,
                )
            self.questions.append(q)

        # Create a sample exam in draft mode
        self.exam = Exam.objects.create(
            title="JEE Advanced Physics Mock 1",
            category=self.category,
            creator=self.teacher,
            status=Exam.ExamStatus.DRAFT,
            subject="Physics",
            duration_minutes=90,
            default_positive_marks=4.00,
            default_negative_marks=1.00,
            total_marks=0.00,
        )

    def test_student_forbidden_from_exam_management(self):
        self.client.login(username="exam_candidate", password="Password123!")

        # Exam list
        res_list = self.client.get(reverse("exams:exam_list"))
        self.assertEqual(res_list.status_code, 403)

        # Exam create
        res_create = self.client.get(reverse("exams:exam_create"))
        self.assertEqual(res_create.status_code, 403)

        # Question builder
        res_builder = self.client.get(reverse("exams:exam_questions", args=[self.exam.pk]))
        self.assertEqual(res_builder.status_code, 403)

    def test_teacher_can_create_exam(self):
        self.client.login(username="math_teacher", password="Password123!")
        res = self.client.post(
            reverse("exams:exam_create"),
            {
                "title": "Calculus Term Test 2026",
                "category": self.category.pk,
                "subject": "Mathematics",
                "duration_minutes": 60,
                "total_marks": 100,
                "passing_percentage": 40,
                "default_positive_marks": 4.00,
                "default_negative_marks": 1.00,
                "max_attempts": 1,
                "status": Exam.ExamStatus.DRAFT,
                "is_free": True,
            },
        )
        self.assertEqual(res.status_code, 302)
        created_exam = Exam.objects.get(title="Calculus Term Test 2026")
        self.assertEqual(created_exam.creator, self.teacher)
        self.assertEqual(created_exam.duration_minutes, 60)
        self.assertTrue(created_exam.is_free)

    def test_exam_add_questions_from_bank_and_recalculates_marks(self):
        self.client.login(username="math_teacher", password="Password123!")
        q_ids = [self.questions[0].pk, self.questions[1].pk, self.questions[2].pk]

        res = self.client.post(
            reverse("exams:exam_add_questions_bulk", args=[self.exam.pk]),
            {"selected_questions": q_ids, "section": "Section A"},
        )
        self.assertEqual(res.status_code, 302)

        self.exam.refresh_from_db()
        self.assertEqual(self.exam.total_questions_count, 3)
        # 3 questions * 4.00 = 12.00 total marks
        self.assertEqual(self.exam.total_marks, 12.00)

    def test_exam_add_random_questions(self):
        self.client.login(username="math_teacher", password="Password123!")
        res = self.client.post(
            reverse("exams:exam_add_random_questions", args=[self.exam.pk]),
            {
                "count": 2,
                "difficulty": "medium",
                "subject": "Physics",
                "section": "Section B",
                "positive_marks": "4.00",
                "negative_marks": "1.00",
            },
        )
        self.assertEqual(res.status_code, 302)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.total_questions_count, 2)
        self.assertEqual(self.exam.total_marks, 8.00)

    def test_exam_remove_question(self):
        self.client.login(username="math_teacher", password="Password123!")
        # Add 2 questions
        self.client.post(
            reverse("exams:exam_add_questions_bulk", args=[self.exam.pk]),
            {"selected_questions": [self.questions[0].pk, self.questions[1].pk]},
        )
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.total_questions_count, 2)

        # Remove 1 question
        res_del = self.client.post(
            reverse("exams:exam_remove_question", args=[self.exam.pk, self.questions[0].pk])
        )
        self.assertEqual(res_del.status_code, 302)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.total_questions_count, 1)
        self.assertEqual(self.exam.total_marks, 4.00)

    def test_publish_validation_requires_questions(self):
        self.client.login(username="math_teacher", password="Password123!")
        # exam currently has 0 questions
        self.assertEqual(self.exam.total_questions_count, 0)

        res = self.client.post(reverse("exams:exam_toggle_publish", args=[self.exam.pk]))
        self.assertEqual(res.status_code, 302)

        self.exam.refresh_from_db()
        # Must stay in DRAFT
        self.assertEqual(self.exam.status, Exam.ExamStatus.DRAFT)

    def test_publish_and_unpublish_flow(self):
        self.client.login(username="math_teacher", password="Password123!")
        # Add question first
        self.client.post(
            reverse("exams:exam_add_questions_bulk", args=[self.exam.pk]),
            {"selected_questions": [self.questions[0].pk]},
        )

        # Publish
        res_pub = self.client.post(reverse("exams:exam_toggle_publish", args=[self.exam.pk]))
        self.assertEqual(res_pub.status_code, 302)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.status, Exam.ExamStatus.PUBLISHED)

        # Unpublish back to Draft
        res_unpub = self.client.post(reverse("exams:exam_toggle_publish", args=[self.exam.pk]))
        self.assertEqual(res_unpub.status_code, 302)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.status, Exam.ExamStatus.DRAFT)

    def test_exam_duplication_clones_questions_and_marks(self):
        self.client.login(username="math_teacher", password="Password123!")
        # Add 2 questions
        self.client.post(
            reverse("exams:exam_add_questions_bulk", args=[self.exam.pk]),
            {"selected_questions": [self.questions[0].pk, self.questions[1].pk]},
        )

        # Duplicate
        res_dup = self.client.post(reverse("exams:exam_duplicate", args=[self.exam.pk]))
        self.assertEqual(res_dup.status_code, 302)

        # Verify cloned exam exists
        cloned_exam = Exam.objects.filter(title__startswith="Copy of").first()
        self.assertIsNotNone(cloned_exam)
        self.assertEqual(cloned_exam.status, Exam.ExamStatus.DRAFT)
        self.assertEqual(cloned_exam.total_questions_count, 2)
        self.assertEqual(cloned_exam.total_marks, 8.00)

    def test_delete_prevented_when_candidate_attempts_exist(self):
        self.client.login(username="math_teacher", password="Password123!")
        # Simulate a candidate attempt
        ExamAttempt.objects.create(
            exam=self.exam,
            student=self.student,
            status=ExamAttempt.AttemptStatus.SUBMITTED,
            score_obtained=10.0,
        )
        self.assertTrue(self.exam.has_attempts)

        res_del = self.client.post(reverse("exams:exam_delete", args=[self.exam.pk]))
        self.assertEqual(res_del.status_code, 302)
        # Exam must NOT be deleted
        self.assertTrue(Exam.objects.filter(pk=self.exam.pk).exists())

    def test_exam_preview_accessible_by_teacher(self):
        self.client.login(username="math_teacher", password="Password123!")
        res = self.client.get(reverse("exams:exam_preview", args=[self.exam.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "JEE Advanced Physics Mock 1")
        self.assertContains(res, "Teacher Preview")


class CBTEngineAndExaminationTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name="Engineering Physics",
            slug="engineering-physics",
            is_active=True,
        )
        self.teacher = User.objects.create_user(
            username="physics_teacher",
            email="pteacher@test.com",
            password="Password123!",
            role=User.Roles.TEACHER,
            is_staff=True,
        )
        self.student = User.objects.create_user(
            username="cbt_candidate_1",
            email="candidate1@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
        )
        self.other_student = User.objects.create_user(
            username="cbt_candidate_2",
            email="candidate2@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
        )

        # Create published exam
        self.exam = Exam.objects.create(
            title="JEE Main Physics Grand Assessment",
            slug="jee-main-physics-grand-assessment",
            category=self.category,
            creator=self.teacher,
            status=Exam.ExamStatus.PUBLISHED,
            subject="Physics",
            duration_minutes=45,
            default_positive_marks=4.00,
            default_negative_marks=1.00,
            passing_percentage=50.0,
            total_marks=12.00,
            max_attempts=2,
            is_free=True,
            show_result_immediately=True,
        )

        # Create 3 questions with options
        self.questions = []
        for i in range(1, 4):
            q = Question.objects.create(
                category=self.category,
                subject="Physics",
                question_text=f"Sample Question {i}: Find acceleration $a = \\frac{{F}}{{m}}$.",
                positive_marks=4.00,
                negative_marks=1.00,
                creator=self.teacher,
            )
            # Create options A, B, C, D (A is always correct in test setup)
            for key, is_corr in [("A", True), ("B", False), ("C", False), ("D", False)]:
                QuestionOption.objects.create(
                    question=q,
                    option_key=key,
                    option_text=f"Option {key} for Q{i}",
                    is_correct=is_corr,
                )
            eq = ExamQuestion.objects.create(
                exam=self.exam,
                question=q,
                order=i,
                section="Mechanics",
                positive_marks=4.00,
                negative_marks=1.00,
            )
            self.questions.append((q, eq))

    def test_student_can_view_instructions(self):
        self.client.login(username="cbt_candidate_1", password="Password123!")
        res = self.client.get(reverse("exams:exam_instructions", args=[self.exam.slug]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "JEE Main Physics Grand Assessment")
        self.assertContains(res, "45 Min")
        self.assertContains(res, "General Instructions for Candidates")
        self.assertTrue(res.context["can_start"])

    def test_student_starts_exam_creates_attempt_and_responses(self):
        self.client.login(username="cbt_candidate_1", password="Password123!")
        res = self.client.post(reverse("exams:exam_start", args=[self.exam.slug]))
        self.assertEqual(res.status_code, 302)

        attempt = ExamAttempt.objects.filter(exam=self.exam, student=self.student).first()
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.IN_PROGRESS)
        self.assertIsNotNone(attempt.expires_at)
        self.assertEqual(attempt.responses.count(), 3)
        # First question is visited
        first_resp = attempt.responses.filter(order=1).first()
        self.assertTrue(first_resp.is_visited)

        # Redirects to session
        self.assertIn(str(attempt.attempt_uuid), res.url)

    def test_security_correct_answers_never_exposed_in_session(self):
        self.client.login(username="cbt_candidate_1", password="Password123!")
        self.client.post(reverse("exams:exam_start", args=[self.exam.slug]))
        attempt = ExamAttempt.objects.get(exam=self.exam, student=self.student)

        res = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(res.status_code, 200)

        # Crucial security assertion: 'is_correct' and 'explanation' must NEVER appear in session HTML or payload
        content = res.content.decode()
        self.assertNotIn("is_correct", content)
        self.assertNotIn('"is_correct": true', content)
        self.assertNotIn('"is_correct": false', content)

    def test_another_student_cannot_access_session(self):
        # Candidate 1 starts exam
        self.client.login(username="cbt_candidate_1", password="Password123!")
        self.client.post(reverse("exams:exam_start", args=[self.exam.slug]))
        attempt = ExamAttempt.objects.get(exam=self.exam, student=self.student)

        # Candidate 2 tries to access Candidate 1's session
        self.client.login(username="cbt_candidate_2", password="Password123!")
        res = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(res.status_code, 403)

    def test_student_save_answer_and_clear_response(self):
        self.client.login(username="cbt_candidate_1", password="Password123!")
        self.client.post(reverse("exams:exam_start", args=[self.exam.slug]))
        attempt = ExamAttempt.objects.get(exam=self.exam, student=self.student)
        first_resp = attempt.responses.filter(order=1).first()
        opt_a = first_resp.exam_question.question.options.filter(option_key="A").first()

        # 1. Save answer
        save_url = reverse("exams:exam_save_answer", args=[attempt.attempt_uuid])
        res_save = self.client.post(
            save_url,
            data=json.dumps({
                "response_id": first_resp.id,
                "action": "save_next",
                "selected_option_id": opt_a.id,
                "is_marked_for_review": False,
                "time_spent_delta": 25,
            }),
            content_type="application/json",
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        self.assertEqual(res_save.status_code, 200)
        data = res_save.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["palette_status"], "answered")

        first_resp.refresh_from_db()
        self.assertEqual(first_resp.selected_option, opt_a)
        self.assertEqual(first_resp.time_spent_seconds, 25)

        # 2. Clear response
        res_clear = self.client.post(
            save_url,
            data=json.dumps({
                "response_id": first_resp.id,
                "action": "clear",
                "selected_option_id": None,
                "is_marked_for_review": False,
                "time_spent_delta": 5,
            }),
            content_type="application/json",
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        self.assertEqual(res_clear.status_code, 200)
        data_clear = res_clear.json()
        self.assertEqual(data_clear["palette_status"], "not_answered")

        first_resp.refresh_from_db()
        self.assertIsNone(first_resp.selected_option)

    def test_student_mark_for_review(self):
        self.client.login(username="cbt_candidate_1", password="Password123!")
        self.client.post(reverse("exams:exam_start", args=[self.exam.slug]))
        attempt = ExamAttempt.objects.get(exam=self.exam, student=self.student)
        resp = attempt.responses.filter(order=2).first()

        save_url = reverse("exams:exam_save_answer", args=[attempt.attempt_uuid])
        res = self.client.post(
            save_url,
            data=json.dumps({
                "response_id": resp.id,
                "action": "mark_review",
                "selected_option_id": None,
                "is_marked_for_review": True,
                "time_spent_delta": 10,
            }),
            content_type="application/json",
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["palette_status"], "marked_for_review")

        resp.refresh_from_db()
        self.assertTrue(resp.is_marked_for_review)

    def test_anti_cheat_event_logging(self):
        self.client.login(username="cbt_candidate_1", password="Password123!")
        self.client.post(reverse("exams:exam_start", args=[self.exam.slug]))
        attempt = ExamAttempt.objects.get(exam=self.exam, student=self.student)

        log_url = reverse("exams:exam_log_event", args=[attempt.attempt_uuid])
        res = self.client.post(
            log_url,
            data=json.dumps({
                "event_type": "TAB_SWITCH",
                "details": "Candidate switched tabs to search engine.",
            }),
            content_type="application/json",
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "recorded")
        self.assertEqual(data["warning_count"], 1)

        attempt.refresh_from_db()
        self.assertEqual(attempt.anti_cheat_warnings, 1)
        self.assertTrue(ExamSecurityLog.objects.filter(attempt=attempt, event_type="TAB_SWITCH").exists())

    def test_timer_heartbeat_sync(self):
        self.client.login(username="cbt_candidate_1", password="Password123!")
        self.client.post(reverse("exams:exam_start", args=[self.exam.slug]))
        attempt = ExamAttempt.objects.get(exam=self.exam, student=self.student)

        sync_url = reverse("exams:exam_sync_status", args=[attempt.attempt_uuid])
        res = self.client.get(sync_url, headers={"x-requested-with": "XMLHttpRequest"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "active")
        self.assertGreater(data["remaining_seconds"], 0)

    def test_manual_exam_submission_and_evaluation(self):
        self.client.login(username="cbt_candidate_1", password="Password123!")
        self.client.post(reverse("exams:exam_start", args=[self.exam.slug]))
        attempt = ExamAttempt.objects.get(exam=self.exam, student=self.student)

        # Answer Q1 correctly (Option A) -> +4.00
        # Answer Q2 incorrectly (Option B) -> -1.00
        # Leave Q3 unanswered -> 0.00
        # Expected net score = 3.00 / 12.00 = 25.0% (Passing benchmark 50% -> is_passed=False)
        r1 = attempt.responses.filter(order=1).first()
        r1.selected_option = r1.exam_question.question.options.filter(option_key="A").first()
        r1.is_visited = True
        r1.save()

        r2 = attempt.responses.filter(order=2).first()
        r2.selected_option = r2.exam_question.question.options.filter(option_key="B").first()
        r2.is_visited = True
        r2.save()

        # Submit
        submit_url = reverse("exams:exam_submit", args=[attempt.attempt_uuid])
        res_submit = self.client.post(submit_url, follow=True)
        self.assertEqual(res_submit.status_code, 200)

        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.SUBMITTED)
        self.assertIsNotNone(attempt.submitted_at)
        self.assertEqual(attempt.score_obtained, Decimal("3.00"))
        self.assertEqual(attempt.percentage, Decimal("25.00"))
        self.assertFalse(attempt.is_passed)

        # Once submitted, saving answers must be blocked
        save_url = reverse("exams:exam_save_answer", args=[attempt.attempt_uuid])
        res_after = self.client.post(
            save_url,
            data=json.dumps({"response_id": r1.id, "action": "save_next"}),
            content_type="application/json",
        )
        self.assertEqual(res_after.json()["status"], "expired")

    def test_server_enforced_auto_timeout_when_expired(self):
        self.client.login(username="cbt_candidate_1", password="Password123!")
        self.client.post(reverse("exams:exam_start", args=[self.exam.slug]))
        attempt = ExamAttempt.objects.get(exam=self.exam, student=self.student)

        # Force expire the session in the past
        attempt.expires_at = timezone.now() - timedelta(minutes=10)
        attempt.save(update_fields=["expires_at"])
        self.assertTrue(attempt.is_expired)

        # Candidate attempts to save an answer after time expired
        r1 = attempt.responses.first()
        save_url = reverse("exams:exam_save_answer", args=[attempt.attempt_uuid])
        res = self.client.post(
            save_url,
            data=json.dumps({"response_id": r1.id, "action": "save_next"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "expired")

        # Verify attempt transitioned to TIMED_OUT on the server
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TIMED_OUT)

    def test_attempt_limits_enforced(self):
        # Exam max_attempts is 2
        self.client.login(username="cbt_candidate_1", password="Password123!")
        # 1st attempt
        self.client.post(reverse("exams:exam_start", args=[self.exam.slug]))
        a1 = ExamAttempt.objects.get(exam=self.exam, student=self.student)
        a1.submit_attempt()

        # 2nd attempt
        self.client.post(reverse("exams:exam_start", args=[self.exam.slug]))
        a2 = ExamAttempt.objects.filter(exam=self.exam, student=self.student, status=ExamAttempt.AttemptStatus.IN_PROGRESS).first()
        self.assertIsNotNone(a2)
        a2.submit_attempt()

        # 3rd attempt -> Must be rejected
        res_3 = self.client.post(reverse("exams:exam_start", args=[self.exam.slug]), follow=True)
        self.assertEqual(res_3.status_code, 200)
        self.assertContains(res_3, "maximum allowed attempts")

    def test_page_refresh_recovers_saved_responses(self):
        self.client.login(username="cbt_candidate_1", password="Password123!")
        self.client.post(reverse("exams:exam_start", args=[self.exam.slug]))
        attempt = ExamAttempt.objects.get(exam=self.exam, student=self.student)

        r1 = attempt.responses.filter(order=1).first()
        opt_a = r1.exam_question.question.options.filter(option_key="A").first()
        r1.selected_option = opt_a
        r1.is_marked_for_review = True
        r1.is_visited = True
        r1.save()

        # Simulate page refresh (GET session)
        res_refresh = self.client.get(reverse("exams:exam_session", args=[attempt.attempt_uuid]))
        self.assertEqual(res_refresh.status_code, 200)
        payload = json.loads(res_refresh.context["questions_json"])
        q1_data = payload[0]
        self.assertEqual(q1_data["selected_option_id"], opt_a.id)
        self.assertTrue(q1_data["is_marked_for_review"])
        self.assertEqual(q1_data["palette_status"], "answered_and_marked")


class ResultEvaluationAndAnalyticsTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name="Advanced Analytics Category",
            slug="advanced-analytics-cat",
            is_active=True,
        )
        self.other_category = Category.objects.create(
            name="Unassigned Category",
            slug="unassigned-cat",
            is_active=True,
        )
        self.teacher = User.objects.create_user(
            username="analytics_teacher",
            email="ateacher@test.com",
            password="Password123!",
            role=User.Roles.TEACHER,
            is_staff=True,
        )
        self.teacher.assigned_categories.add(self.category)

        self.other_teacher = User.objects.create_user(
            username="foreign_teacher",
            email="fteacher@test.com",
            password="Password123!",
            role=User.Roles.TEACHER,
            is_staff=True,
        )

        self.student_1 = User.objects.create_user(
            username="cand_alpha",
            email="alpha@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
        )
        self.student_2 = User.objects.create_user(
            username="cand_beta",
            email="beta@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
        )
        self.student_3 = User.objects.create_user(
            username="cand_gamma",
            email="gamma@test.com",
            password="Password123!",
            role=User.Roles.STUDENT,
        )

        # Create published exam
        self.exam = Exam.objects.create(
            title="NEET Biology & Physics Mock 2026",
            slug="neet-bio-physics-mock-2026",
            category=self.category,
            creator=self.teacher,
            status=Exam.ExamStatus.PUBLISHED,
            duration_minutes=60,
            passing_percentage=50.0,
            total_marks=Decimal("12.00"),
            show_result_immediately=True,
            allow_review_answers=True,
        )

        # 3 questions
        self.questions = []
        for i in range(1, 4):
            subj = "Physics" if i == 1 else "Biology"
            q = Question.objects.create(
                category=self.category,
                subject=subj,
                topic="Thermodynamics" if i == 1 else "Cell Biology",
                chapter="Heat" if i == 1 else "Mitochondria",
                difficulty=Question.DifficultyLevel.MEDIUM,
                question_text=f"Question Statement {i}: Detailed scientific assessment.",
                positive_marks=Decimal("4.00"),
                negative_marks=Decimal("1.00"),
                creator=self.teacher,
                explanation=f"Step-by-step solution for question {i}.",
            )
            for key, is_corr in [("A", True), ("B", False), ("C", False), ("D", False)]:
                QuestionOption.objects.create(
                    question=q,
                    option_key=key,
                    option_text=f"Option {key} for question {i}",
                    is_correct=is_corr,
                )
            eq = ExamQuestion.objects.create(
                exam=self.exam,
                question=q,
                order=i,
                section="Section 1",
                positive_marks=Decimal("4.00"),
                negative_marks=Decimal("1.00"),
            )
            self.questions.append((q, eq))

    def _create_submitted_attempt(self, student, q1_key=None, q2_key=None, q3_key=None, time_taken=60):
        attempt = ExamAttempt.objects.create(
            exam=self.exam,
            student=student,
            status=ExamAttempt.AttemptStatus.IN_PROGRESS,
            total_marks=self.exam.total_marks,
            time_taken_seconds=time_taken,
        )
        keys = [q1_key, q2_key, q3_key]
        for idx, (q, eq) in enumerate(self.questions):
            chosen_key = keys[idx]
            opt = q.options.filter(option_key=chosen_key).first() if chosen_key else None
            ExamResponse.objects.create(
                attempt=attempt,
                exam_question=eq,
                selected_option=opt,
                order=idx + 1,
                is_visited=True,
                time_spent_seconds=20,
            )
        attempt.submit_attempt()
        return attempt

    def test_student_result_scorecard_displays_correct_metrics(self):
        # Student 1 answers Q1=A (+4), Q2=A (+4), Q3=B (-1) -> Net score = 7.00
        attempt = self._create_submitted_attempt(self.student_1, q1_key="A", q2_key="A", q3_key="B", time_taken=120)

        self.client.login(username="cand_alpha", password="Password123!")
        res = self.client.get(reverse("exams:student_result_detail", args=[attempt.attempt_uuid]))
        self.assertEqual(res.status_code, 200)

        self.assertContains(res, "7.00")
        self.assertContains(res, "58.33%")  # 7 / 12 * 100
        self.assertContains(res, "QUALIFIED / PASSED")
        self.assertEqual(attempt.correct_count, 2)
        self.assertEqual(attempt.incorrect_count, 1)
        self.assertEqual(attempt.unattempted_count, 0)
        self.assertEqual(attempt.accuracy_percentage, Decimal("66.67"))

    def test_student_cannot_view_another_students_result(self):
        attempt = self._create_submitted_attempt(self.student_1, q1_key="A", q2_key="B", time_taken=80)

        # Student 2 tries to view Student 1's private scorecard
        self.client.login(username="cand_beta", password="Password123!")
        res = self.client.get(reverse("exams:student_result_detail", args=[attempt.attempt_uuid]))
        self.assertEqual(res.status_code, 403)

    def test_result_visibility_restriction_when_show_immediately_false(self):
        self.exam.show_result_immediately = False
        self.exam.save()

        attempt = self._create_submitted_attempt(self.student_1, q1_key="A", q2_key="A", time_taken=100)

        # Student views result -> Score must be hidden under evaluation banner
        self.client.login(username="cand_alpha", password="Password123!")
        res = self.client.get(reverse("exams:student_result_detail", args=[attempt.attempt_uuid]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Results & Analytics Under Evaluation")
        self.assertNotContains(res, "Score Obtained")

        # Staff / Teacher views the same result -> Score is visible
        self.client.login(username="analytics_teacher", password="Password123!")
        res_teacher = self.client.get(reverse("exams:student_result_detail", args=[attempt.attempt_uuid]))
        self.assertEqual(res_teacher.status_code, 200)
        self.assertContains(res_teacher, "Marks Obtained")

    def test_solution_review_permitted_and_answers_revealed(self):
        attempt = self._create_submitted_attempt(self.student_1, q1_key="A", q2_key="B", time_taken=60)

        self.client.login(username="cand_alpha", password="Password123!")
        res = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Question-by-Question Solution Review")
        self.assertContains(res, "Step-by-step solution for question 1")
        self.assertContains(res, "Your Answer (Correct)")
        self.assertContains(res, "Correct Answer")

    def test_solution_review_restricted_when_disallowed(self):
        self.exam.allow_review_answers = False
        self.exam.save()

        attempt = self._create_submitted_attempt(self.student_1, q1_key="A", time_taken=50)

        self.client.login(username="cand_alpha", password="Password123!")
        res = self.client.get(reverse("exams:student_result_review", args=[attempt.attempt_uuid]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Solution Review Restricted")
        self.assertNotContains(res, "Step-by-step solution")

    def test_pdf_scorecard_generation_returns_valid_pdf(self):
        attempt = self._create_submitted_attempt(self.student_1, q1_key="A", q2_key="A", time_taken=90)

        self.client.login(username="cand_alpha", password="Password123!")
        res = self.client.get(reverse("exams:student_result_pdf", args=[attempt.attempt_uuid]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        self.assertIn("scorecard.pdf", res["Content-Disposition"])
        self.assertTrue(res.content.startswith(b"%PDF-"))

    def test_rank_and_percentile_calculation_with_tie_breaking(self):
        # Candidate 1: Q1=A (+4), Q2=A (+4) -> 8.00 marks (0 incorrect, time=100s) -> Rank 1
        a1 = self._create_submitted_attempt(self.student_1, q1_key="A", q2_key="A", time_taken=100)

        # Candidate 2: Q1=A (+4), Q2=A (+4), Q3=B (-1) -> 7.00 marks -> Rank 3
        a2 = self._create_submitted_attempt(self.student_2, q1_key="A", q2_key="A", q3_key="B", time_taken=120)

        # Candidate 3: Q1=A (+4) -> 4.00 marks -> Rank 3 of 3 (Wait, let's test tie-breaker between a1 and a4)
        # Let's create an exact score match for Rank 1 to test tie-breaker
        student_4 = User.objects.create_user(username="cand_delta", email="delta@test.com", password="Password123!", role=User.Roles.STUDENT)
        # Candidate 4: Q1=A (+4), Q2=A (+4), Q3=B (-1), wait: 8.00 vs 8.00
        # If candidate 4 has score 8.00 but 1 incorrect (e.g. if test had 4 questions or different mark):
        # With current questions, let's test faster time tie-breaker:
        # Candidate 4 scores 8.00 with 0 incorrect, but time=80s (faster than candidate 1's 100s)!
        a4 = self._create_submitted_attempt(student_4, q1_key="A", q2_key="A", time_taken=80)

        a1.refresh_from_db()
        a2.refresh_from_db()
        a4.refresh_from_db()

        # Candidate 4 should be Rank 1 (faster time: 80s vs 100s)
        self.assertEqual(a4.rank, 1)
        # Candidate 1 should be Rank 2 (same 8.00 marks, but 100s)
        self.assertEqual(a1.rank, 2)
        # Candidate 2 should be Rank 3 (7.00 marks)
        self.assertEqual(a2.rank, 3)

        # Percentile checks
        self.assertEqual(a4.percentile, Decimal("100.00"))
        self.assertEqual(a1.percentile, Decimal("66.67"))
        self.assertEqual(a2.percentile, Decimal("33.33"))

    def test_exam_leaderboard_view(self):
        self._create_submitted_attempt(self.student_1, q1_key="A", time_taken=50)

        self.client.login(username="cand_alpha", password="Password123!")
        res = self.client.get(reverse("exams:exam_leaderboard", args=[self.exam.slug]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Official Examination Leaderboard")
        self.assertContains(res, "cand_alpha")

    def test_teacher_analytics_scoped_by_permission(self):
        self._create_submitted_attempt(self.student_1, q1_key="A", q2_key="A", time_taken=70)

        # Permitted teacher
        self.client.login(username="analytics_teacher", password="Password123!")
        res = self.client.get(reverse("exams:teacher_exam_analytics", args=[self.exam.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Institutional Examination Analytics")
        self.assertContains(res, "Class Pass Rate")

        # Foreign teacher (not assigned to category)
        self.client.login(username="foreign_teacher", password="Password123!")
        res_foreign = self.client.get(reverse("exams:teacher_exam_analytics", args=[self.exam.pk]))
        self.assertEqual(res_foreign.status_code, 403)

    def test_teacher_export_results_csv(self):
        self._create_submitted_attempt(self.student_1, q1_key="A", q2_key="A", time_taken=80)

        self.client.login(username="analytics_teacher", password="Password123!")
        res = self.client.get(reverse("exams:teacher_export_results_csv", args=[self.exam.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "text/csv")
        self.assertIn(".csv", res["Content-Disposition"])
        content = res.content.decode()
        self.assertIn("Rank,Candidate Name,Username,Email Address", content)
        self.assertIn("cand_alpha", content)
        self.assertIn("alpha@test.com", content)


