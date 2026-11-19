"""Owner-only PDF-to-practice workflow; never uses publicly served media."""
from decimal import Decimal
import uuid

import pymupdf as fitz
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db import OperationalError, connection, transaction
from django.db.models import Exists, Max, OuterRef, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.cache import patch_cache_control
from django.views import View

from apps.accounts.models import User
from apps.core.permissions import StudentRequiredMixin
from apps.core.exam_targets import targets_for_track
from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion
from apps.payments.services import QuotaExceeded, FeatureUnavailable, consume, lock_practice_user, require_feature
from apps.core.utils import log_audit
from .answer_key_validation import answer_keys_set, confirmed_staged_key, edited_answer_status, staged_question_signature
from .figure_utils import page_image, validate_box
from .figure_views import _png_upload, _target
from .models import AnswerStatus, ExtractedFigure, ExtractedQuestion, Question, QuestionFigure, QuestionOption, QuestionPaperUpload
from .practice_forms import PracticeQuestionForm, PracticeSettingsForm, PracticeUploadForm, practice_question_error
from .presentation import answer_key_preview_questions, decorate_staged_question, user_diagnostic
from .tasks import extract_cloud_paper, extract_private_practice_paper


def active_private_attempt(user):
    # Hide all answer-bearing workspaces while any private test is active.
    # Expired sessions are finalized before deciding whether access is blocked.
    attempts = ExamAttempt.objects.filter(
        student=user, exam__is_private_practice=True,
        status=ExamAttempt.AttemptStatus.IN_PROGRESS,
    ).select_related("exam").order_by("started_at")
    for attempt in attempts:
        if attempt.is_expired:
            attempt.auto_timeout_if_expired()
        else:
            return attempt
    return None


class PrivateStudentMixin(StudentRequiredMixin):
    sensitive_during_attempt = False

    def dispatch(self, request, *args, **kwargs):
        try:
            if (self.sensitive_during_attempt and request.user.is_authenticated
                    and request.user.is_active and request.user.is_student):
                # Coordinate with private test starts, which also lock this user.
                with transaction.atomic():
                    # A track update may have committed while this request was
                    # waiting. All authorization below must use the locked row.
                    request.user = lock_practice_user(request.user)
                    # This request may have entered before a concurrent email
                    # change committed. Preserve the middleware's exception for
                    # existing results, but block new review/test work.
                    from apps.accounts.middleware import EmailVerificationMiddleware
                    view_name = getattr(request.resolver_match, "view_name", None)
                    if request.user.needs_email_verification and view_name not in EmailVerificationMiddleware.ALLOWED:
                        response = redirect("accounts:verification_pending")
                    else:
                        active = active_private_attempt(request.user)
                        if active:
                            messages.info(request, "Finish your active practice test before opening answers, the source paper or question bank.")
                            response = redirect("exams:exam_session", attempt_uuid=active.attempt_uuid)
                        else:
                            response = super().dispatch(request, *args, **kwargs)
            else:
                response = super().dispatch(request, *args, **kwargs)
        except User.DoesNotExist:
            raise Http404() from None
        except OperationalError as exc:
            if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                raise
            response = HttpResponse("Practice is busy. Please retry; your active timer continues.", status=503)
            response["Retry-After"] = "1"
        patch_cache_control(response, private=True, no_store=True)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response


def owned_paper(user, pk):
    # Unknown legacy papers remain available to their owner for review and
    # explicit classification. Known papers from the other track are hidden.
    allowed = targets_for_track(user.exam_track)
    return get_object_or_404(QuestionPaperUpload.objects.filter(Q(target_type__in=allowed) | Q(target_type="")),
                             pk=pk, uploaded_by=user, is_student_private=True)


def owned_question(user, pk):
    allowed = targets_for_track(user.exam_track)
    return get_object_or_404(
        ExtractedQuestion.objects.select_related("paper").prefetch_related("figures").filter(
            Q(paper__target_type__in=allowed) | Q(paper__target_type="")),
        pk=pk, paper__uploaded_by=user, paper__is_student_private=True,
    )


def editable(paper):
    if paper.practice_exam_id or paper.status in (paper.ProcessingStatus.PENDING, paper.ProcessingStatus.PROCESSING):
        raise Http404("This paper is being processed or its test has already been created.")


def _private_file_response(field, content_type):
    if not field:
        raise Http404()
    try:
        handle = field.open("rb")
    except (OSError, ValueError):
        raise Http404() from None
    response = FileResponse(handle, content_type=content_type)
    patch_cache_control(response, private=True, no_store=True)
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _open_private_pdf(paper):
    try:
        with paper.private_pdf_file.open("rb") as source:
            return fitz.open(stream=source.read(), filetype="pdf")
    except (OSError, ValueError, RuntimeError):
        raise Http404("The private source PDF is unavailable.") from None


def _system_category():
    # A private inactive category lets students start without an administrator
    # needing to set up public categories. It contains no public exam content.
    name = "Student Private Practice (System)"
    if Category.objects.filter(name=name).exclude(slug="student-private-practice").exists():
        name = f"Student Private Practice (System {uuid.uuid4().hex[:8]})"
    category, _ = Category.objects.get_or_create(
        slug="student-private-practice", defaults={"name": name, "is_active": False},
    )
    return category


class PracticeListView(PrivateStudentMixin, View):
    def get(self, request):
        # Reconnecting to the list also finalizes any expired attempts, even if
        # the optional background worker was offline.
        overdue = ExamAttempt.objects.filter(
            student=request.user, exam__is_private_practice=True,
            status=ExamAttempt.AttemptStatus.IN_PROGRESS, expires_at__lte=timezone.now(),
        ).select_related("exam")
        for attempt in overdue:
            attempt.auto_timeout_if_expired()
        allowed = targets_for_track(request.user.exam_track)
        active = ExamAttempt.objects.filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
            student=request.user, status=ExamAttempt.AttemptStatus.IN_PROGRESS,
        )
        paper_conflicts = ExamQuestion.objects.filter(exam_id=OuterRef("practice_exam_id")).exclude(question__target_type__in=allowed)
        papers = (QuestionPaperUpload.objects.filter(uploaded_by=request.user, is_student_private=True)
                  .annotate(needs_exam_review=Exists(paper_conflicts),
                            has_active_attempt=Exists(active.filter(exam_id=OuterRef("practice_exam_id"))))
                  .filter(Q(target_type__in=allowed) | Q(target_type="") | Q(has_active_attempt=True))
                  .select_related("practice_exam").prefetch_related("practice_exam__attempts"))
        # Do not share institutional result metrics with ungraded practice.
        bank_conflicts = ExamQuestion.objects.filter(exam_id=OuterRef("pk")).exclude(question__target_type__in=allowed)
        bank_tests = (Exam.objects.filter(creator=request.user, is_private_practice=True, private_source_paper__isnull=True)
                      .annotate(needs_exam_review=Exists(bank_conflicts),
                                has_active_attempt=Exists(active.filter(exam_id=OuterRef("pk"))))
                      .filter(Q(target_type__in=allowed) | Q(target_type="") | Q(has_active_attempt=True))
                      .prefetch_related("attempts").order_by("-created_at"))
        return render(request, "questions/practice_list.html", {"papers": papers, "bank_tests": bank_tests})


class PracticeUploadView(PrivateStudentMixin, View):
    def get(self, request):
        return render(request, "questions/practice_upload.html", {"form": PracticeUploadForm(user=request.user), "max_pages": settings.PRACTICE_MAX_PDF_PAGES})

    def post(self, request):
        form = PracticeUploadForm(request.POST, request.FILES, user=request.user)
        if not form.is_valid():
            return render(request, "questions/practice_upload.html", {"form": form, "max_pages": settings.PRACTICE_MAX_PDF_PAGES})
        upload = form.cleaned_data["pdf_file"]
        paper = None

        def discard_rolled_back_file():
            # Django file storage is not transactional. If the DB operation
            # rolled back after writing the private upload, remove that orphan.
            field = paper.private_pdf_file if paper is not None else None
            if field and getattr(field, "_committed", False):
                try:
                    field.storage.delete(field.name)
                except OSError:
                    import logging
                    logging.getLogger(__name__).warning("Could not remove a rolled-back private upload.")

        try:
            with transaction.atomic():
                user = lock_practice_user(request.user)
                if not user.is_active or not user.is_student:
                    raise PermissionDenied()
                if user.needs_email_verification:
                    return redirect("accounts:verification_pending")
                if form.cleaned_data["target_type"] not in targets_for_track(user.exam_track):
                    form.add_error("target_type", "Your profile track changed. Choose a paper for your current exam.")
                    return render(request, "questions/practice_upload.html", {"form": form, "max_pages": settings.PRACTICE_MAX_PDF_PAGES})
                # Private practice extraction is local-only; PDFs are never sent
                # to external providers (enforced by the form's local-only mode).
                from apps.payments.packs import debit
                debit_record = debit(user, "pdf", f"pending-pdf:{uuid.uuid4()}")
                paper = QuestionPaperUpload(
                    is_student_private=True, title=form.cleaned_data["title"],
                    subject=form.cleaned_data["subject"], target_type=form.cleaned_data["target_type"], category=_system_category(),
                    uploaded_by=user, private_pdf_file=upload, file_size_bytes=upload.size,
                    total_pages=form.pdf_page_count,
                    extraction_mode=QuestionPaperUpload.ExtractionMode.LOCAL,
                    image_first=form.cleaned_data.get("image_first", True),
                )
                paper.save()
                debit_record.resource_key = f"pdf:{paper.pk}"
                debit_record.save(update_fields=["resource_key"])
        except (QuotaExceeded, FeatureUnavailable) as exc:
            discard_rolled_back_file()
            messages.error(request, str(exc))
            return redirect("payments:plans")
        except OperationalError as exc:
            discard_rolled_back_file()
            if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                raise
            messages.warning(request, "Practice is busy. Please retry your upload.")
            return redirect("questions:practice_upload")
        except Exception:
            discard_rolled_back_file()
            raise
        log_audit(request.user, "PRIVATE_PDF_UPLOADED", f"Private paper {paper.pk} uploaded.", request=request)
        task = extract_private_practice_paper
        try:
            if settings.CELERY_TASK_ALWAYS_EAGER:
                task(paper.pk)
            else:
                task.delay(paper.pk)
        except Exception:
            # A broker timeout is ambiguous: the worker may already have taken
            # the job. Cancel only a still-PENDING job via the same atomic claim
            # used by the worker. A late delivery will then refuse to run it.
            pending = [QuestionPaperUpload.ProcessingStatus.PENDING]
            if settings.CELERY_TASK_ALWAYS_EAGER and not paper.extracted_questions.exists() and not paper.figures.exists():
                pending.append(QuestionPaperUpload.ProcessingStatus.PROCESSING)
            cancelled = QuestionPaperUpload.objects.filter(pk=paper.pk, status__in=pending).update(
                status=QuestionPaperUpload.ProcessingStatus.FAILED, updated_at=timezone.now(),
                extraction_log="Extraction did not start. Your PDF is saved; you can add questions manually.",
            )
            if cancelled:
                from apps.payments.packs import reverse_failed_pdf
                returned = reverse_failed_pdf(f"pdf:{paper.pk}")
                if returned:
                    messages.warning(request, "Extraction could not be completed. Your PDF is saved for manual review and your credit was returned.")
                else:
                    messages.warning(request, "Extraction could not be completed. Your PDF is saved for manual review; contact support if credit reconciliation is needed.")
            else:
                # An eager worker may have already marked the empty failure and
                # reversed its credit before re-raising. Do not mislabel that
                # definite outcome as an uncertain broker response.
                from apps.payments.models import CreditUse
                if CreditUse.objects.filter(resource_key=f"pdf:{paper.pk}", reversed_at__isnull=False).exists():
                    messages.warning(request, "Extraction failed. Your PDF is saved for manual review and your credit was returned.")
                else:
                    messages.warning(request, "The queue response was uncertain. Processing may already be underway; your PDF and credit record are preserved. Contact support if it remains stuck.")
        return redirect("questions:practice_settings", pk=paper.pk)


class PracticeSettingsView(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    def _context(self, paper, form=None):
        questions = list(paper.extracted_questions.prefetch_related("figures", "ai_suggestions").order_by("question_number", "id"))
        included = [q for q in questions if q.include_in_practice]
        for question in questions:
            decorate_staged_question(question, image_first=paper.image_first, private=True)
            question.practice_error = practice_question_error(question, allow_confirmed_crop_choices=True)
            question.preview_confirmed_key = confirmed_staged_key(question, AnswerStatus.STUDENT_CONFIRMED)
            question.latest_ai = next(iter(question.ai_suggestions.all()), None)
        return {
            "paper": paper, "questions": questions, "included": included,
            "total_questions": len(included),
            "answer_key_preview_questions": answer_key_preview_questions(included, student=True),
            "total_marks": sum((q.positive_marks for q in included), Decimal("0.00")),
            "keyed_count": sum(bool(confirmed_staged_key(q, AnswerStatus.STUDENT_CONFIRMED)) for q in included),
            "flagged_count": sum(q.is_flagged for q in included),
            "unassigned_figure_count": paper.figures.filter(question__isnull=True).count(),
            "extraction_warnings": [user_diagnostic(line.removeprefix("Warning: ")) for line in paper.extraction_log.splitlines() if line.startswith("Warning: ")],
            "extraction_log_display": user_diagnostic(paper.extraction_log),
            "cloud_usage": [line for line in paper.extraction_log.splitlines() if line.startswith((
                "Gemini model:", "Gemini reported", "Gemini did not report usable token counts",
            ))],
            "form": form or PracticeSettingsForm(questions=included, unassigned_count=paper.figures.filter(question__isnull=True).count()),
            "presets": [x for x in (15, 30, 45, 60, 90, 120) if settings.PRACTICE_MIN_DURATION_MINUTES <= x <= settings.PRACTICE_MAX_DURATION_MINUTES],
        }

    def get(self, request, pk):
        paper = owned_paper(request.user, pk)
        if paper.practice_exam_id:
            return redirect("exams:exam_instructions", slug=paper.practice_exam.slug)
        return render(request, "questions/practice_settings.html", self._context(paper))

    def post(self, request, pk):
        owned_paper(request.user, pk)
        # Serializing on the paper prevents duplicate exams and simultaneous
        # edits from changing the preview while it is being saved.
        with transaction.atomic():
            paper = get_object_or_404(QuestionPaperUpload.objects.select_for_update(), pk=pk, uploaded_by=request.user, is_student_private=True)
            if paper.practice_exam_id:
                return redirect("exams:exam_instructions", slug=paper.practice_exam.slug)
            if paper.target_type not in targets_for_track(request.user.exam_track):
                messages.error(request, "Choose this paper's target exam before creating the test.")
                return redirect("questions:practice_settings", pk=paper.pk)
            editable(paper)
            included = list(paper.extracted_questions.filter(include_in_practice=True).prefetch_related("figures").order_by("question_number", "id"))
            form = PracticeSettingsForm(request.POST, questions=included, unassigned_count=paper.figures.filter(question__isnull=True).count())
            if not form.is_valid():
                return render(request, "questions/practice_settings.html", self._context(paper, form))
            # Creating a test from an already paid-for paper does not consume
            # another PDF or classroom credit.
            exam = Exam.objects.create(
                is_private_practice=True, slug=f"practice-{uuid.uuid4().hex}", title=paper.title,
                subject=paper.subject, target_type=paper.target_type, category=paper.category, creator=request.user,
                status=Exam.ExamStatus.PUBLISHED, is_free=True, max_attempts=0,
                duration_minutes=form.cleaned_data["duration_minutes"],
                total_marks=Decimal("0.00"), passing_percentage=form.cleaned_data["passing_percentage"] or Decimal("0.00"),
                shuffle_questions=False, shuffle_options=False, image_first_mode=bool(paper.image_first),
                show_result_immediately=True, allow_review_answers=True,
                description="Private practice from your PDF. Only questions with a supplied answer key are graded.",
            )
            for position, staged in enumerate(included, 1):
                # A historical or extracted suggestion is not a confirmation.
                # Keep it in staging but grade only an explicitly entered key or
                # one the student previously confirmed in the question editor.
                saved_key = confirmed_staged_key(staged, AnswerStatus.STUDENT_CONFIRMED)
                correct = form.answer_keys.get(staged.question_number, saved_key)
                is_typed = staged.detected_type in (ExtractedQuestion.DetectedType.MULTIPLE, ExtractedQuestion.DetectedType.NUMERICAL)
                labels = (set(answer_keys_set(correct)) if staged.detected_type == ExtractedQuestion.DetectedType.MULTIPLE
                          else {correct} if staged.detected_type == ExtractedQuestion.DetectedType.SINGLE and correct else set())
                bank = Question.objects.create(
                    is_private_practice=True, key_verified=bool(correct), is_image_first=bool(staged.is_image_first),
                    question_type=staged.detected_type, typed_key_verified=bool(is_typed and correct),
                    numeric_key=Decimal(correct) if correct and staged.detected_type == ExtractedQuestion.DetectedType.NUMERICAL else None,
                    numeric_tolerance=staged.numeric_tolerance, multi_scoring_mode=staged.multi_scoring_mode,
                    partial_credit_per_choice=staged.partial_credit_per_choice,
                    answer_status=AnswerStatus.STUDENT_CONFIRMED if correct else AnswerStatus.NEEDS_REVIEW, category=paper.category, creator=request.user,
                    source_paper=paper, target_type=paper.target_type, subject=paper.subject or "General",
                    question_text=staged.question_text, tables=staged.tables, source_regions=staged.source_regions, primary_image_region=staged.primary_image_region, explanation=staged.explanation,
                    private_image=staged.private_image.name,
                    positive_marks=staged.positive_marks, negative_marks=staged.negative_marks,
                )
                options = {}
                for index, option in enumerate(staged.options):
                    key = option["key"].upper().strip()
                    options[key] = QuestionOption.objects.create(
                        question=bank, option_key=key, option_text=option.get("text", ""),
                        is_correct=key in labels, order=index,
                    )
                for figure in staged.figures.all():
                    if figure.private_image:
                        QuestionFigure.objects.create(
                            question=bank, option=options.get(figure.option_key) if figure.option_key else None,
                            private_image=figure.private_image.name, order=figure.order, page_number=figure.page_number,
                            bounding_box=figure.bounding_box, source=figure.source,
                        )
                ExamQuestion.objects.create(
                    exam=exam, question=bank, order=position, section=paper.subject or "General",
                    positive_marks=staged.positive_marks, negative_marks=staged.negative_marks,
                )
                updates = ["approved_question", "status"]
                if correct:
                    staged.correct_option = correct
                    staged.answer_status = AnswerStatus.STUDENT_CONFIRMED
                    staged.typed_key_verified = is_typed
                    updates.extend(["correct_option", "answer_status", "typed_key_verified"])
                staged.approved_question = bank
                staged.status = ExtractedQuestion.Status.APPROVED
                staged.save(update_fields=updates)
            exam.recalculate_marks()
            paper.practice_exam = exam
            paper.status = QuestionPaperUpload.ProcessingStatus.COMPLETED
            paper.questions_approved = len(included)
            paper.save(update_fields=["practice_exam", "status", "questions_approved"])
        log_audit(request.user, "PRIVATE_TEST_CREATED", f"Private test {exam.pk} created from a paper.", request=request)
        messages.success(request, f"Your private {exam.duration_minutes}-minute practice test is ready.")
        return redirect("exams:exam_instructions", slug=exam.slug)


class PracticeQuestionEditView(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    def get(self, request, pk):
        question = owned_question(request.user, pk)
        editable(question.paper)
        return render(request, "questions/practice_question_edit.html", {"question": question, "form": PracticeQuestionForm(question=question)})

    def post(self, request, pk):
        initial = owned_question(request.user, pk)
        with transaction.atomic():
            paper = get_object_or_404(QuestionPaperUpload.objects.select_for_update(), pk=initial.paper_id, uploaded_by=request.user, is_student_private=True)
            editable(paper)
            question = owned_question(request.user, pk)
            form = PracticeQuestionForm(request.POST, question=question)
            if not form.is_valid():
                return render(request, "questions/practice_question_edit.html", {"question": question, "form": form})
            data = form.cleaned_data
            previous_content = staged_question_signature(question)
            previous_key, previous_typed = question.correct_option, question.typed_key_verified
            figure_keys = {f.option_key for f in question.figures.all() if f.private_image}
            question.detected_type = data["detected_type"]
            question.question_text = data["question_text"]
            question.tables = data["tables_data"]
            crop_keys = set(data.get("visible_crop_choices") or [])
            question.options = [{"key": k, "text": data[f"option_{k}"].strip(), "visible_in_crop": k in crop_keys}
                                for k in "ABCDE" if data[f"option_{k}"].strip() or k in figure_keys or k in crop_keys]
            question.numeric_tolerance = data["numeric_tolerance"]
            question.multi_scoring_mode = data["multi_scoring_mode"]
            question.partial_credit_per_choice = data["partial_credit_per_choice"]
            changed = previous_content != staged_question_signature(question)
            key = data.get("answer_key_value", "")
            question.answer_status = edited_answer_status(previous_key, question.answer_status,
                key, data["confirm_answer_key"], AnswerStatus.STUDENT_CONFIRMED, content_changed=changed)
            question.typed_key_verified = bool(question.detected_type in (ExtractedQuestion.DetectedType.MULTIPLE, ExtractedQuestion.DetectedType.NUMERICAL)
                and key and question.answer_status == AnswerStatus.STUDENT_CONFIRMED
                and (data["confirm_answer_key"] or previous_typed and not changed and previous_key == key))
            question.correct_option = key
            question.positive_marks = data["positive_marks"]
            question.negative_marks = data["negative_marks"]
            question.explanation = data["explanation"]
            question.is_flagged = not data["reviewed"]
            question.flag_reason = "" if data["reviewed"] else "Not yet checked against source."
            question.status = ExtractedQuestion.Status.REVIEWED
            question.save(update_fields=["detected_type", "question_text", "tables", "options", "correct_option", "answer_status", "typed_key_verified", "numeric_tolerance", "multi_scoring_mode", "partial_credit_per_choice", "positive_marks", "negative_marks", "explanation", "is_flagged", "flag_reason", "status"])
        if question.correct_option and (question.answer_status != AnswerStatus.STUDENT_CONFIRMED or
                question.detected_type != ExtractedQuestion.DetectedType.SINGLE and not question.typed_key_verified):
            messages.warning(request, "Question saved. Its answer is unconfirmed and will remain ungraded until you check it.")
        else:
            messages.success(request, "Question saved.")
        return redirect("questions:practice_settings", pk=question.paper_id)


class PracticeQuestionIncludeView(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    def post(self, request, pk):
        initial = owned_question(request.user, pk)
        with transaction.atomic():
            paper = get_object_or_404(QuestionPaperUpload.objects.select_for_update(), pk=initial.paper_id, uploaded_by=request.user, is_student_private=True)
            editable(paper)
            question = owned_question(request.user, pk)
            question.include_in_practice = not question.include_in_practice
            question.save(update_fields=["include_in_practice"])
        return redirect("questions:practice_settings", pk=paper.pk)


class PracticeQuestionAddView(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    def post(self, request, pk):
        owned_paper(request.user, pk)
        with transaction.atomic():
            paper = get_object_or_404(QuestionPaperUpload.objects.select_for_update(), pk=pk, uploaded_by=request.user, is_student_private=True)
            editable(paper)
            number = (paper.extracted_questions.aggregate(value=Max("question_number"))["value"] or 0) + 1
            question = ExtractedQuestion.objects.create(
                paper=paper, question_number=number, question_text="", options=[],
                is_flagged=True, flag_reason="Manually added; review before creating the test.",
            )
            paper.questions_found = paper.extracted_questions.count()
            paper.status = QuestionPaperUpload.ProcessingStatus.EXTRACTED
            paper.save(update_fields=["questions_found", "status"])
        return redirect("questions:practice_question_edit", pk=question.pk)


class PracticePdfView(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    def get(self, request, pk):
        paper = owned_paper(request.user, pk)
        response = _private_file_response(paper.private_pdf_file, "application/pdf")
        response["Content-Disposition"] = 'inline; filename="practice-source.pdf"'
        return response


class PracticeSourcePageView(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    def get(self, request, pk, page_number):
        question = owned_question(request.user, pk)
        with _open_private_pdf(question.paper) as document:
            if not 1 <= page_number <= len(document):
                raise Http404()
            try:
                png = page_image(document[page_number - 1], max_side=1800, scale=1.8)
            except ValueError:
                raise Http404() from None
        return HttpResponse(png, content_type="image/png")


class PracticeSourceRegionView(PrivateStudentMixin, View):
    """A reviewer-only excerpt; never available during an active practice test."""
    sensitive_during_attempt = True
    def get(self, request, pk, index):
        question = owned_question(request.user, pk)
        try:
            region = question.source_regions[index]
            page_number, box = int(region["page"]), validate_box(region["box"])
        except (IndexError, KeyError, TypeError, ValueError):
            raise Http404() from None
        with _open_private_pdf(question.paper) as document:
            if not 1 <= page_number <= len(document):
                raise Http404()
            try:
                png = page_image(document[page_number - 1], box, max_side=1800, scale=2)
            except ValueError:
                raise Http404() from None
        return HttpResponse(png, content_type="image/png")


class PracticeExtractedImageView(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    def get(self, request, pk):
        return _private_file_response(owned_question(request.user, pk).private_image, "image/png")


class PracticeFigureFileView(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    def get(self, request, pk):
        figure = get_object_or_404(ExtractedFigure, pk=pk, paper__uploaded_by=request.user, paper__is_student_private=True)
        owned_paper(request.user, figure.paper_id)
        return _private_file_response(figure.private_image, "image/png")


def _can_view_bank_asset(request, question):
    """Track-scoped assets, with explicit exceptions for owned attempts.

    A live pre-upgrade attempt must keep its images. Older result pages supply
    an attempt UUID so past results still work after a student changes tracks,
    without making every opposite-track bank image accessible by guessing IDs.
    """
    if question.target_type in targets_for_track(request.user.exam_track) or not question.target_type:
        return True
    related = question.exam_instances.filter(candidate_responses__attempt__student=request.user,
                                              exam__is_private_practice=True, exam__creator=request.user)
    if related.filter(
        Q(candidate_responses__attempt__expires_at__isnull=True) |
        Q(candidate_responses__attempt__expires_at__gt=timezone.now()),
        candidate_responses__attempt__status=ExamAttempt.AttemptStatus.IN_PROGRESS,
    ).exists():
        return True
    try:
        attempt_id = uuid.UUID(request.GET.get("attempt", ""))
    except (ValueError, TypeError, AttributeError):
        return False
    if active_private_attempt(request.user):
        return False
    return related.filter(candidate_responses__attempt__attempt_uuid=attempt_id,
                          candidate_responses__attempt__status__in=ExamAttempt.FINAL_STATUSES).exists()


class PracticeBankImageView(PrivateStudentMixin, View):
    def get(self, request, pk):
        question = get_object_or_404(Question, pk=pk, creator=request.user, is_private_practice=True)
        if not _can_view_bank_asset(request, question):
            raise Http404()
        return _private_file_response(question.private_image, "image/png")


class PracticeBankFigureView(PrivateStudentMixin, View):
    def get(self, request, pk):
        figure = get_object_or_404(QuestionFigure.objects.select_related("question"), pk=pk,
                                   question__creator=request.user, question__is_private_practice=True)
        if not _can_view_bank_asset(request, figure.question):
            raise Http404()
        return _private_file_response(figure.private_image, "image/png")


class PracticeFigureManageView(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    def get(self, request, pk):
        question = owned_question(request.user, pk)
        editable(question.paper)
        return render(request, "questions/figure_manage.html", {
            "question": question, "figures": question.figures.all(), "student_practice": True,
            "page_range": range(1, question.paper.total_pages + 1),
        })

    def post(self, request, pk):
        initial = owned_question(request.user, pk)
        with transaction.atomic():
            paper = get_object_or_404(QuestionPaperUpload.objects.select_for_update(), pk=initial.paper_id, uploaded_by=request.user, is_student_private=True)
            editable(paper)
            question = owned_question(request.user, pk)
            action = request.POST.get("action", "")
            try:
                if action == "remove_primary":
                    old = question.private_image.name
                    question.private_image = ""
                    question.primary_image_region = {}
                    question.save(update_fields=["private_image", "primary_image_region"])
                    if old:
                        transaction.on_commit(lambda: question.private_image.storage.delete(old))
                elif action in ("retarget", "unassign", "delete"):
                    figure = get_object_or_404(ExtractedFigure, pk=request.POST.get("figure_id"), question=question, paper=paper)
                    if action == "retarget":
                        figure.option_key = _target(request.POST.get("target"))
                        if figure.option_key and figure.source == "question_crop":
                            figure.source = "manual_crop"
                        figure.save(update_fields=["option_key", "source"])
                        self._ensure_option(question, figure.option_key)
                    elif action == "unassign":
                        figure.question = None
                        figure.option_key = ""
                        figure.save(update_fields=["question", "option_key"])
                    else:
                        old = figure.private_image.name
                        figure.delete()
                        if old:
                            transaction.on_commit(lambda: figure.private_image.storage.delete(old))
                elif action in ("crop", "upload"):
                    raw_target = request.POST.get("target", "")
                    original_crop = raw_target == "__crop__"
                    target = "" if original_crop else _target(raw_target)
                    replace_id = request.POST.get("replace_id", "")
                    figure = get_object_or_404(ExtractedFigure, pk=replace_id, question=question, paper=paper) if replace_id else ExtractedFigure(paper=paper, question=question)
                    keep_crop = original_crop or (figure.source == "question_crop" and not target)
                    old = figure.private_image.name
                    if action == "crop":
                        try:
                            page_number = int(request.POST.get("page_number", ""))
                            box = validate_box([request.POST.get(k, "") for k in ("x0", "y0", "x1", "y1")])
                        except (ValueError, TypeError):
                            raise ValueError("Choose a valid page and a non-empty rectangle inside it.") from None
                        with _open_private_pdf(paper) as document:
                            if not 1 <= page_number <= len(document):
                                raise ValueError("That source page does not exist.")
                            png = page_image(document[page_number - 1], box, max_side=3200, scale=3) if keep_crop else page_image(document[page_number - 1], box)
                        figure.page_number, figure.bounding_box, figure.source = page_number, box, "question_crop" if keep_crop else "manual_crop"
                    else:
                        png = _png_upload(request.FILES.get("image"))
                        figure.page_number, figure.bounding_box, figure.source = question.page_number, [], "question_crop" if keep_crop else "upload"
                    figure.option_key = target
                    figure.private_image.save(f"practice_{paper.pk}_figure.png", ContentFile(png), save=False)
                    figure.save()
                    if old:
                        transaction.on_commit(lambda: figure.private_image.storage.delete(old))
                    self._ensure_option(question, target)
                else:
                    raise ValueError("Unknown image operation.")
                question.is_flagged = True
                question.flag_reason = "Image assignment changed; check the question against its source."
                question.answer_status = AnswerStatus.NEEDS_REVIEW
                question.save(update_fields=["options", "is_flagged", "flag_reason", "answer_status"])
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("questions:practice_figures", pk=question.pk)
        messages.success(request, "Image changes saved. Review the question before creating the test.")
        return redirect("questions:practice_figures", pk=question.pk)

    @staticmethod
    def _ensure_option(question, key):
        if key and not any(opt.get("key") == key for opt in question.options if isinstance(opt, dict)):
            question.options = list(question.options) + [{"key": key, "text": ""}]
            question.options.sort(key=lambda opt: opt.get("key", ""))


class PracticeResultView(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    def get(self, request, attempt_uuid):
        attempt = get_object_or_404(
            ExamAttempt.objects.select_related("exam"), attempt_uuid=attempt_uuid,
            student=request.user, exam__creator=request.user, exam__is_private_practice=True,
        )
        attempt.auto_timeout_if_expired()
        if attempt.status == ExamAttempt.AttemptStatus.IN_PROGRESS:
            return redirect("exams:exam_session", attempt_uuid=attempt.attempt_uuid)
        from .practice_analytics import compute_practice_result
        result = compute_practice_result(attempt)
        from .practice_estimate_views import estimate_context
        result.update(estimate_context(attempt, result["rows"]))
        log_audit(request.user, "PRACTICE_RESULT_VIEWED", f"Private attempt {attempt.attempt_uuid} viewed.", request=request)
        history = (ExamAttempt.objects.filter(exam=attempt.exam, student=request.user)
                   .exclude(status=ExamAttempt.AttemptStatus.IN_PROGRESS)
                   .prefetch_related("responses__exam_question").order_by("-started_at", "-pk"))
        for previous in history:
            saved = list(previous.responses.all())
            previous.comparison_legacy = any(row.key_confirmed_at_grading is None for row in saved)
            previous.comparison_keyed = any(row.key_confirmed_at_grading is True for row in saved)
            previous.comparison_marks = sum((row.exam_question.positive_marks for row in saved
                                             if row.key_confirmed_at_grading is True), Decimal("0.00"))
        return render(request, "questions/practice_result.html", {
            "attempt": attempt, "exam": attempt.exam, "history": history,
            **result,
        })


class PracticeHistoryView(PrivateStudentMixin, View):
    def get(self, request):
        from django.core.paginator import Paginator
        attempts = (ExamAttempt.objects.filter(student=request.user, exam__creator=request.user,
                                                exam__is_private_practice=True)
                    .select_related("exam").prefetch_related("responses").order_by("-started_at", "-pk"))
        page = Paginator(attempts, 25).get_page(request.GET.get("page"))
        for attempt in page:
            saved = list(attempt.responses.all())
            attempt.has_verified_key = any(row.key_confirmed_at_grading is True for row in saved)
            attempt.has_legacy_key_details = any(row.key_confirmed_at_grading is None for row in saved)
        return render(request, "questions/practice_history.html", {"page_obj": page})


class PracticeReportView(PrivateStudentMixin, View):
    sensitive_during_attempt = True

    def get(self, request, attempt_uuid):
        from apps.core.utils import log_audit
        from .practice_analytics import compute_practice_result
        from .practice_report import generate_practice_report
        attempt = get_object_or_404(ExamAttempt.objects.select_related("exam"), attempt_uuid=attempt_uuid,
                                    student=request.user, exam__creator=request.user, exam__is_private_practice=True)
        attempt.auto_timeout_if_expired()
        if attempt.status == ExamAttempt.AttemptStatus.IN_PROGRESS:
            return redirect("exams:exam_session", attempt_uuid=attempt.attempt_uuid)
        try:
            require_feature(request.user, "pdf_reports")
        except FeatureUnavailable as exc:
            messages.error(request, str(exc))
            return redirect("payments:plans")
        pdf = generate_practice_report(attempt, compute_practice_result(attempt))
        log_audit(request.user, "PRACTICE_REPORT_DOWNLOADED", f"Private practice report {attempt.attempt_uuid} downloaded.", request=request)
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="practice-report-{attempt.attempt_uuid}.pdf"'
        response["X-Content-Type-Options"] = "nosniff"
        return response


class PracticeFigurePoolView(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    """Assign or discard unassigned cloud image candidates in private storage."""
    def get(self, request, pk):
        paper = owned_paper(request.user, pk)
        editable(paper)
        return render(request, "questions/figure_pool.html", {
            "paper": paper, "figures": paper.figures.filter(question__isnull=True),
            "questions": paper.extracted_questions.order_by("question_number", "id"),
            "student_practice": True,
        })

    def post(self, request, pk):
        owned_paper(request.user, pk)
        with transaction.atomic():
            paper = get_object_or_404(QuestionPaperUpload.objects.select_for_update(), pk=pk, uploaded_by=request.user, is_student_private=True)
            editable(paper)
            try:
                figure = get_object_or_404(ExtractedFigure, pk=request.POST.get("figure_id"), paper=paper, question__isnull=True)
                action = request.POST.get("action", "")
                if action == "delete":
                    old = figure.private_image.name
                    figure.delete()
                    if old:
                        transaction.on_commit(lambda: figure.private_image.storage.delete(old))
                elif action == "assign":
                    target = _target(request.POST.get("target"))
                    question = get_object_or_404(ExtractedQuestion, pk=request.POST.get("question_id"), paper=paper)
                    figure.question = question
                    figure.option_key = target
                    figure.save(update_fields=["question", "option_key"])
                    PracticeFigureManageView._ensure_option(question, target)
                    question.is_flagged = True
                    question.flag_reason = "Image assignment changed; check the question against its source."
                    question.answer_status = AnswerStatus.NEEDS_REVIEW
                    question.save(update_fields=["options", "is_flagged", "flag_reason", "answer_status"])
                else:
                    raise ValueError("Choose an image action.")
            except (ValueError, TypeError) as exc:
                messages.error(request, str(exc) or "Choose a valid question and image.")
                return redirect("questions:practice_figure_pool", pk=paper.pk)
        messages.success(request, "Image candidate updated.")
        return redirect("questions:practice_figure_pool", pk=paper.pk)


class PracticeDeleteView(PrivateStudentMixin, View):
    """Explicit owner deletion of a paper, its private test, attempts and files."""
    def post(self, request, pk):
        import logging
        from .private_storage import private_practice_storage

        with transaction.atomic():
            # Use the same fresh, locked track as a simultaneous profile change.
            user = lock_practice_user(request.user)
            owned_paper(user, pk)
            paper = get_object_or_404(QuestionPaperUpload.objects.select_for_update(), pk=pk, uploaded_by=user, is_student_private=True)
            if paper.status in (paper.ProcessingStatus.PENDING, paper.ProcessingStatus.PROCESSING):
                messages.warning(request, "Wait for extraction to finish before deleting this paper.")
                return redirect("questions:practice_list")
            bank = Question.objects.filter(source_paper=paper, creator=request.user, is_private_practice=True)
            names = {paper.private_pdf_file.name}
            names.update(paper.extracted_questions.exclude(private_image="").values_list("private_image", flat=True))
            names.update(paper.figures.exclude(private_image="").values_list("private_image", flat=True))
            names.update(bank.exclude(private_image="").values_list("private_image", flat=True))
            names.update(QuestionFigure.objects.filter(question__in=bank).exclude(private_image="").values_list("private_image", flat=True))
            names.discard("")
            names.discard(None)
            if paper.practice_exam_id:
                paper.practice_exam.delete()
            # A bank-made test may still reference these exact question/option
            # versions. Preserve those snapshots and any shared private images.
            retained = list(bank.filter(exam_instances__isnull=False).values_list("pk", flat=True).distinct())
            bank.filter(pk__in=retained).update(is_bank_archived=True)
            bank.exclude(pk__in=retained).delete()
            paper.delete()

            def remove_files():
                for name in names:
                    # Names may be shared by saved exam snapshots. Never remove a
                    # file while any surviving private record still references it.
                    in_use = (Question.objects.filter(private_image=name).exists()
                              or QuestionFigure.objects.filter(private_image=name).exists()
                              or ExtractedQuestion.objects.filter(private_image=name).exists()
                              or ExtractedFigure.objects.filter(private_image=name).exists()
                              or QuestionPaperUpload.objects.filter(private_pdf_file=name).exists())
                    if in_use:
                        continue
                    try:
                        private_practice_storage.delete(name)
                    except OSError:
                        logging.getLogger(__name__).warning("A deleted practice file could not be removed from storage.")

            transaction.on_commit(remove_files)
        log_audit(request.user, "PRIVATE_PAPER_DELETED", f"Private paper {pk} and its original test deleted.", request=request)
        messages.success(request, "Practice paper, test and attempts deleted.")
        return redirect("questions:practice_list")
