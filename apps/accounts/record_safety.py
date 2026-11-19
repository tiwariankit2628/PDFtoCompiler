"""Avoid accidental cascade deletion of assessment and commercial records."""


def has_protected_account_records(user):
    """Application/admin deletion guard; not a substitute for database backups.

    Ordinary institutional attempts and public papers/tests matter too: deleting
    a student or a test creator would otherwise cascade their historical work.
    Authorship is retained even where a question's creator uses SET_NULL.
    """
    return bool(
        user.uploaded_papers.exists() or user.created_exams.exists()
        or user.authored_questions.exists() or user.exam_attempts.exists()
        or user.classroom_sessions.exists() or user.classroom_activations.exists()
        or user.classroom_participations.exists() or user.practice_usage.exists()
        or user.practice_checkouts.exists() or user.practice_entitlements.exists()
        or user.pack_checkouts.exists() or user.credit_grants.exists() or user.credit_uses.exists()
    )
