"""Explicit JEE / NEET classifications used for student-facing access.

Titles, subjects and categories are deliberately NOT used for inference.  The
legacy mapper accepts only a small set of unambiguous whole-field aliases.
"""
from django.db import models
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone


class ExamTrack(models.TextChoices):
    JEE = "jee", "JEE (Main and Advanced)"
    NEET = "neet", "NEET"


class ExamTarget(models.TextChoices):
    JEE = "jee", "JEE (Main and Advanced)"
    JEE_MAIN = "jee_main", "JEE Main"
    JEE_ADVANCED = "jee_advanced", "JEE Advanced"
    NEET = "neet", "NEET"


JEE_TARGETS = (ExamTarget.JEE, ExamTarget.JEE_MAIN, ExamTarget.JEE_ADVANCED)
NEET_TARGETS = (ExamTarget.NEET,)


def targets_for_track(track):
    if track == ExamTrack.JEE:
        return JEE_TARGETS
    if track == ExamTrack.NEET:
        return NEET_TARGETS
    return ()


def track_for_target(target):
    if target in JEE_TARGETS:
        return ExamTrack.JEE
    if target in NEET_TARGETS:
        return ExamTrack.NEET
    return ""


def exam_matches_student(user, exam):
    """Fail closed for unknown classifications and known conflicting items."""
    if not user.is_student:
        return True
    allowed = targets_for_track(user.exam_track)
    if exam.target_type not in allowed:
        return False
    # An unclassified or invalid question could belong to either exam. It
    # must be reviewed before a *new* attempt, not silently treated as safe.
    return not exam.exam_questions.exclude(question__target_type__in=allowed).exists()


def student_exam_queryset(queryset, user, *, include_active=False):
    """Filter exams and their contents, never depending on a category name.

    Existing active attempts can optionally be resumed even if the account or
    the exam was unclassified before an upgrade. This never creates a new attempt.
    """
    if not user.is_student:
        return queryset
    allowed = targets_for_track(user.exam_track)
    from apps.exams.models import ExamQuestion
    conflict = ExamQuestion.objects.filter(exam_id=OuterRef("pk")).exclude(question__target_type__in=allowed)
    condition = Q(target_type__in=allowed) & ~Exists(conflict)
    if include_active:
        # Use Exists rather than a join, which could duplicate catalog entries.
        from apps.exams.models import ExamAttempt
        active = ExamAttempt.objects.filter(
            Q(expires_at__gt=timezone.now()) | Q(expires_at__isnull=True),
            exam_id=OuterRef("pk"), student=user, status=ExamAttempt.AttemptStatus.IN_PROGRESS,
        )
        condition |= Exists(active)
    return queryset.filter(condition)


def legacy_target(value):
    """Only exact, explicit legacy labels; never guess from a title or category."""
    normalized = " ".join((value or "").strip().casefold().replace("-", " ").split())
    return {
        "jee": ExamTarget.JEE,
        "jee main": ExamTarget.JEE_MAIN,
        "jee mains": ExamTarget.JEE_MAIN,
        "jee advanced": ExamTarget.JEE_ADVANCED,
        "neet": ExamTarget.NEET,
        "neet ug": ExamTarget.NEET,
    }.get(normalized, "")
