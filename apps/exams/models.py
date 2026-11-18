import uuid
from decimal import Decimal
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django.utils.text import slugify


class Category(models.Model):
    """
    Dynamic examination category (e.g. Competitive Exams, Engineering & Medical,
    Government, Coding & Technical, University/School, Custom Mocks).
    """

    name = models.CharField("Category Name", max_length=150, unique=True)
    slug = models.SlugField("URL Slug", max_length=160, unique=True, db_index=True)
    description = models.TextField("Description", blank=True)
    icon = models.CharField(
        "Bootstrap Icon Class",
        max_length=60,
        default="bi-mortarboard",
        help_text="Bootstrap icon class e.g. bi-trophy, bi-cpu, bi-code-slash, bi-mortarboard",
    )
    image = models.ImageField(
        "Category Image",
        upload_to="categories/",
        blank=True,
        null=True,
    )
    is_active = models.BooleanField("Active Status", default=True, db_index=True)
    display_order = models.PositiveIntegerField("Display Order", default=0, db_index=True)
    seo_title = models.CharField("SEO Title", max_length=200, blank=True)
    seo_description = models.TextField("SEO Meta Description", blank=True, max_length=300)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Examination Category"
        verbose_name_plural = "Examination Categories"
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @property
    def exam_count(self):
        return self.exams.filter(is_private_practice=False, is_classroom_only=False).count()

    @property
    def active_exam_count(self):
        return self.exams.filter(status=Exam.ExamStatus.PUBLISHED, is_private_practice=False, is_classroom_only=False).count()


class Exam(models.Model):
    """
    Examination model linking categories, teachers/creators, questions,
    and candidate attempts. Supports comprehensive CBT configuration.
    """

    class ExamStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        SCHEDULED = "scheduled", "Scheduled"
        ARCHIVED = "archived", "Archived"

    is_private_practice = models.BooleanField(default=False, db_index=True)
    # Dedicated classroom tests are available only through a session join code,
    # never through the public/institutional catalog or ordinary start route.
    is_classroom_only = models.BooleanField(default=False, db_index=True)
    title = models.CharField("Examination Title", max_length=255)
    slug = models.SlugField("URL Slug", max_length=260, unique=True)
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="exams",
        help_text="Protected deletion: Category cannot be deleted if associated with examinations.",
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="created_exams",
    )
    status = models.CharField(
        "Status",
        max_length=20,
        choices=ExamStatus.choices,
        default=ExamStatus.DRAFT,
        db_index=True,
    )
    description = models.TextField("Examination Description", blank=True)
    subject = models.CharField("Subject / Domain", max_length=150, blank=True, db_index=True)
    topics = models.CharField("Topics / Syllabus Covered", max_length=255, blank=True)
    target_exam = models.CharField("Target Examination", max_length=150, blank=True)

    is_free = models.BooleanField(
        "Free Examination",
        default=False,
        help_text="Eligible for trial or open without subscription.",
    )
    duration_minutes = models.PositiveIntegerField("Duration (Minutes)", default=60)
    total_marks = models.DecimalField("Total Marks", max_digits=6, decimal_places=2, default=100.0)
    passing_percentage = models.DecimalField(
        "Passing Percentage", max_digits=5, decimal_places=2, default=40.0
    )
    default_positive_marks = models.DecimalField(
        "Default Positive Marks", max_digits=5, decimal_places=2, default=4.0
    )
    default_negative_marks = models.DecimalField(
        "Default Negative Marks", max_digits=5, decimal_places=2, default=1.0
    )
    shuffle_questions = models.BooleanField("Shuffle Questions for Candidates", default=True)
    shuffle_options = models.BooleanField("Shuffle Options for Candidates", default=True)
    # Image-first exams render each question as the original-PDF crop with A/B/C/D
    # selection buttons below; answers are still bound by option id, so scoring is
    # unchanged. Enabling this forces options into fixed A..E order (see views).
    image_first_mode = models.BooleanField("Image-first display", default=False)
    # Strict Exam Mode (teacher-led tests only): disclosed three-warning page-switch
    # rule. Scoped to this exam; never changes other exams or private practice.
    strict_exam_mode = models.BooleanField(
        "Strict exam mode (3-warning page-switch rule)", default=False,
        help_text="Disclosed to students. Counts distinct page switches; the 4th ends the attempt for review.",
    )
    max_attempts = models.PositiveIntegerField(
        "Max Attempts Allowed", default=1, help_text="0 allows unlimited attempts."
    )
    show_result_immediately = models.BooleanField("Show Results Immediately on Submit", default=True)
    allow_review_answers = models.BooleanField("Allow Answer Key & Solution Review", default=True)

    instructions = models.TextField("Instructions & Guidelines", blank=True)
    start_time = models.DateTimeField("Start Window", null=True, blank=True)
    end_time = models.DateTimeField("End Window", null=True, blank=True)

    questions = models.ManyToManyField(
        "questions.Question",
        through="ExamQuestion",
        related_name="assigned_exams",
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Examination"
        verbose_name_plural = "Examinations"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.title) or "exam"
            unique_slug = base_slug
            counter = 1
            while Exam.objects.filter(slug=unique_slug).exclude(pk=self.pk).exists():
                unique_slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = unique_slug
        super().save(*args, **kwargs)

    @property
    def total_questions_count(self):
        return self.exam_questions.count()

    @property
    def calculated_total_marks(self):
        agg = self.exam_questions.aggregate(models.Sum("positive_marks"))["positive_marks__sum"]
        return agg or 0.0

    @property
    def has_attempts(self):
        return self.attempts.exists()

    @property
    def is_expired(self):
        from django.utils import timezone
        return bool(self.end_time and timezone.now() > self.end_time)

    @property
    def is_scheduled_upcoming(self):
        from django.utils import timezone
        return bool(self.start_time and timezone.now() < self.start_time)

    def recalculate_marks(self):
        """Calculates and updates total_marks from assigned exam questions."""
        agg = self.exam_questions.aggregate(models.Sum("positive_marks"))["positive_marks__sum"]
        if agg is not None:
            self.total_marks = agg
            self.save(update_fields=["total_marks"])


class ExamQuestion(models.Model):
    """
    Intermediate model mapping questions to examinations with display order,
    optional section segregation, and question-specific mark overrides.
    """

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="exam_questions")
    question = models.ForeignKey(
        "questions.Question",
        on_delete=models.PROTECT,
        related_name="exam_instances",
        help_text="Protected deletion: Question cannot be deleted from bank if assigned to an exam.",
    )
    order = models.PositiveIntegerField("Question Order", default=1)
    section = models.CharField("Section Name", max_length=100, default="General", blank=True)
    positive_marks = models.DecimalField("Positive Marks", max_digits=5, decimal_places=2, default=4.0)
    negative_marks = models.DecimalField("Negative Marks", max_digits=5, decimal_places=2, default=1.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Exam Question"
        verbose_name_plural = "Exam Questions"
        ordering = ["order", "id"]
        unique_together = ("exam", "question")

    def __str__(self):
        return f"[{self.exam.title}] Q#{self.order}: {self.question.question_text[:40]}"


class ExamAttempt(models.Model):
    """
    Student candidate examination attempt tracking performance, score, and completion.
    """

    class AttemptStatus(models.TextChoices):
        IN_PROGRESS = "in_progress", "In Progress"
        SUBMITTED = "submitted", "Submitted"
        EVALUATED = "evaluated", "Evaluated"
        TIMED_OUT = "timed_out", "Timed Out"
        # Ended by Strict Exam Mode after the disclosed page-switch limit. Saved
        # answers are kept and evaluated; this is a reported signal for review,
        # never a proof of cheating.
        TERMINATED = "terminated", "Ended for Security Review"

    # Terminal states that must never resume, be re-timed, or accept new answers.
    FINAL_STATUSES = ("submitted", "evaluated", "timed_out", "terminated")

    attempt_uuid = models.UUIDField(
        "Attempt UUID",
        default=uuid.uuid4,
        editable=False,
        db_index=True,
    )
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="attempts")
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="exam_attempts",
    )
    status = models.CharField(
        "Status",
        max_length=20,
        choices=AttemptStatus.choices,
        default=AttemptStatus.IN_PROGRESS,
        db_index=True,
    )
    # Snapshot needed for a database-enforced single active private attempt.
    is_private_practice = models.BooleanField(default=False, editable=False)
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField("Session Expiry Time", null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    score_obtained = models.DecimalField("Score Obtained", max_digits=6, decimal_places=2, default=0.0)
    total_marks = models.DecimalField("Total Marks", max_digits=6, decimal_places=2, default=100.0)
    percentage = models.DecimalField("Percentage", max_digits=5, decimal_places=2, default=0.0)
    is_passed = models.BooleanField("Passed", default=False)
    time_taken_seconds = models.PositiveIntegerField("Time Taken (Seconds)", default=0)
    last_active_question = models.ForeignKey(
        "ExamQuestion",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    # Classroom (teacher-led, join-code) session this attempt belongs to, if any.
    # Null for ordinary public exams and private practice. Results, counts and the
    # leaderboard for a classroom test are scoped by this session.
    classroom_session = models.ForeignKey(
        "ClassroomSession", on_delete=models.CASCADE, null=True, blank=True, related_name="attempts",
    )
    # A MIXED deterrent telemetry counter (blur, copy, fullscreen exit, etc.).
    # It is NOT the strict three-strike tab-switch count; see tab_switch_warnings.
    anti_cheat_warnings = models.PositiveIntegerField("Anti-Cheat Warnings", default=0)
    # Distinct visible->hidden (page switch) episodes counted ONLY in strict mode.
    # Server-authoritative and idempotent per client_event_id; drives termination.
    tab_switch_warnings = models.PositiveIntegerField("Tab-switch Warnings", default=0)
    # True once the strict page-switch limit ended the attempt for review.
    is_security_locked = models.BooleanField("Ended for security review", default=False)
    rank = models.PositiveIntegerField("Candidate Rank", null=True, blank=True)
    percentile = models.DecimalField("Percentile", max_digits=5, decimal_places=2, null=True, blank=True)
    correct_count = models.PositiveIntegerField("Correct Count", default=0)
    partial_count = models.PositiveIntegerField("Partially Correct Count", default=0)
    incorrect_count = models.PositiveIntegerField("Incorrect Count", default=0)
    unattempted_count = models.PositiveIntegerField("Unattempted Count", default=0)
    accuracy_percentage = models.DecimalField("Accuracy Percentage", max_digits=5, decimal_places=2, default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Examination Attempt"
        verbose_name_plural = "Examination Attempts"
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["exam", "student"],
                condition=models.Q(is_private_practice=True, status="in_progress"),
                name="one_active_private_practice_attempt",
            ),
            # One in-progress attempt per student per classroom session; prevents
            # duplicate joins/attempts from reconnects or concurrent requests.
            models.UniqueConstraint(
                fields=["classroom_session", "student"],
                condition=models.Q(status="in_progress", classroom_session__isnull=False),
                name="one_active_classroom_attempt",
            ),
        ]

    def __str__(self):
        return f"{self.student.username} - {self.exam.title} ({self.status})"

    def save(self, *args, **kwargs):
        # Keep the constraint snapshot consistent for every normal ORM create,
        # even if a future caller forgets to pass the private flag explicitly.
        if self._state.adding and self.exam_id:
            self.is_private_practice = self.exam.is_private_practice
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        if not self.expires_at:
            return False
        return timezone.now() >= self.expires_at

    @property
    def remaining_seconds(self):
        if not self.expires_at:
            return 0
        diff = int((self.expires_at - timezone.now()).total_seconds())
        return max(0, diff)

    @property
    def remaining_minutes(self):
        return round(self.remaining_seconds / 60, 1)

    @property
    def total_questions_count(self):
        return self.responses.count()

    @property
    def answered_count(self):
        answered = (models.Q(selected_option__isnull=False) | ~models.Q(selected_option_ids=[])
                    | models.Q(numerical_response__isnull=False))
        if self.status in self.FINAL_STATUSES:
            answered |= (models.Q(selected_option_key_at_grading__isnull=False)
                         | models.Q(is_correct__isnull=False))
        return self.responses.filter(answered).count()

    @property
    def marked_review_count(self):
        return self.responses.filter(is_marked_for_review=True).count()

    @property
    def not_visited_count(self):
        return self.responses.filter(is_visited=False).count()

    @property
    def not_answered_count(self):
        return self.responses.filter(is_visited=True, selected_option__isnull=True,
                                     selected_option_ids=[], numerical_response__isnull=True).count()

    @property
    def uses_confirmed_key_only(self):
        """Classroom and private-practice attempts grade only teacher/student
        confirmed keys; a missing key is 'awaiting key', never a wrong answer.
        Ordinary single-correct institutional questions retain their existing
        grading rules; PDF-derived and unsupported types are guarded as well."""
        return self.exam.is_private_practice or self.classroom_session_id is not None

    @property
    def has_ungraded_keys(self):
        """Whether a recorded grading snapshot includes an unavailable key."""
        return self.responses.filter(key_confirmed_at_grading=False).exists()

    @property
    def has_legacy_pdf_key_snapshots(self):
        """Older PDF attempts retain their recorded scores but lack key labels."""
        return self.responses.filter(exam_question__question__is_pdf_derived=True,
                                     key_confirmed_at_grading__isnull=True).exists()

    @property
    def has_legacy_key_snapshots(self):
        """Older PDF or typed attempts may have a score but no recorded key."""
        return self.responses.filter(
            models.Q(exam_question__question__is_pdf_derived=True)
            | models.Q(exam_question__question__question_type__in=("mcq_multiple", "numerical")),
            key_confirmed_at_grading__isnull=True,
        ).exists()

    @property
    def graded_possible_marks(self):
        """Possible marks excluding ungraded questions, if the key status is known."""
        if self.has_legacy_key_snapshots:
            return None
        return sum((r.exam_question.positive_marks for r in
                    self.responses.select_related("exam_question").exclude(key_confirmed_at_grading=False)), Decimal("0.00"))

    def evaluate_attempt(self):
        """
        Evaluates student's responses, calculating positive marks,
        negative penalties, net score, and pass/fail outcome.
        Stores question-level correctness and marks awarded snapshot.
        """
        confirmed = self.uses_confirmed_key_only
        total_pos = Decimal("0.00")
        total_neg = Decimal("0.00")
        corr = 0
        partial = 0
        incorr = 0
        unatt = 0
        graded_marks = Decimal("0.00")
        all_required_keys_verified = True
        responses = self.responses.select_related("exam_question__question", "selected_option").prefetch_related(
            "exam_question__question__options")

        for resp in responses.all():
            # Student PDFs commonly contain no answer key. Never interpret a
            # missing key as an incorrect answer or invent the correct choice.
            question = resp.exam_question.question
            # A private key is student-confirmed; a classroom/public PDF key is
            # teacher-confirmed. Unsupported types are never scored as if one
            # option represented the whole answer, even outside PDF workflows.
            options = list(question.options.all())
            correct_options = [opt for opt in options if opt.is_correct]
            key_confirmed = (question.key_verified if question.is_private_practice
                             else question.answer_status == "manually_verified")
            if question.question_type in ("mcq_multiple", "numerical"):
                from .typed_responses import grade_typed_response
                decision = grade_typed_response(resp, question, options, key_confirmed)
                resp.typed_grading_snapshot = decision
                has_key = decision["key_confirmed"]
                resp.key_confirmed_at_grading = has_key
                # Multiple/numerical keys are stored only in the typed snapshot.
                resp.correct_option_key_at_grading = None
                resp.selected_option_key_at_grading = None
                outcome = decision["outcome"]
                marks = Decimal(decision["marks"])
                resp.marks_awarded = marks
                resp.is_correct = True if outcome == "correct" else False if outcome == "incorrect" else None
                if not has_key:
                    all_required_keys_verified = False
                else:
                    graded_marks += resp.exam_question.positive_marks
                if outcome == "correct":
                    corr += 1
                    total_pos += marks
                elif outcome == "partial":
                    partial += 1
                    total_pos += marks
                elif outcome == "incorrect":
                    incorr += 1
                    total_neg -= marks
                elif outcome == "unattempted":
                    unatt += 1
                resp.save(update_fields=["typed_grading_snapshot", "key_confirmed_at_grading",
                    "correct_option_key_at_grading", "selected_option_key_at_grading", "is_correct", "marks_awarded"])
                continue
            guarded = (confirmed or question.is_pdf_derived or question.question_type != "mcq_single"
                       or len(correct_options) != 1)
            has_key = not guarded or (key_confirmed and question.question_type == "mcq_single" and len(correct_options) == 1)
            snapshot_key = guarded
            if snapshot_key:
                # Preserve the actual decision for PDF and classroom questions;
                # a later key edit must not rewrite a past attempt's outcome.
                resp.key_confirmed_at_grading = has_key
                resp.correct_option_key_at_grading = correct_options[0].option_key if has_key else None
                resp.selected_option_key_at_grading = resp.selected_option.option_key if resp.selected_option else None
            if not has_key:
                all_required_keys_verified = False
            if has_key:
                graded_marks += resp.exam_question.positive_marks
            if resp.selected_option is not None and not has_key:
                resp.is_correct = None
                resp.marks_awarded = Decimal("0.00")
            elif resp.selected_option is not None:
                if resp.selected_option.is_correct:
                    marks = resp.exam_question.positive_marks
                    total_pos += marks
                    corr += 1
                    resp.is_correct = True
                    resp.marks_awarded = marks
                else:
                    neg = resp.exam_question.negative_marks
                    total_neg += neg
                    incorr += 1
                    resp.is_correct = False
                    resp.marks_awarded = -neg
            else:
                unatt += 1
                resp.is_correct = None
                resp.marks_awarded = Decimal("0.00")
            fields = ["is_correct", "marks_awarded"]
            if snapshot_key:
                fields += ["key_confirmed_at_grading", "correct_option_key_at_grading", "selected_option_key_at_grading"]
            resp.save(update_fields=fields)

        net_score = total_pos - total_neg
        self.score_obtained = net_score
        self.correct_count = corr
        self.partial_count = partial
        self.incorrect_count = incorr
        self.unattempted_count = unatt

        # Accuracy is the share of fully correct answers among graded attempts;
        # partial-credit responses count in the denominator, not as incorrect.
        attempted_total = corr + partial + incorr
        if attempted_total > 0:
            self.accuracy_percentage = round(Decimal(corr) / Decimal(attempted_total) * Decimal("100.0"), 2)
        else:
            self.accuracy_percentage = Decimal("0.00")

        total_m = (graded_marks if confirmed or not all_required_keys_verified else
                   Decimal(str(self.total_marks)) if self.total_marks else Decimal("0.00"))
        if total_m > Decimal("0.00"):
            pct = (net_score / total_m) * Decimal("100.0")
            self.percentage = max(Decimal("0.00"), round(pct, 2))
        else:
            self.percentage = Decimal("0.00")

        passing_pct = Decimal(str(self.exam.passing_percentage)) if self.exam.passing_percentage else Decimal("0.00")
        # Never report a pass/fail result for a partially keyed paper. Views
        # show a pending/partial outcome rather than interpreting False as fail.
        if confirmed:
            self.is_passed = bool(all_required_keys_verified and graded_marks > 0 and passing_pct > 0 and self.percentage >= passing_pct)
        else:
            self.is_passed = bool(all_required_keys_verified and self.percentage >= passing_pct)
        return self.score_obtained

    def recalculate_ranks(self):
        """
        Recomputes ranks and percentiles across all submitted attempts of this exam.
        Standard competition tie-breaking:
        1. Higher score
        2. Fewer incorrect questions
        3. Faster completion time
        """
        completed = self.exam.attempts.filter(
            classroom_session__isnull=True,
            status__in=[self.AttemptStatus.SUBMITTED, self.AttemptStatus.EVALUATED, self.AttemptStatus.TIMED_OUT],
        )
        # A partial score cannot fairly compete with complete results. Historical
        # attempts with no key snapshot keep their existing legacy treatment.
        incomplete = completed.filter(responses__key_confirmed_at_grading=False)
        incomplete.update(rank=None, percentile=None)
        if self.has_ungraded_keys:
            self.rank = self.percentile = None
        attempts = list(completed.exclude(responses__key_confirmed_at_grading=False).order_by(
            "-score_obtained", "incorrect_count", "time_taken_seconds", "started_at"))
        total_cohort = len(attempts)
        if total_cohort == 0:
            return

        for i, att in enumerate(attempts):
            if i > 0:
                prev = attempts[i - 1]
                if (
                    att.score_obtained == prev.score_obtained
                    and att.incorrect_count == prev.incorrect_count
                    and att.time_taken_seconds == prev.time_taken_seconds
                ):
                    att.rank = prev.rank
                else:
                    att.rank = i + 1
            else:
                att.rank = 1

            if total_cohort >= 2:
                pctile = Decimal((total_cohort - (att.rank - 1)) / total_cohort * 100.0)
                att.percentile = round(pctile, 2)
            else:
                att.percentile = Decimal("100.00")

            att.save(update_fields=["rank", "percentile"])
            if att.id == self.id:
                self.rank = att.rank
                self.percentile = att.percentile

    def submit_attempt(self, is_timeout=False):
        """
        Submits the examination attempt atomically and evaluates candidate score.
        Prevents duplicate submissions and re-calculates exam leaderboard ranks.
        """
        if self.status in self.FINAL_STATUSES:
            return False

        now = timezone.now()
        self.status = self.AttemptStatus.TIMED_OUT if is_timeout else self.AttemptStatus.SUBMITTED
        self.submitted_at = now
        if not self.time_taken_seconds and self.started_at:
            end = (min(now, self.expires_at)
                   if (self.exam.is_private_practice or self.classroom_session_id) and is_timeout and self.expires_at else now)
            self.time_taken_seconds = max(0, int((end - self.started_at).total_seconds()))
        self.evaluate_attempt()
        self.save()
        # Classroom ranks are computed per session in-view, not globally.
        if not self.exam.is_private_practice and not self.classroom_session_id:
            self.recalculate_ranks()
        return True

    def terminate_for_security(self, reason=""):
        """End this attempt under Strict Exam Mode, keeping and evaluating all
        saved answers. Idempotent: a no-op once the attempt is already final."""
        if self.status in self.FINAL_STATUSES:
            return False
        now = timezone.now()
        self.status = self.AttemptStatus.TERMINATED
        self.is_security_locked = True
        self.submitted_at = now
        if not self.time_taken_seconds and self.started_at:
            self.time_taken_seconds = max(0, int((now - self.started_at).total_seconds()))
        # Preserve and score whatever was already saved; termination is not a zero.
        self.evaluate_attempt()
        self.save()
        if not self.exam.is_private_practice and not self.classroom_session_id:
            self.recalculate_ranks()
        return True

    def auto_timeout_if_expired(self):
        """Auto-submit once under the attempt lock; never recreate a deleted row."""
        if self.pk is None or self.status != self.AttemptStatus.IN_PROGRESS or not self.is_expired:
            return False
        with transaction.atomic():
            from django.db import connection
            from django.db.models import F
            # SQLite ignores SELECT FOR UPDATE. Acquire its writer lock before
            # reading the row so concurrent timeout/submit operations serialize.
            if connection.vendor == "sqlite":
                type(self).objects.filter(pk=self.pk).update(id=F("id"))
            # Classroom controls acquire session then attempt locks. Do not also
            # lock the joined exam: that would invert the order against end-now.
            locked = type(self).objects.select_for_update(of=("self",)).select_related("exam").filter(pk=self.pk).first()
            if not locked:  # The owner may have deleted the practice test.
                return False
            changed = (locked.status == self.AttemptStatus.IN_PROGRESS and locked.is_expired
                       and locked.submit_attempt(is_timeout=True))
        try:
            self.refresh_from_db()
        except type(self).DoesNotExist:
            return bool(changed)
        return bool(changed)


class ExamResponse(models.Model):
    """
    Candidate's saved response and palette state for a specific question in an attempt.
    """

    attempt = models.ForeignKey(ExamAttempt, on_delete=models.CASCADE, related_name="responses")
    exam_question = models.ForeignKey(ExamQuestion, on_delete=models.CASCADE, related_name="candidate_responses")
    selected_option = models.ForeignKey(
        "questions.QuestionOption",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="selected_in_responses",
    )
    # The existing FK remains authoritative for single-choice responses.
    # Multi IDs are validated against this question on every save/submit.
    selected_option_ids = models.JSONField(default=list, blank=True)
    numerical_response = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    # Grading-time answer, key, outcome and rule for new typed questions.
    # NULL deliberately means a legacy/in-progress row, never a reconstructed key.
    typed_grading_snapshot = models.JSONField(null=True, blank=True, editable=False)
    is_marked_for_review = models.BooleanField("Marked for Review", default=False)
    is_visited = models.BooleanField("Visited", default=False)
    time_spent_seconds = models.PositiveIntegerField("Time Spent (Seconds)", default=0)
    order = models.PositiveIntegerField("Candidate Display Order", default=0)
    marks_awarded = models.DecimalField("Marks Awarded", max_digits=5, decimal_places=2, default=0.0)
    is_correct = models.BooleanField("Is Correct", null=True, blank=True)
    # Nullable grading snapshots for classroom, private-practice and PDF items.
    # NULL means an older record (or an in-progress response), not an
    # unconfirmed key. Option text may be edited; these preserve saved labels.
    key_confirmed_at_grading = models.BooleanField(null=True, blank=True, editable=False)
    correct_option_key_at_grading = models.CharField(max_length=10, null=True, blank=True, editable=False)
    selected_option_key_at_grading = models.CharField(max_length=10, null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Candidate Exam Response"
        verbose_name_plural = "Candidate Exam Responses"
        unique_together = ("attempt", "exam_question")
        ordering = ["order", "id"]

    def __str__(self):
        return f"Attempt #{self.attempt_id} - Q#{self.order} ({self.palette_status})"

    @property
    def palette_status(self):
        """
        Returns one of: 'answered', 'not_answered', 'marked_for_review',
        'answered_and_marked', 'not_visited'.
        """
        if not self.is_visited:
            return "not_visited"
        answered = (self.selected_option_id is not None or bool(self.selected_option_ids)
                    or self.numerical_response is not None)
        if answered and self.is_marked_for_review:
            return "answered_and_marked"
        if self.is_marked_for_review:
            return "marked_for_review"
        if answered:
            return "answered"
        return "not_answered"


class ExamSecurityLog(models.Model):
    """
    Audit log of suspicious candidate events during an active examination session.
    Used for proctoring review, warning alerts, and tamper deterrence.
    """

    class EventType(models.TextChoices):
        TAB_SWITCH = "TAB_SWITCH", "Tab Switched / Hidden"
        WINDOW_BLUR = "WINDOW_BLUR", "Window Focus Lost"
        FULLSCREEN_EXIT = "FULLSCREEN_EXIT", "Exited Fullscreen"
        COPY_ATTEMPT = "COPY_ATTEMPT", "Copy / Cut Action"
        PASTE_ATTEMPT = "PASTE_ATTEMPT", "Paste Action"
        RIGHT_CLICK = "RIGHT_CLICK", "Right Click / Context Menu"
        KEY_VIOLATION = "KEY_VIOLATION", "Restricted Key Pressed"

    attempt = models.ForeignKey(ExamAttempt, on_delete=models.CASCADE, related_name="security_logs")
    event_type = models.CharField("Event Type", max_length=50, choices=EventType.choices)
    details = models.CharField("Details / Meta", max_length=255, blank=True)
    # Client-generated id for ONE visible->hidden episode. A unique (attempt,
    # client_event_id) pair makes strict counting idempotent: duplicate or delayed
    # reports of the same episode are recorded once. Blank for non-counting signals.
    client_event_id = models.CharField(max_length=64, blank=True, default="")
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Exam Security Log"
        verbose_name_plural = "Exam Security Logs"
        ordering = ["-timestamp"]
        constraints = [
            models.UniqueConstraint(
                fields=["attempt", "client_event_id"],
                condition=~models.Q(client_event_id=""),
                name="unique_attempt_client_event",
            )
        ]

    def __str__(self):
        return f"Attempt #{self.attempt_id} - {self.event_type} at {self.timestamp}"


class ClassroomSession(models.Model):
    """A teacher-led, join-code session of an exam (quiz-platform style).

    One exam (the test template) can have many sessions - e.g. one per class -
    each with its own unpredictable join code, its own attempts, counts and
    leaderboard. Attempts link via ExamAttempt.classroom_session, so results stay
    separate per session. There is NO application-imposed cap on participants.
    """

    # Unambiguous alphabet (no 0/O/1/I/L) for codes that are easy to read/type.
    CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    CODE_LENGTH = 6

    exam = models.ForeignKey("Exam", on_delete=models.CASCADE, related_name="classroom_sessions")
    teacher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="classroom_sessions")
    label = models.CharField("Session label", max_length=120, blank=True, help_text="e.g. 'Class 10-B, Monday'.")
    code = models.CharField(max_length=12, unique=True, db_index=True)
    # An optional one-shot form token makes a retried PDF-to-session POST
    # idempotent without limiting the number of sessions created from a paper.
    creation_token = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    is_open = models.BooleanField("Joining open", default=True)
    allow_retakes = models.BooleanField(
        "Allow retakes", default=False,
        help_text="If on, a student may take this session more than once; only their best graded attempt is ranked.",
    )
    show_leaderboard_to_students = models.BooleanField("Students can see leaderboard", default=False)
    results_released = models.BooleanField(
        "Release results and answer review to participants", default=False,
        help_text="Per-session release; does not change the underlying examination or other sessions.",
    )
    ended_at = models.DateTimeField(null=True, blank=True, help_text="Set when the teacher ends the session.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["teacher", "-created_at"]),
            models.Index(fields=["exam", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.exam.title} [{self.code}]"

    @classmethod
    def generate_code(cls):
        import secrets
        return "".join(secrets.choice(cls.CODE_ALPHABET) for _ in range(cls.CODE_LENGTH))

    @classmethod
    def create_for(cls, exam, teacher, **kwargs):
        """Create a session with a unique code, retrying safely on collision."""
        from django.db import IntegrityError
        for _ in range(12):
            try:
                with transaction.atomic():
                    return cls.objects.create(exam=exam, teacher=teacher, code=cls.generate_code(), **kwargs)
            except IntegrityError:
                continue
        raise RuntimeError("Could not allocate a unique join code; please retry.")

    def regenerate_code(self):
        from django.db import IntegrityError
        for _ in range(12):
            new_code = self.generate_code()
            if new_code == self.code:
                continue
            try:
                with transaction.atomic():
                    self.code = new_code
                    self.save(update_fields=["code", "updated_at"])
                    return new_code
            except IntegrityError:
                continue
        raise RuntimeError("Could not allocate a unique join code; please retry.")

    @property
    def is_ended(self):
        return self.ended_at is not None

    @property
    def student_results_visible(self):
        return bool(self.results_released or self.exam.show_result_immediately)

    @property
    def student_answers_visible(self):
        return bool(self.results_released or (self.exam.allow_review_answers and self.exam.show_result_immediately))

    @property
    def joining_allowed(self):
        """Whether NEW students may join. Existing attempts are unaffected by this."""
        return self.is_open and not self.is_ended and self.exam.status == Exam.ExamStatus.PUBLISHED

    def end_now(self):
        """Atomically close the session and finalize saved active attempts.

        Starts and end-now lock the same session first, so an attempt cannot be
        created after the teacher has ended the session.
        """
        from django.db import connection
        from django.db.models import F
        with transaction.atomic():
            if connection.vendor == "sqlite":
                type(self).objects.filter(pk=self.pk).update(id=F("id"))
            session = type(self).objects.select_for_update().get(pk=self.pk)
            if session.is_ended:
                self.ended_at, self.is_open = session.ended_at, session.is_open
                return False
            session.ended_at = timezone.now()
            session.is_open = False
            session.save(update_fields=["ended_at", "is_open", "updated_at"])
            for attempt in session.attempts.select_for_update(of=("self",)).select_related("exam").filter(status=ExamAttempt.AttemptStatus.IN_PROGRESS):
                attempt.submit_attempt(is_timeout=True)
            self.ended_at, self.is_open = session.ended_at, session.is_open
        return True

    def counts(self):
        """Distinct, server-truthful participation counts for live monitoring.

        All counts are distinct students, not attempts. Retake statuses can
        overlap: someone may have submitted and then started another attempt.
        """
        from django.db.models import Count, Q as _Q
        agg = self.attempts.aggregate(
            in_progress=Count("student_id", distinct=True, filter=_Q(status=ExamAttempt.AttemptStatus.IN_PROGRESS)),
            submitted=Count("student_id", distinct=True, filter=_Q(status__in=[ExamAttempt.AttemptStatus.SUBMITTED,
                                                        ExamAttempt.AttemptStatus.EVALUATED])),
            timed_out=Count("student_id", distinct=True, filter=_Q(status=ExamAttempt.AttemptStatus.TIMED_OUT)),
            terminated=Count("student_id", distinct=True, filter=_Q(status=ExamAttempt.AttemptStatus.TERMINATED)),
        )
        agg["joined"] = self.participants.count()
        agg["started"] = self.attempts.values("student").distinct().count()
        return agg


class ClassroomParticipant(models.Model):
    """A student who joined a classroom session by code.

    Joining registers identity (idempotently - a reconnect never double-counts)
    WITHOUT starting the timer, keeping "joined" distinct from "started". The
    actual attempt (and server timer) is created only when the student begins.
    """

    session = models.ForeignKey(ClassroomSession, on_delete=models.CASCADE, related_name="participants")
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="classroom_participations")
    joined_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["session", "student"], name="unique_session_participant"),
        ]
        indexes = [models.Index(fields=["session", "joined_at"])]
        ordering = ["joined_at", "id"]

    def __str__(self):
        return f"{self.student_id} in {self.session_id}"
