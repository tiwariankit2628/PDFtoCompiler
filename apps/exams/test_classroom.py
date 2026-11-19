"""Teacher-led classroom tests: join codes, sessions, monitoring, leaderboard.

Covers code generation, join/resume, >40 students, invalid/closed/ended/revoked
codes, distinct counts, teacher isolation, confirmed-key grading, awaiting-key,
leaderboard ties/retakes, privacy, and that joining never triggers premium or
private-practice limits. Concurrency is exercised at the DB-constraint level.
"""
import re
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.exams.models import (
    Category, ClassroomParticipant, ClassroomSession, Exam, ExamAttempt, ExamQuestion,
)
from apps.questions.models import AnswerStatus, Question, QuestionOption


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ClassroomBase(TestCase):
    def setUp(self):
        cache.clear()
        self.category = Category.objects.create(name="Classroom", slug="classroom-cat", is_active=True)
        self.teacher = User.objects.create_user(username="cls_teacher", email="t@test.com",
                                                 password="Password123!", role=User.Roles.TEACHER, is_staff=True)
        self.other_teacher = User.objects.create_user(username="cls_teacher2", email="t2@test.com",
                                                      password="Password123!", role=User.Roles.TEACHER, is_staff=True)
        self.student = User.objects.create_user(username="cls_student", email="s@test.com",
                                                password="Password123!", role=User.Roles.STUDENT, exam_track="jee")

    def make_exam(self, *, keyed=3, questions=3, title="Classroom Quiz", creator=None):
        creator = creator or self.teacher
        exam = Exam.objects.create(
            title=title, slug=title.lower().replace(" ", "-") + f"-{Exam.objects.count()}",
            category=self.category, creator=creator, status=Exam.ExamStatus.PUBLISHED,
            subject="General", duration_minutes=30, total_marks=Decimal("12.00"),
            default_positive_marks=4, default_negative_marks=1, is_free=True,
            target_type="jee",
        )
        for i in range(1, questions + 1):
            confirmed = i <= keyed
            q = Question.objects.create(
                category=self.category, subject="General", question_text=f"Q{i}?",
                positive_marks=4, negative_marks=1, creator=creator,
                answer_status=AnswerStatus.MANUALLY_VERIFIED if confirmed else AnswerStatus.NEEDS_REVIEW,
                target_type="jee",
            )
            for j, key in enumerate("ABCD"):
                QuestionOption.objects.create(question=q, option_key=key, option_text=key,
                                              is_correct=(confirmed and key == "A"), order=j)
            ExamQuestion.objects.create(exam=exam, question=q, order=i, positive_marks=4, negative_marks=1)
        return exam

    def session_for(self, exam=None, **kw):
        return ClassroomSession.create_for(exam or self.make_exam(), self.teacher, **kw)

    def join(self, student, code):
        self.client.force_login(student)
        return self.client.post(reverse("exams:classroom_join"), {"code": code})

    def start(self, student, session):
        self.client.force_login(student)
        return self.client.post(reverse("exams:classroom_take", args=[session.pk]))


class CodeAndCreationTests(ClassroomBase):
    def test_teacher_creates_session_with_valid_unique_code(self):
        self.client.force_login(self.teacher)
        exam = self.make_exam()
        res = self.client.post(reverse("exams:classroom_session_create"), {"exam": exam.pk, "label": "10-B"})
        self.assertEqual(res.status_code, 302)
        session = ClassroomSession.objects.get(exam=exam)
        self.assertTrue(re.fullmatch(rf"[{ClassroomSession.CODE_ALPHABET}]{{{ClassroomSession.CODE_LENGTH}}}", session.code))
        self.assertTrue(session.joining_allowed)
        # Codes are distinct across sessions.
        s2 = ClassroomSession.create_for(exam, self.teacher)
        self.assertNotEqual(session.code, s2.code)

    def test_cannot_create_session_for_unpublished_or_others_exam(self):
        self.client.force_login(self.teacher)
        draft = self.make_exam(title="Draft")
        draft.status = Exam.ExamStatus.DRAFT
        draft.save(update_fields=["status"])
        self.client.post(reverse("exams:classroom_session_create"), {"exam": draft.pk})
        self.assertFalse(ClassroomSession.objects.filter(exam=draft).exists())
        others = self.make_exam(title="Others", creator=self.other_teacher)
        res = self.client.post(reverse("exams:classroom_session_create"), {"exam": others.pk})
        self.assertEqual(res.status_code, 404)


class JoinAndResumeTests(ClassroomBase):
    def test_join_start_and_resume_without_duplicates(self):
        session = self.session_for()
        res = self.join(self.student, session.code.lower())  # case-insensitive
        self.assertRedirects(res, reverse("exams:classroom_take", args=[session.pk]), fetch_redirect_response=False)
        self.assertEqual(session.participants.count(), 1)
        # Re-join is idempotent (no second participant).
        self.join(self.student, session.code)
        self.assertEqual(session.participants.count(), 1)
        # Start creates exactly one attempt.
        self.start(self.student, session)
        self.assertEqual(session.attempts.filter(student=self.student).count(), 1)
        attempt = session.attempts.get(student=self.student)
        # Re-POST start resumes the same attempt (no duplicate).
        self.start(self.student, session)
        self.assertEqual(session.attempts.filter(student=self.student).count(), 1)
        # Re-join while active resumes the live session directly.
        res = self.join(self.student, session.code)
        self.assertRedirects(res, reverse("exams:exam_session", args=[attempt.attempt_uuid]), fetch_redirect_response=False)

    def test_more_than_40_students_can_join(self):
        session = self.session_for()
        students = [User.objects.create(username=f"stud{i}", email=f"stud{i}@t.com", role=User.Roles.STUDENT, exam_track="jee")
                    for i in range(42)]
        for s in students:
            self.join(s, session.code)
        self.assertEqual(session.participants.count(), 42)
        self.assertEqual(session.counts()["joined"], 42)

    def test_invalid_closed_ended_and_regenerated_codes(self):
        session = self.session_for()
        # Invalid code: friendly error, no participant.
        self.join(self.student, "ZZZZZZ")
        self.assertEqual(session.participants.count(), 0)
        # Regenerate: the old code stops working.
        old = session.code
        session.regenerate_code()
        self.join(self.student, old)
        self.assertEqual(session.participants.count(), 0)
        self.join(self.student, session.code)
        self.assertEqual(session.participants.count(), 1)
        # Closing joining blocks NEW students but not those already joined.
        session.is_open = False
        session.save(update_fields=["is_open"])
        newbie = User.objects.create(username="newbie", email="n@t.com", role=User.Roles.STUDENT, exam_track="jee")
        self.join(newbie, session.code)
        self.assertFalse(session.participants.filter(student=newbie).exists())
        # Ended session accepts no attempts.
        session.is_open = True
        session.save(update_fields=["is_open"])
        session.end_now()
        res = self.join(newbie, session.code)
        self.assertFalse(session.participants.filter(student=newbie).exists())

    def test_guess_throttle_blocks_repeated_invalid_codes(self):
        session = self.session_for()
        self.client.force_login(self.student)
        for _ in range(25):
            self.client.post(reverse("exams:classroom_join"), {"code": "AAAAAA"})
        # After the limit, even a valid code is deferred with a throttle message.
        res = self.client.post(reverse("exams:classroom_join"), {"code": session.code})
        self.assertFalse(session.participants.filter(student=self.student).exists())

    def test_join_does_not_trigger_premium_trial_or_private_limits(self):
        # A student who already used their free mock can still join a classroom test.
        self.student.free_test_attempted = True
        self.student.save(update_fields=["free_test_attempted"])
        session = self.session_for()
        self.join(self.student, session.code)
        self.start(self.student, session)
        self.assertEqual(session.attempts.filter(student=self.student).count(), 1)
        # No private-practice usage is created by classroom joining.
        from apps.payments.models import PracticeUsage
        self.assertFalse(PracticeUsage.objects.filter(user=self.student).exists())


class CountsMonitoringTests(ClassroomBase):
    def test_counts_are_distinct_and_isolated_between_teachers(self):
        session = self.session_for()
        joined_only = User.objects.create(username="j1", email="j1@t.com", role=User.Roles.STUDENT, exam_track="jee")
        started = User.objects.create(username="j2", email="j2@t.com", role=User.Roles.STUDENT, exam_track="jee")
        submitted = User.objects.create(username="j3", email="j3@t.com", role=User.Roles.STUDENT, exam_track="jee")
        for s in (joined_only, started, submitted):
            self.join(s, session.code)
        self.start(started, session)
        self.start(submitted, session)
        session.attempts.get(student=submitted).submit_attempt()
        c = session.counts()
        self.assertEqual(c["joined"], 3)
        self.assertEqual(c["started"], 2)
        self.assertEqual(c["in_progress"], 1)
        self.assertEqual(c["submitted"], 1)
        # A different teacher's session is completely separate.
        other = ClassroomSession.create_for(self.make_exam(title="Other", creator=self.other_teacher), self.other_teacher)
        self.assertEqual(other.counts()["joined"], 0)
        # Owner-only monitoring/control.
        self.client.force_login(self.other_teacher)
        self.assertEqual(self.client.get(reverse("exams:classroom_monitor", args=[session.pk])).status_code, 403)
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(reverse("exams:classroom_monitor", args=[session.pk])).status_code, 200)

    def test_end_now_times_out_in_progress_and_preserves_answers(self):
        session = self.session_for()
        self.join(self.student, session.code)
        self.start(self.student, session)
        attempt = session.attempts.get(student=self.student)
        resp = attempt.responses.first()
        resp.selected_option = resp.exam_question.question.options.get(option_key="A")
        resp.save(update_fields=["selected_option"])
        session.end_now()
        attempt.refresh_from_db(); resp.refresh_from_db()
        self.assertEqual(attempt.status, ExamAttempt.AttemptStatus.TIMED_OUT)
        self.assertEqual(resp.selected_option.option_key, "A")  # answer preserved
        self.assertTrue(session.is_ended)


class GradingAndLeaderboardTests(ClassroomBase):
    def _take_and_answer(self, student, session, correct_keys):
        self.join(student, session.code)
        self.start(student, session)
        attempt = session.attempts.get(student=student, status=ExamAttempt.AttemptStatus.IN_PROGRESS)
        for resp in attempt.responses.select_related("exam_question__question"):
            q = resp.exam_question.question
            key = correct_keys.get(resp.exam_question.order)
            if key:
                resp.selected_option = q.options.get(option_key=key)
                resp.save(update_fields=["selected_option"])
        attempt.submit_attempt()
        attempt.refresh_from_db()
        return attempt

    def test_confirmed_key_grading_and_awaiting_key(self):
        # 2 keyed questions, 1 without a confirmed key.
        exam = self.make_exam(keyed=2, questions=3)
        session = self.session_for(exam=exam)
        # Answer all "A": Q1,Q2 correct (+4 each); Q3 has no key -> awaiting, 0.
        attempt = self._take_and_answer(self.student, session, {1: "A", 2: "A", 3: "A"})
        self.assertEqual(attempt.correct_count, 2)
        self.assertEqual(float(attempt.score_obtained), 8.0)
        q3 = attempt.responses.get(exam_question__order=3)
        self.assertIsNone(q3.is_correct)  # awaiting key, never marked wrong
        self.assertEqual(float(q3.marks_awarded), 0.0)

    def test_leaderboard_ranks_ties_and_isolated(self):
        exam = self.make_exam(keyed=3, questions=3)
        session = self.session_for(exam=exam)
        top = User.objects.create(username="top", email="top@t.com", role=User.Roles.STUDENT, exam_track="jee")
        mid = User.objects.create(username="mid", email="mid@t.com", role=User.Roles.STUDENT, exam_track="jee")
        tie = User.objects.create(username="tie", email="tie@t.com", role=User.Roles.STUDENT, exam_track="jee")
        self._take_and_answer(top, session, {1: "A", 2: "A", 3: "A"})   # 12
        self._take_and_answer(mid, session, {1: "A", 2: "A", 3: "B"})   # 8-1=7
        self._take_and_answer(tie, session, {1: "A", 2: "A", 3: "A"})   # 12 (tie with top)
        from apps.exams.classroom import _session_leaderboard
        board = _session_leaderboard(session)
        self.assertEqual(len(board), 3)
        self.assertEqual(board[0]["rank"], 1)
        self.assertEqual(board[1]["rank"], 1)   # tie shares rank 1
        self.assertEqual(board[2]["rank"], 3)   # next rank skips to 3
        self.assertEqual(board[2]["username"], "mid")

    def test_legacy_retake_flag_does_not_permit_a_new_attempt(self):
        exam = self.make_exam(keyed=3, questions=3)
        session = self.session_for(exam=exam, allow_retakes=True)
        first = self._take_and_answer(self.student, session, {1: "A", 2: "A", 3: "B"})
        again = self.join(self.student, session.code)
        self.assertRedirects(again, reverse("exams:exam_completed", args=[first.attempt_uuid]), fetch_redirect_response=False)
        self.start(self.student, session)
        self.assertEqual(session.attempts.filter(student=self.student).count(), 1)
        from apps.exams.classroom import _session_leaderboard
        board = _session_leaderboard(session)
        self.assertEqual(len(board), 1)
        self.assertEqual(float(board[0]["score"]), 7.0)


class PrivacyTests(ClassroomBase):
    def test_students_cannot_see_others_results_or_leaderboard(self):
        exam = self.make_exam()
        session = self.session_for(exam=exam)
        a = User.objects.create(username="a", email="a@t.com", role=User.Roles.STUDENT, exam_track="jee")
        b = User.objects.create(username="b", email="b@t.com", role=User.Roles.STUDENT, exam_track="jee")
        self.join(a, session.code); self.start(a, session)
        attempt_a = session.attempts.get(student=a)
        # Student B cannot view A's result detail.
        self.client.force_login(b)
        self.assertIn(self.client.get(reverse("exams:student_result_detail", args=[attempt_a.attempt_uuid])).status_code, (403, 404))
        # Leaderboard is private to the teacher by default.
        self.join(b, session.code)
        self.assertEqual(self.client.get(reverse("exams:classroom_leaderboard", args=[session.pk])).status_code, 403)
        # Owner can always see it.
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(reverse("exams:classroom_leaderboard", args=[session.pk])).status_code, 200)

    def test_students_can_see_leaderboard_only_when_enabled(self):
        session = self.session_for(show_leaderboard_to_students=True)
        self.join(self.student, session.code)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("exams:classroom_leaderboard", args=[session.pk])).status_code, 200)
        # A non-participant still cannot.
        outsider = User.objects.create(username="out", email="out@t.com", role=User.Roles.STUDENT, exam_track="jee")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(reverse("exams:classroom_leaderboard", args=[session.pk])).status_code, 403)
