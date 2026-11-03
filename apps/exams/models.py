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
        return self.exams.filter(is_private_practice=False).count()

    @property
    def active_exam_count(self):
        return self.exams.filter(status=Exam.ExamStatus.PUBLISHED, is_private_practice=False).count()


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
    anti_cheat_warnings = models.PositiveIntegerField("Anti-Cheat Warnings", default=0)
    rank = models.PositiveIntegerField("Candidate Rank", null=True, blank=True)
    percentile = models.DecimalField("Percentile", max_digits=5, decimal_places=2, null=True, blank=True)
    correct_count = models.PositiveIntegerField("Correct Count", default=0)
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
            )
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
        return self.responses.filter(selected_option__isnull=False).count()

    @property
    def marked_review_count(self):
        return self.responses.filter(is_marked_for_review=True).count()

    @property
    def not_visited_count(self):
        return self.responses.filter(is_visited=False).count()

    @property
    def not_answered_count(self):
        return self.responses.filter(is_visited=True, selected_option__isnull=True).count()

    def evaluate_attempt(self):
        """
        Evaluates student's responses, calculating positive marks,
        negative penalties, net score, and pass/fail outcome.
        Stores question-level correctness and marks awarded snapshot.
        """
        total_pos = Decimal("0.00")
        total_neg = Decimal("0.00")
        corr = 0
        incorr = 0
        unatt = 0
        graded_marks = Decimal("0.00")
        all_private_keys_verified = True
        responses = self.responses.select_related("exam_question__question", "selected_option")
        if self.exam.is_private_practice:
            responses = responses.prefetch_related("exam_question__question__options")

        for resp in responses.all():
            # Student PDFs commonly contain no answer key. Never interpret a
            # missing key as an incorrect answer or invent the correct choice.
            question = resp.exam_question.question
            has_key = (not self.exam.is_private_practice or
                       (question.key_verified and sum(1 for opt in question.options.all() if opt.is_correct) == 1))
            if self.exam.is_private_practice and not has_key:
                all_private_keys_verified = False
            if has_key and self.exam.is_private_practice:
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
            resp.save(update_fields=["is_correct", "marks_awarded"])

        net_score = total_pos - total_neg
        self.score_obtained = net_score
        self.correct_count = corr
        self.incorrect_count = incorr
        self.unattempted_count = unatt

        attempted_total = corr + incorr
        if attempted_total > 0:
            self.accuracy_percentage = round(Decimal(corr) / Decimal(attempted_total) * Decimal("100.0"), 2)
        else:
            self.accuracy_percentage = Decimal("0.00")

        total_m = (graded_marks if self.exam.is_private_practice else
                   Decimal(str(self.total_marks)) if self.total_marks else Decimal("0.00"))
        if total_m > Decimal("0.00"):
            pct = (net_score / total_m) * Decimal("100.0")
            self.percentage = max(Decimal("0.00"), round(pct, 2))
        else:
            self.percentage = Decimal("0.00")

        passing_pct = Decimal(str(self.exam.passing_percentage)) if self.exam.passing_percentage else Decimal("0.00")
        # A private practice target is meaningful only with a complete,
        # student-confirmed key. It is not an institutional qualification.
        if self.exam.is_private_practice:
            self.is_passed = bool(all_private_keys_verified and graded_marks > 0 and passing_pct > 0 and self.percentage >= passing_pct)
        else:
            self.is_passed = self.percentage >= passing_pct
        return self.score_obtained

    def recalculate_ranks(self):
        """
        Recomputes ranks and percentiles across all submitted attempts of this exam.
        Standard competition tie-breaking:
        1. Higher score
        2. Fewer incorrect questions
        3. Faster completion time
        """
        attempts = list(
            self.exam.attempts.filter(
                status__in=[
                    self.AttemptStatus.SUBMITTED,
                    self.AttemptStatus.EVALUATED,
                    self.AttemptStatus.TIMED_OUT,
                ]
            ).order_by("-score_obtained", "incorrect_count", "time_taken_seconds", "started_at")
        )
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
        if self.status in [self.AttemptStatus.SUBMITTED, self.AttemptStatus.EVALUATED, self.AttemptStatus.TIMED_OUT]:
            return False

        now = timezone.now()
        self.status = self.AttemptStatus.TIMED_OUT if is_timeout else self.AttemptStatus.SUBMITTED
        self.submitted_at = now
        if not self.time_taken_seconds and self.started_at:
            end = (min(now, self.expires_at)
                   if self.exam.is_private_practice and is_timeout and self.expires_at else now)
            self.time_taken_seconds = max(0, int((end - self.started_at).total_seconds()))
        self.evaluate_attempt()
        self.save()
        if not self.exam.is_private_practice:
            self.recalculate_ranks()
        return True

    def auto_timeout_if_expired(self):
        """Auto-submit once; serialize private timeout with answer saves."""
        if self.status != self.AttemptStatus.IN_PROGRESS or not self.is_expired:
            return False
        if not self.exam.is_private_practice:
            return self.submit_attempt(is_timeout=True)
        with transaction.atomic():
            locked = type(self).objects.select_for_update().select_related("exam").filter(pk=self.pk).first()
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
    is_marked_for_review = models.BooleanField("Marked for Review", default=False)
    is_visited = models.BooleanField("Visited", default=False)
    time_spent_seconds = models.PositiveIntegerField("Time Spent (Seconds)", default=0)
    order = models.PositiveIntegerField("Candidate Display Order", default=0)
    marks_awarded = models.DecimalField("Marks Awarded", max_digits=5, decimal_places=2, default=0.0)
    is_correct = models.BooleanField("Is Correct", null=True, blank=True)
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
        if self.selected_option is not None and self.is_marked_for_review:
            return "answered_and_marked"
        if self.is_marked_for_review:
            return "marked_for_review"
        if self.selected_option is not None:
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
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Exam Security Log"
        verbose_name_plural = "Exam Security Logs"
        ordering = ["-timestamp"]

    def __str__(self):
        return f"Attempt #{self.attempt_id} - {self.event_type} at {self.timestamp}"
