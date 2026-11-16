"""Private teacher PDF review -> immutable classroom test and join code.

Extraction and image handling are entirely local. A created test copies its
question images into private storage and snapshots the reviewed content and keys;
later paper edits never silently change an existing test or result.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.core.files.base import File, ContentFile
from django.db import OperationalError, connection, transaction
from django.db.models import F, Max
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.cache import patch_cache_control
from django.views import View

from apps.core.permissions import TeacherRequiredMixin
from apps.core.utils import log_audit
from apps.exams.models import Category, ClassroomSession, Exam, ExamQuestion
from .classroom_forms import ClassroomCreateForm, ClassroomPaperUploadForm, ClassroomQuestionForm
from .extractor import PDFQuestionExtractor
from .figure_utils import page_image, validate_box
from .figure_views import _png_upload, _target
from .models import (AnswerStatus, ExtractedFigure, ExtractedQuestion, Question,
                     QuestionFigure, QuestionOption, QuestionPaperUpload)
from .practice_forms import practice_question_error
from .practice_views import _open_private_pdf, _private_file_response
from .private_storage import private_practice_storage


def owned_paper(user, pk):
    return get_object_or_404(QuestionPaperUpload, pk=pk, uploaded_by=user,
                             is_student_private=False, is_classroom_private=True)


def owned_question(user, pk):
    return get_object_or_404(ExtractedQuestion.objects.select_related("paper").prefetch_related("figures"),
                             pk=pk, paper__uploaded_by=user, paper__is_student_private=False,
                             paper__is_classroom_private=True)


def _locked_paper(user, pk):
    # SQLite ignores SELECT FOR UPDATE. Acquire its write lock before reading
    # the paper, so concurrent create/edit requests serialize instead of racing.
    if connection.vendor == "sqlite":
        QuestionPaperUpload.objects.filter(pk=pk, uploaded_by=user, is_classroom_private=True).update(id=F("id"))
    return get_object_or_404(QuestionPaperUpload.objects.select_for_update(), pk=pk, uploaded_by=user,
                             is_student_private=False, is_classroom_private=True)


def _editable(paper):
    if paper.status in (paper.ProcessingStatus.PENDING, paper.ProcessingStatus.PROCESSING):
        raise Http404("Wait for extraction to finish before editing this paper.")


class PrivateClassroomMixin(TeacherRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        try:
            response = super().dispatch(request, *args, **kwargs)
        except OperationalError as exc:
            if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                raise
            response = HttpResponse("This paper is busy. Please retry; your review work was preserved.", status=503)
            response["Retry-After"] = "1"
        patch_cache_control(response, private=True, no_store=True)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response


def _classroom_category():
    """Allow a new teacher to create classroom tests without admin setup."""
    name = "Teacher Classroom (System)"
    if Category.objects.filter(name=name).exclude(slug="teacher-classroom-system").exists():
        name = f"Teacher Classroom (System {uuid.uuid4().hex[:8]})"
    category, _ = Category.objects.get_or_create(
        slug="teacher-classroom-system", defaults={"name": name, "is_active": False},
    )
    return category


class ClassroomPaperUploadView(PrivateClassroomMixin, View):
    def get(self, request):
        return render(request, "questions/paper_upload.html", {
            "form": ClassroomPaperUploadForm(user=request.user), "classroom_upload": True,
        })

    def post(self, request):
        form = ClassroomPaperUploadForm(request.POST, request.FILES, user=request.user)
        if not form.is_valid():
            return render(request, "questions/paper_upload.html", {"form": form, "classroom_upload": True})
        data = form.cleaned_data
        upload = data["pdf_file"]
        paper = QuestionPaperUpload.objects.create(
            title=data["title"], category=data["category"] or _classroom_category(), subject=data["subject"],
            target_exam=data["target_exam"], uploaded_by=request.user,
            is_classroom_private=True, private_pdf_file=upload, file_size_bytes=upload.size,
            total_pages=form.pdf_page_count, image_first=data["image_first"],
            extraction_mode=QuestionPaperUpload.ExtractionMode.LOCAL,
        )
        count = PDFQuestionExtractor(paper).extract()
        if count:
            messages.success(request, f"Extracted {count} question(s). Review them against the original before creating the test.")
        else:
            messages.warning(request, "No structured questions were found. The original is saved; inspect the log or add questions manually.")
        log_audit(request.user, "CLASSROOM_PDF_UPLOADED", f"Private classroom paper {paper.pk} uploaded.", request=request)
        return redirect("questions:classroom_paper_review", pk=paper.pk)


def _review_context(paper, form=None):
    questions = list(paper.extracted_questions.prefetch_related("figures").order_by("question_number", "id"))
    included = [q for q in questions if q.include_in_practice]
    for question in questions:
        question.practice_error = practice_question_error(question, allow_confirmed_crop_choices=True)
    unassigned = paper.figures.filter(question__isnull=True).count()
    return {
        "paper": paper, "questions": questions, "total_questions": len(included),
        "total_marks": sum((q.positive_marks for q in included), Decimal("0.00")),
        "keyed_count": sum(bool(q.correct_option and q.answer_status == AnswerStatus.MANUALLY_VERIFIED) for q in included),
        "flagged_count": sum(q.is_flagged for q in included), "unassigned_figure_count": unassigned,
        "extraction_warnings": [line.removeprefix("Warning: ") for line in paper.extraction_log.splitlines() if line.startswith("Warning: ")],
        "form": form if form is not None else ClassroomCreateForm(questions=included, unassigned_count=unassigned),
    }


def _copy_private_image(source, destination, created_files):
    if not source:
        return
    try:
        with source.open("rb") as handle:
            destination.save(f"classroom-{uuid.uuid4().hex}.png", File(handle), save=False)
        created_files.append(destination.name)
    except (OSError, ValueError) as exc:
        raise ValueError("A reviewed image is unavailable. Check or replace it before creating the test.") from exc


class ClassroomPaperReviewView(PrivateClassroomMixin, View):
    def get(self, request, pk):
        paper = owned_paper(request.user, pk)
        return render(request, "questions/classroom_review.html", _review_context(paper))

    def post(self, request, pk):
        owned_paper(request.user, pk)
        created_files = []
        try:
            with transaction.atomic():
                paper = _locked_paper(request.user, pk)
                _editable(paper)
                included = list(paper.extracted_questions.filter(include_in_practice=True)
                                .prefetch_related("figures").order_by("question_number", "id"))
                form = ClassroomCreateForm(request.POST, questions=included,
                                           unassigned_count=paper.figures.filter(question__isnull=True).count())
                if not form.is_valid():
                    return render(request, "questions/classroom_review.html", _review_context(paper, form))
                token = form.cleaned_data["creation_token"]
                existing = ClassroomSession.objects.filter(creation_token=token).select_related("exam").first()
                if existing:
                    if existing.teacher_id != request.user.pk or not existing.exam.exam_questions.filter(question__source_paper=paper).exists():
                        form.add_error(None, "This creation token is already in use. Reload this page and try again.")
                        return render(request, "questions/classroom_review.html", _review_context(paper, form))
                    return redirect("exams:classroom_session", pk=existing.pk)
                exam = Exam.objects.create(
                    is_classroom_only=True, slug=f"classroom-{uuid.uuid4().hex}", title=paper.title,
                    subject=paper.subject, target_exam=paper.target_exam, category=paper.category,
                    creator=request.user, status=Exam.ExamStatus.PUBLISHED, is_free=True,
                    duration_minutes=form.cleaned_data["duration_minutes"], total_marks=Decimal("0.00"),
                    passing_percentage=Decimal("0.00"), shuffle_questions=False, shuffle_options=False,
                    image_first_mode=paper.image_first, strict_exam_mode=form.cleaned_data["strict_exam_mode"],
                    show_result_immediately=form.cleaned_data["show_results"],
                    allow_review_answers=form.cleaned_data["show_results"],
                    description="Teacher-led classroom test. Only confirmed answer keys are scored.",
                )
                for position, staged in enumerate(included, 1):
                    supplied = form.answer_keys.get(staged.question_number)
                    correct = supplied if supplied is not None else (
                        staged.correct_option.upper() if staged.answer_status == AnswerStatus.MANUALLY_VERIFIED else "")
                    snapshot = Question(
                        is_classroom_snapshot=True, is_image_first=staged.is_image_first,
                        answer_status=AnswerStatus.MANUALLY_VERIFIED if correct else AnswerStatus.NEEDS_REVIEW,
                        category=paper.category, creator=request.user, source_paper=paper,
                        subject=paper.subject or "General", topic=paper.target_exam,
                        question_text=staged.question_text, tables=staged.tables,
                        source_regions=staged.source_regions, primary_image_region=staged.primary_image_region,
                        explanation=staged.explanation, positive_marks=staged.positive_marks,
                        negative_marks=staged.negative_marks,
                    )
                    _copy_private_image(staged.private_image, snapshot.private_image, created_files)
                    snapshot.save()
                    options = {}
                    for index, option in enumerate(staged.options):
                        key = option["key"].strip().upper()
                        options[key] = QuestionOption.objects.create(
                            question=snapshot, option_key=key, option_text=option.get("text", ""),
                            is_correct=bool(correct) and key == correct, order=index,
                        )
                    for figure in staged.figures.all():
                        if not figure.private_image:
                            raise ValueError(f"An image for question {staged.question_number} is missing.")
                        if figure.option_key and figure.option_key not in options:
                            raise ValueError(f"Question {staged.question_number} has an image assigned to a missing option.")
                        copy = QuestionFigure(question=snapshot, option=options.get(figure.option_key),
                                              order=figure.order, page_number=figure.page_number,
                                              bounding_box=figure.bounding_box, source=figure.source)
                        _copy_private_image(figure.private_image, copy.private_image, created_files)
                        copy.save()
                    ExamQuestion.objects.create(exam=exam, question=snapshot, order=position,
                                                section=paper.subject or "General", positive_marks=staged.positive_marks,
                                                negative_marks=staged.negative_marks)
                exam.recalculate_marks()
                session = ClassroomSession.create_for(
                    exam, request.user, creation_token=token, label=form.cleaned_data["label"],
                    allow_retakes=form.cleaned_data["allow_retakes"],
                    show_leaderboard_to_students=form.cleaned_data["show_leaderboard_to_students"],
                )
        except ValueError as exc:
            for name in created_files:
                private_practice_storage.delete(name)
            form.add_error(None, str(exc))
            return render(request, "questions/classroom_review.html", _review_context(owned_paper(request.user, pk), form))
        except Exception:
            for name in created_files:
                private_practice_storage.delete(name)
            raise
        log_audit(request.user, "CLASSROOM_TEST_CREATED", f"Classroom test {exam.pk} and session {session.pk} created.", request=request)
        messages.success(request, "Your classroom test is ready. Share the join code or link with students.")
        return redirect("exams:classroom_session", pk=session.pk)


class ClassroomQuestionEditView(PrivateClassroomMixin, View):
    def get(self, request, pk):
        question = owned_question(request.user, pk)
        _editable(question.paper)
        return render(request, "questions/classroom_question_edit.html", {"question": question, "form": ClassroomQuestionForm(question=question)})

    def post(self, request, pk):
        initial = owned_question(request.user, pk)
        with transaction.atomic():
            paper = _locked_paper(request.user, initial.paper_id)
            _editable(paper)
            question = owned_question(request.user, pk)
            form = ClassroomQuestionForm(request.POST, question=question)
            if not form.is_valid():
                return render(request, "questions/classroom_question_edit.html", {"question": question, "form": form})
            data = form.cleaned_data
            figure_keys = {f.option_key for f in question.figures.all() if f.private_image}
            question.question_number = data["question_number"]
            question.detected_type = data["detected_type"]
            question.question_text = data["question_text"]
            question.tables = data["tables_data"]
            crop_keys = set(data.get("visible_crop_choices") or [])
            question.options = [
                {"key": k, "text": data[f"option_{k}"].strip(), "visible_in_crop": k in crop_keys}
                for k in "ABCDE" if data[f"option_{k}"].strip() or k in figure_keys or k in crop_keys
            ]
            question.correct_option = data["correct_option"]
            question.answer_status = AnswerStatus.MANUALLY_VERIFIED if question.correct_option else AnswerStatus.NEEDS_REVIEW
            question.positive_marks = data["positive_marks"]
            question.negative_marks = data["negative_marks"]
            question.explanation = data["explanation"]
            question.is_flagged = not data["reviewed"]
            question.flag_reason = "" if data["reviewed"] else "Not yet checked against the source."
            question.status = ExtractedQuestion.Status.REVIEWED
            question.save()
        messages.success(request, "Question saved. Previously created tests remain unchanged.")
        return redirect("questions:classroom_paper_review", pk=paper.pk)


class ClassroomQuestionIncludeView(PrivateClassroomMixin, View):
    def post(self, request, pk):
        initial = owned_question(request.user, pk)
        with transaction.atomic():
            paper = _locked_paper(request.user, initial.paper_id)
            _editable(paper)
            question = owned_question(request.user, pk)
            question.include_in_practice = not question.include_in_practice
            question.save(update_fields=["include_in_practice"])
        return redirect("questions:classroom_paper_review", pk=paper.pk)


class ClassroomQuestionAddView(PrivateClassroomMixin, View):
    def post(self, request, pk):
        owned_paper(request.user, pk)
        with transaction.atomic():
            paper = _locked_paper(request.user, pk)
            _editable(paper)
            number = (paper.extracted_questions.aggregate(value=Max("question_number"))["value"] or 0) + 1
            question = ExtractedQuestion.objects.create(paper=paper, question_number=number, question_text="", options=[],
                is_image_first=paper.image_first, is_flagged=True, flag_reason="Manually added; review against the PDF.")
            paper.questions_found = paper.extracted_questions.count()
            paper.status = QuestionPaperUpload.ProcessingStatus.EXTRACTED
            paper.save(update_fields=["questions_found", "status"])
        return redirect("questions:classroom_question_edit", pk=question.pk)


class ClassroomPdfView(PrivateClassroomMixin, View):
    def get(self, request, pk):
        response = _private_file_response(owned_paper(request.user, pk).private_pdf_file, "application/pdf")
        response["Content-Disposition"] = 'inline; filename="classroom-source.pdf"'
        return response


class ClassroomSourcePageView(PrivateClassroomMixin, View):
    def get(self, request, pk, page_number):
        question = owned_question(request.user, pk)
        with _open_private_pdf(question.paper) as document:
            if not 1 <= page_number <= len(document):
                raise Http404()
            try:
                png = page_image(document[page_number - 1], max_side=1800, scale=1.8)
            except ValueError:
                raise Http404() from None
        response = HttpResponse(png, content_type="image/png")
        response["X-Content-Type-Options"] = "nosniff"
        return response


class ClassroomExtractedImageView(PrivateClassroomMixin, View):
    def get(self, request, pk):
        return _private_file_response(owned_question(request.user, pk).private_image, "image/png")


class ClassroomFigureFileView(PrivateClassroomMixin, View):
    def get(self, request, pk):
        figure = get_object_or_404(ExtractedFigure, pk=pk, paper__uploaded_by=request.user,
                                   paper__is_classroom_private=True, paper__is_student_private=False)
        return _private_file_response(figure.private_image, "image/png")


def _ensure_option(question, key):
    if key and not any(isinstance(o, dict) and o.get("key") == key for o in question.options):
        question.options = list(question.options) + [{"key": key, "text": ""}]
        question.options.sort(key=lambda opt: opt.get("key", ""))


class ClassroomFigureManageView(PrivateClassroomMixin, View):
    def get(self, request, pk):
        question = owned_question(request.user, pk)
        _editable(question.paper)
        return render(request, "questions/figure_manage.html", {
            "question": question, "figures": question.figures.all(), "classroom_teacher": True,
            "page_range": range(1, question.paper.total_pages + 1),
        })

    def post(self, request, pk):
        initial = owned_question(request.user, pk)
        with transaction.atomic():
            paper = _locked_paper(request.user, initial.paper_id)
            _editable(paper)
            question = owned_question(request.user, pk)
            action = request.POST.get("action", "")
            try:
                if action == "remove_primary":
                    old = question.private_image.name
                    question.private_image = ""
                    question.primary_image_region = {}
                    question.save(update_fields=["private_image", "primary_image_region"])
                    if old:
                        transaction.on_commit(lambda: private_practice_storage.delete(old))
                elif action in ("retarget", "unassign", "delete"):
                    figure = get_object_or_404(ExtractedFigure, pk=request.POST.get("figure_id"), question=question, paper=paper)
                    if action == "retarget":
                        figure.option_key = _target(request.POST.get("target"))
                        # Moving a crop to an option makes it an ordinary image.
                        if figure.option_key and figure.source == "question_crop":
                            figure.source = "manual_crop"
                        figure.save(update_fields=["option_key", "source"])
                        _ensure_option(question, figure.option_key)
                    elif action == "unassign":
                        figure.question = None
                        figure.option_key = ""
                        figure.save(update_fields=["question", "option_key"])
                    else:
                        old = figure.private_image.name
                        figure.delete()
                        if old:
                            transaction.on_commit(lambda: private_practice_storage.delete(old))
                elif action in ("crop", "upload"):
                    raw_target = request.POST.get("target", "")
                    original_crop = raw_target == "__crop__"
                    target = "" if original_crop else _target(raw_target)
                    replace_id = request.POST.get("replace_id", "")
                    figure = get_object_or_404(ExtractedFigure, pk=replace_id, question=question, paper=paper) if replace_id else ExtractedFigure(paper=paper, question=question)
                    old = figure.private_image.name
                    keep_crop = original_crop or (figure.source == "question_crop" and not target)
                    if action == "crop":
                        try:
                            page_number = int(request.POST.get("page_number", ""))
                            box = validate_box([request.POST.get(k, "") for k in ("x0", "y0", "x1", "y1")])
                        except (ValueError, TypeError):
                            raise ValueError("Choose a valid page and a non-empty rectangle inside it.") from None
                        with _open_private_pdf(paper) as document:
                            if not 1 <= page_number <= len(document):
                                raise ValueError("That source page does not exist.")
                            png = page_image(document[page_number - 1], box, max_side=3200, scale=3)
                        figure.page_number, figure.bounding_box, figure.source = page_number, box, "question_crop" if keep_crop else "manual_crop"
                    else:
                        png = _png_upload(request.FILES.get("image"))
                        figure.page_number, figure.bounding_box, figure.source = question.page_number, [], "question_crop" if keep_crop else "upload"
                    figure.option_key = target
                    figure.private_image.save(f"classroom_{paper.pk}_figure.png", ContentFile(png), save=False)
                    figure.save()
                    if old:
                        transaction.on_commit(lambda: private_practice_storage.delete(old))
                    _ensure_option(question, target)
                else:
                    raise ValueError("Unknown image operation.")
                question.is_flagged = True
                question.flag_reason = "Image assignment changed; check the question against the source."
                question.save(update_fields=["options", "is_flagged", "flag_reason"])
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("questions:classroom_figures", pk=question.pk)
        messages.success(request, "Image changes saved. Review the question before creating a test.")
        return redirect("questions:classroom_figures", pk=question.pk)


class ClassroomFigurePoolView(PrivateClassroomMixin, View):
    def get(self, request, pk):
        paper = owned_paper(request.user, pk)
        _editable(paper)
        return render(request, "questions/figure_pool.html", {
            "paper": paper, "figures": paper.figures.filter(question__isnull=True),
            "questions": paper.extracted_questions.order_by("question_number", "id"), "classroom_teacher": True,
        })

    def post(self, request, pk):
        owned_paper(request.user, pk)
        with transaction.atomic():
            paper = _locked_paper(request.user, pk)
            _editable(paper)
            figure = get_object_or_404(ExtractedFigure, pk=request.POST.get("figure_id"), paper=paper, question__isnull=True)
            try:
                if request.POST.get("action") == "delete":
                    old = figure.private_image.name
                    figure.delete()
                    if old:
                        transaction.on_commit(lambda: private_practice_storage.delete(old))
                elif request.POST.get("action") == "assign":
                    target = _target(request.POST.get("target"))
                    question = get_object_or_404(ExtractedQuestion, pk=request.POST.get("question_id"), paper=paper)
                    figure.question, figure.option_key = question, target
                    figure.save(update_fields=["question", "option_key"])
                    _ensure_option(question, target)
                    question.is_flagged = True
                    question.flag_reason = "Image assignment changed; check against the source."
                    question.save(update_fields=["options", "is_flagged", "flag_reason"])
                else:
                    raise ValueError("Choose an image action.")
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("questions:classroom_figure_pool", pk=pk)
        messages.success(request, "Image candidate updated.")
        return redirect("questions:classroom_figure_pool", pk=pk)
