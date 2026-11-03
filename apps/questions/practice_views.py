"""Owner-only PDF-to-practice workflow; never uses publicly served media."""
from decimal import Decimal
import uuid

import pymupdf as fitz
from django.conf import settings
from django.contrib import messages
from django.core.files.base import ContentFile
from django.db import OperationalError, connection, transaction
from django.db.models import Max
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.cache import patch_cache_control
from django.views import View

from apps.core.permissions import StudentRequiredMixin
from apps.exams.models import Category, Exam, ExamAttempt, ExamQuestion
from apps.payments.services import QuotaExceeded, FeatureUnavailable, consume, lock_practice_user, require_feature
from apps.core.utils import log_audit
from .figure_utils import page_image, validate_box
from .figure_views import _png_upload, _target
from .models import AnswerStatus, ExtractedFigure, ExtractedQuestion, Question, QuestionFigure, QuestionOption, QuestionPaperUpload
from .practice_forms import PracticeQuestionForm, PracticeSettingsForm, PracticeUploadForm, practice_question_error
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
                    lock_practice_user(request.user)
                    active = active_private_attempt(request.user)
                    if active:
                        messages.info(request, "Finish your active practice test before opening answers, the source paper or question bank.")
                        response = redirect("exams:exam_session", attempt_uuid=active.attempt_uuid)
                    else:
                        response = super().dispatch(request, *args, **kwargs)
            else:
                response = super().dispatch(request, *args, **kwargs)
        except OperationalError as exc:
            if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                raise
            response = HttpResponse("Practice is busy. Please retry; your active timer continues.", status=503)
            response["Retry-After"] = "1"
        patch_cache_control(response, private=True, no_store=True)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response


def owned_paper(user, pk):
    return get_object_or_404(QuestionPaperUpload, pk=pk, uploaded_by=user, is_student_private=True)


def owned_question(user, pk):
    return get_object_or_404(
        ExtractedQuestion.objects.select_related("paper").prefetch_related("figures"),
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
        papers = QuestionPaperUpload.objects.filter(
            uploaded_by=request.user, is_student_private=True
        ).select_related("practice_exam").prefetch_related("practice_exam__attempts")
        # Do not share institutional result metrics with ungraded practice.
        bank_tests = (Exam.objects.filter(creator=request.user, is_private_practice=True, private_source_paper__isnull=True)
                      .prefetch_related("attempts").order_by("-created_at"))
        return render(request, "questions/practice_list.html", {"papers": papers, "bank_tests": bank_tests})


class PracticeUploadView(PrivateStudentMixin, View):
    def get(self, request):
        return render(request, "questions/practice_upload.html", {"form": PracticeUploadForm(), "max_pages": settings.PRACTICE_MAX_PDF_PAGES})

    def post(self, request):
        form = PracticeUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, "questions/practice_upload.html", {"form": form, "max_pages": settings.PRACTICE_MAX_PDF_PAGES})
        upload = form.cleaned_data["pdf_file"]
        try:
            with transaction.atomic():
                cloud_mode = form.cleaned_data["extraction_mode"] in (
                    QuestionPaperUpload.ExtractionMode.CLOUD, QuestionPaperUpload.ExtractionMode.GEMINI,
                )
                if cloud_mode:
                    require_feature(request.user, "cloud_extraction")
                consume(request.user, "pdf_uploads")
                paper = QuestionPaperUpload.objects.create(
                    is_student_private=True, title=form.cleaned_data["title"],
                    subject=form.cleaned_data["subject"], category=_system_category(),
                    uploaded_by=request.user, private_pdf_file=upload, file_size_bytes=upload.size,
                    total_pages=form.pdf_page_count, extraction_mode=form.cleaned_data["extraction_mode"],
                    cloud_consent_at=timezone.now() if cloud_mode else None,
                )
        except (QuotaExceeded, FeatureUnavailable) as exc:
            messages.error(request, str(exc))
            return redirect("payments:plans")
        except OperationalError as exc:
            if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                raise
            messages.warning(request, "Practice is busy. Please retry your upload.")
            return redirect("questions:practice_upload")
        log_audit(request.user, "PRIVATE_PDF_UPLOADED", f"Private paper {paper.pk} uploaded.", request=request)
        task = extract_cloud_paper if paper.extraction_mode in (
            QuestionPaperUpload.ExtractionMode.CLOUD, QuestionPaperUpload.ExtractionMode.GEMINI,
        ) else extract_private_practice_paper
        try:
            if settings.CELERY_TASK_ALWAYS_EAGER:
                task(paper.pk)
            else:
                task.delay(paper.pk)
        except Exception:
            # Preserve the source for manual entry; do not expose broker details.
            paper.status = QuestionPaperUpload.ProcessingStatus.FAILED
            paper.extraction_log = "Extraction could not be queued. You can add questions manually."
            paper.save(update_fields=["status", "extraction_log"])
            messages.warning(request, "Extraction could not be queued. Your PDF is saved and you can add questions manually.")
        return redirect("questions:practice_settings", pk=paper.pk)


class PracticeSettingsView(PrivateStudentMixin, View):
    sensitive_during_attempt = True
    def _context(self, paper, form=None):
        questions = list(paper.extracted_questions.prefetch_related("figures", "ai_suggestions").order_by("question_number", "id"))
        included = [q for q in questions if q.include_in_practice]
        for question in questions:
            question.practice_error = practice_question_error(question)
            question.latest_ai = next(iter(question.ai_suggestions.all()), None)
        return {
            "paper": paper, "questions": questions, "included": included,
            "total_questions": len(included),
            "total_marks": sum((q.positive_marks for q in included), Decimal("0.00")),
            "keyed_count": sum(bool(q.correct_option) for q in included),
            "flagged_count": sum(q.is_flagged for q in included),
            "unassigned_figure_count": paper.figures.filter(question__isnull=True).count(),
            "extraction_warnings": [line.removeprefix("Warning: ") for line in paper.extraction_log.splitlines() if line.startswith("Warning: ")],
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
            editable(paper)
            included = list(paper.extracted_questions.filter(include_in_practice=True).prefetch_related("figures").order_by("question_number", "id"))
            form = PracticeSettingsForm(request.POST, questions=included, unassigned_count=paper.figures.filter(question__isnull=True).count())
            if not form.is_valid():
                return render(request, "questions/practice_settings.html", self._context(paper, form))
            try:
                consume(request.user, "test_creations")
            except QuotaExceeded as exc:
                messages.error(request, str(exc))
                return redirect("payments:plans")
            exam = Exam.objects.create(
                is_private_practice=True, slug=f"practice-{uuid.uuid4().hex}", title=paper.title,
                subject=paper.subject, category=paper.category, creator=request.user,
                status=Exam.ExamStatus.PUBLISHED, is_free=True, max_attempts=0,
                duration_minutes=form.cleaned_data["duration_minutes"],
                total_marks=Decimal("0.00"), passing_percentage=form.cleaned_data["passing_percentage"] or Decimal("0.00"),
                shuffle_questions=False, shuffle_options=False,
                show_result_immediately=True, allow_review_answers=True,
                description="Private practice from your PDF. Only questions with a supplied answer key are graded.",
            )
            for position, staged in enumerate(included, 1):
                correct = form.answer_keys.get(staged.question_number, staged.correct_option).upper()
                bank = Question.objects.create(
                    is_private_practice=True, key_verified=bool(correct),
                    answer_status=AnswerStatus.STUDENT_CONFIRMED if correct else AnswerStatus.NEEDS_REVIEW, category=paper.category, creator=request.user,
                    source_paper=paper, subject=paper.subject or "General",
                    question_text=staged.question_text, tables=staged.tables, source_regions=staged.source_regions, primary_image_region=staged.primary_image_region, explanation=staged.explanation,
                    private_image=staged.private_image.name,
                    positive_marks=staged.positive_marks, negative_marks=staged.negative_marks,
                )
                options = {}
                for index, option in enumerate(staged.options):
                    key = option["key"].upper().strip()
                    options[key] = QuestionOption.objects.create(
                        question=bank, option_key=key, option_text=option.get("text", ""),
                        is_correct=bool(correct) and key == correct, order=index,
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
                staged.correct_option = correct
                staged.answer_status = AnswerStatus.STUDENT_CONFIRMED if correct else AnswerStatus.NEEDS_REVIEW
                staged.approved_question = bank
                staged.status = ExtractedQuestion.Status.APPROVED
                staged.save(update_fields=["correct_option", "answer_status", "approved_question", "status"])
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
            figure_keys = {f.option_key for f in question.figures.all() if f.private_image}
            question.question_text = data["question_text"]
            question.tables = data["tables_data"]
            question.options = [{"key": k, "text": data[f"option_{k}"].strip()} for k in "ABCDE" if data[f"option_{k}"].strip() or k in figure_keys]
            question.correct_option = data["correct_option"]
            question.answer_status = AnswerStatus.STUDENT_CONFIRMED if question.correct_option else AnswerStatus.NEEDS_REVIEW
            question.positive_marks = data["positive_marks"]
            question.negative_marks = data["negative_marks"]
            question.explanation = data["explanation"]
            question.is_flagged = not data["reviewed"]
            question.flag_reason = "" if data["reviewed"] else "Not yet checked against source."
            question.status = ExtractedQuestion.Status.REVIEWED
            question.save(update_fields=["question_text", "tables", "options", "correct_option", "answer_status", "positive_marks", "negative_marks", "explanation", "is_flagged", "flag_reason", "status"])
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
        return _private_file_response(figure.private_image, "image/png")


class PracticeBankImageView(PrivateStudentMixin, View):
    def get(self, request, pk):
        question = get_object_or_404(Question, pk=pk, creator=request.user, is_private_practice=True)
        return _private_file_response(question.private_image, "image/png")


class PracticeBankFigureView(PrivateStudentMixin, View):
    def get(self, request, pk):
        figure = get_object_or_404(QuestionFigure, pk=pk, question__creator=request.user, question__is_private_practice=True)
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
                        figure.save(update_fields=["option_key"])
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
                    target = _target(request.POST.get("target"))
                    replace_id = request.POST.get("replace_id", "")
                    figure = get_object_or_404(ExtractedFigure, pk=replace_id, question=question, paper=paper) if replace_id else ExtractedFigure(paper=paper, question=question)
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
                            png = page_image(document[page_number - 1], box)
                        figure.page_number, figure.bounding_box, figure.source = page_number, box, "manual_crop"
                    else:
                        png = _png_upload(request.FILES.get("image"))
                        figure.page_number, figure.bounding_box, figure.source = question.page_number, [], "upload"
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
                question.save(update_fields=["options", "is_flagged", "flag_reason"])
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
        log_audit(request.user, "PRACTICE_RESULT_VIEWED", f"Private attempt {attempt.attempt_uuid} viewed.", request=request)
        history = (ExamAttempt.objects.filter(exam=attempt.exam, student=request.user)
                   .exclude(status=ExamAttempt.AttemptStatus.IN_PROGRESS).order_by("-started_at", "-pk"))
        return render(request, "questions/practice_result.html", {
            "attempt": attempt, "exam": attempt.exam, "history": history,
            **result,
        })


class PracticeHistoryView(PrivateStudentMixin, View):
    def get(self, request):
        from django.core.paginator import Paginator
        from django.db.models import Count, Q
        attempts = (ExamAttempt.objects.filter(student=request.user, exam__creator=request.user,
                                                exam__is_private_practice=True)
                    .select_related("exam").order_by("-started_at", "-pk"))
        page = Paginator(attempts, 25).get_page(request.GET.get("page"))
        # Avoid presenting an entirely unkeyed paper as a zero score. Tests
        # share immutable question versions across their own attempts.
        exam_ids = {attempt.exam_id for attempt in page if attempt.status != ExamAttempt.AttemptStatus.IN_PROGRESS}
        keyed_exams = set(ExamQuestion.objects.filter(exam_id__in=exam_ids, question__key_verified=True)
            .annotate(correct_keys=Count("question__options", filter=Q(question__options__is_correct=True)))
            .filter(correct_keys=1).values_list("exam_id", flat=True))
        for attempt in page:
            attempt.has_verified_key = attempt.exam_id in keyed_exams
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
                    question.save(update_fields=["options", "is_flagged", "flag_reason"])
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

        owned_paper(request.user, pk)
        with transaction.atomic():
            paper = get_object_or_404(QuestionPaperUpload.objects.select_for_update(), pk=pk, uploaded_by=request.user, is_student_private=True)
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
