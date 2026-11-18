"""Teacher-only figure review, source-page cropping, and manual staging."""
import io

import pymupdf as fitz
from PIL import Image, ImageOps, UnidentifiedImageError
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from apps.core.permissions import TeacherRequiredMixin
from .figure_utils import page_image, validate_box
from .models import AnswerStatus, ExtractedFigure, ExtractedQuestion, QuestionPaperUpload


def _authorize(user, paper):
    if paper.uses_private_storage:
        raise Http404()
    if not user.is_admin_role and paper.uploaded_by_id != user.pk:
        if not user.assigned_categories.filter(pk=paper.category_id).exists():
            raise PermissionDenied("You cannot manage this paper.")


def _safe_id(value):
    try:
        number = int(value)
        return number if number > 0 else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def _editable(question):
    if question.status == ExtractedQuestion.Status.APPROVED:
        raise PermissionDenied("This question is already in the bank; staging figures can no longer be changed.")


def _target(value):
    value = (value or "").strip().upper()
    if value not in ("", "A", "B", "C", "D", "E"):
        raise ValueError("Choose the question or an option A–E.")
    return value


def _ensure_option(question, key):
    if not key:
        return
    options = question.options if isinstance(question.options, list) else []
    if not any(isinstance(option, dict) and option.get("key") == key for option in options):
        options.append({"key": key, "text": ""})
        options.sort(key=lambda option: option.get("key", ""))
        question.options = options


def _flag(question):
    question.is_flagged = True
    question.flag_reason = "Image assignment changed; review question and options against the source."
    # An image can contain the statement or choices on which the key depends.
    # Keep the entered answer as a suggestion, but require a fresh check.
    question.answer_status = AnswerStatus.NEEDS_REVIEW
    if question.status == ExtractedQuestion.Status.EXTRACTED:
        question.status = ExtractedQuestion.Status.REVIEWED
    question.save(update_fields=["options", "is_flagged", "flag_reason", "status", "answer_status"])


def _png_upload(upload):
    if not upload or upload.size > 8 * 1024 * 1024:
        raise ValueError("Choose an image up to 8 MB.")
    try:
        with Image.open(upload) as image:
            if image.width * image.height > 16_000_000:
                raise ValueError("The image is too large; maximum is 16 megapixels.")
            image = ImageOps.exif_transpose(image)
            image.load()
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            out = io.BytesIO()
            image.save(out, format="PNG")
            return out.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("The uploaded file is not a valid supported image.") from None


def _open_pdf(paper):
    try:
        with paper.pdf_file.open("rb") as source:
            return fitz.open(stream=source.read(), filetype="pdf")
    except (OSError, ValueError, RuntimeError):
        raise Http404("The source PDF could not be opened.") from None


class SourcePageImageView(TeacherRequiredMixin, View):
    def get(self, request, pk, page_number):
        question = get_object_or_404(ExtractedQuestion.objects.select_related("paper"), pk=pk)
        _authorize(request.user, question.paper)
        with _open_pdf(question.paper) as doc:
            if not 1 <= page_number <= len(doc):
                raise Http404("Page does not exist.")
            try:
                png = page_image(doc[page_number - 1], max_side=1800, scale=1.8)
            except ValueError:
                raise Http404("The source page cannot be rendered.") from None
        response = HttpResponse(png, content_type="image/png")
        response["Cache-Control"] = "private, no-store"
        return response


class SourceRegionImageView(TeacherRequiredMixin, View):
    def get(self, request, pk, index):
        question = get_object_or_404(ExtractedQuestion.objects.select_related("paper"), pk=pk)
        _authorize(request.user, question.paper)
        try:
            region = question.source_regions[index]
            page_number, box = int(region["page"]), validate_box(region["box"])
        except (IndexError, KeyError, TypeError, ValueError):
            raise Http404() from None
        with _open_pdf(question.paper) as document:
            if not 1 <= page_number <= len(document):
                raise Http404()
            try:
                png = page_image(document[page_number - 1], box, max_side=1800, scale=2)
            except ValueError:
                raise Http404() from None
        response = HttpResponse(png, content_type="image/png")
        response["Cache-Control"] = "private, no-store"
        return response


class FigureImageView(TeacherRequiredMixin, View):
    def get(self, request, pk):
        figure = get_object_or_404(ExtractedFigure.objects.select_related("paper"), pk=pk)
        _authorize(request.user, figure.paper)
        try:
            handle = figure.image.open("rb")
        except (FileNotFoundError, OSError):
            raise Http404("Figure not found.") from None
        response = FileResponse(handle, content_type="image/png")
        response["Cache-Control"] = "private, no-store"
        return response


class QuestionFigureManageView(TeacherRequiredMixin, View):
    def get_question(self, request, pk):
        question = get_object_or_404(ExtractedQuestion.objects.select_related("paper"), pk=pk)
        _authorize(request.user, question.paper)
        return question

    def get(self, request, pk):
        question = self.get_question(request, pk)
        return render(request, "questions/figure_manage.html", {
            "question": question, "figures": question.figures.all(),
            "page_range": range(1, question.paper.total_pages + 1),
        })

    def post(self, request, pk):
        question = self.get_question(request, pk)
        _editable(question)
        action = request.POST.get("action", "")
        try:
            if action in ("retarget", "unassign", "delete"):
                figure = get_object_or_404(ExtractedFigure, pk=_safe_id(request.POST.get("figure_id")), question=question)
                with transaction.atomic():
                    if action == "retarget":
                        figure.option_key = _target(request.POST.get("target"))
                        if figure.option_key and figure.source == "question_crop":
                            figure.source = "manual_crop"
                        _ensure_option(question, figure.option_key)
                        figure.save(update_fields=["option_key", "source"])
                    elif action == "unassign":
                        figure.question = None
                        figure.option_key = ""
                        figure.save(update_fields=["question", "option_key"])
                    else:
                        # Do not remove a storage file: bank images can share
                        # the path and Django storage isn't transactional.
                        figure.delete()
                    _flag(question)
            elif action in ("crop", "upload"):
                raw_target = request.POST.get("target", "")
                original_crop = raw_target == "__crop__"
                target = "" if original_crop else _target(raw_target)
                replace_id = request.POST.get("replace_id", "")
                figure = get_object_or_404(ExtractedFigure, pk=_safe_id(replace_id), question=question) if replace_id else ExtractedFigure(paper=question.paper, question=question)
                keep_crop = original_crop or (figure.source == "question_crop" and not target)
                if action == "crop":
                    try:
                        page_number = int(request.POST.get("page_number", ""))
                        box = validate_box([request.POST.get(k, "") for k in ("x0", "y0", "x1", "y1")])
                    except (ValueError, TypeError):
                        raise ValueError("Choose a valid page and draw a non-empty rectangle inside it.") from None
                    with _open_pdf(question.paper) as doc:
                        if not 1 <= page_number <= len(doc):
                            raise ValueError("That source page does not exist.")
                        png = page_image(doc[page_number - 1], box, max_side=3200, scale=3) if keep_crop else page_image(doc[page_number - 1], box)
                    figure.page_number, figure.bounding_box, figure.source = page_number, box, "question_crop" if keep_crop else "manual_crop"
                else:
                    png = _png_upload(request.FILES.get("image"))
                    figure.page_number, figure.bounding_box, figure.source = question.page_number, [], "question_crop" if keep_crop else "upload"
                figure.option_key = target
                figure.image.save(f"paper_{question.paper_id}_manual.png", ContentFile(png), save=False)
                with transaction.atomic():
                    figure.save()
                    _ensure_option(question, target)
                    _flag(question)
            else:
                raise ValueError("Unknown image operation.")
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Image changes saved. Check the question and clear its review flag when ready.")
        return redirect("questions:figure_manage", pk=pk)


class PaperFigurePoolView(TeacherRequiredMixin, View):
    def get_paper(self, request, pk):
        paper = get_object_or_404(QuestionPaperUpload, pk=pk)
        _authorize(request.user, paper)
        return paper

    def get(self, request, pk):
        paper = self.get_paper(request, pk)
        return render(request, "questions/figure_pool.html", {
            "paper": paper, "figures": paper.figures.filter(question__isnull=True),
            "questions": paper.extracted_questions.exclude(status=ExtractedQuestion.Status.APPROVED),
        })

    def post(self, request, pk):
        paper = self.get_paper(request, pk)
        figure = get_object_or_404(ExtractedFigure, pk=_safe_id(request.POST.get("figure_id")), paper=paper, question__isnull=True)
        if request.POST.get("action") == "delete":
            figure.delete()
            messages.success(request, "Candidate removed from the pool.")
        else:
            question_id = _safe_id(request.POST.get("question_id"))
            if not question_id:
                messages.error(request, "Choose a question before attaching an image.")
                return redirect("questions:figure_pool", pk=pk)
            question = get_object_or_404(ExtractedQuestion, pk=question_id, paper=paper)
            _editable(question)
            try:
                target = _target(request.POST.get("target"))
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                with transaction.atomic():
                    figure.question, figure.option_key = question, target
                    figure.save(update_fields=["question", "option_key"])
                    _ensure_option(question, target)
                    _flag(question)
                messages.success(request, f"Candidate attached to question {question.question_number} for review.")
        return redirect("questions:figure_pool", pk=pk)


class ManualQuestionCreateView(TeacherRequiredMixin, View):
    def get_paper(self, request, pk):
        paper = get_object_or_404(QuestionPaperUpload, pk=pk)
        _authorize(request.user, paper)
        return paper

    def get(self, request, pk):
        return render(request, "questions/manual_question.html", {"paper": self.get_paper(request, pk)})

    def post(self, request, pk):
        paper = self.get_paper(request, pk)
        try:
            number = int(request.POST.get("question_number", ""))
            page = int(request.POST.get("page_number", ""))
            text = request.POST.get("question_text", "").strip()
            if not 1 <= number <= 100000 or not 1 <= page <= paper.total_pages or not text or len(text) > 30000:
                raise ValueError
        except ValueError:
            messages.error(request, "Enter a valid question number, source page and question text.")
            return render(request, "questions/manual_question.html", {"paper": paper, "values": request.POST}, status=400)
        with transaction.atomic():
            question = ExtractedQuestion.objects.create(
                paper=paper, question_number=number, page_number=page, source_pages=[page], question_text=text,
                options=[], is_flagged=True, flag_reason="Manually added; complete options and review against the source.",
            )
            paper.questions_found = paper.extracted_questions.count()
            paper.save(update_fields=["questions_found"])
        return redirect("questions:extracted_edit", pk=question.pk)


def _authorize_bank(user, question):
    if question.is_private_practice or question.is_classroom_snapshot:
        raise Http404()
    if not user.is_admin_role and question.creator_id != user.pk:
        if not user.assigned_categories.filter(pk=question.category_id).exists():
            raise PermissionDenied("You cannot manage images for this bank question.")


def _bank_source(user, question):
    if not question.source_paper_id:
        return None
    staged = question.source_extracted_item.filter(paper_id=question.source_paper_id).select_related("paper").first()
    if staged:
        try:
            _authorize(user, staged.paper)
        except PermissionDenied:
            return None
    return staged


def _bank_target(question, key):
    key = _target(key)
    if not key:
        return None
    option = question.options.filter(option_key=key).first()
    if not option:
        raise ValueError("Add this option in the question editor before attaching an image to it.")
    return option


def _can_remove_bank_figure(figure):
    option = figure.option
    return not option or bool(option.option_text.strip() or option.image or option.figures.exclude(pk=figure.pk).exists())


class BankFigureManageView(TeacherRequiredMixin, View):
    """Manage bank figures separately from read-only approved staging items."""

    def get_question(self, request, pk):
        from .models import Question
        question = get_object_or_404(Question.objects.select_related("source_paper"), pk=pk)
        _authorize_bank(request.user, question)
        return question

    def get(self, request, pk):
        question = self.get_question(request, pk)
        staged = _bank_source(request.user, question)
        return render(request, "questions/bank_figures.html", {
            "question": question, "figures": question.figures.select_related("option"),
            "options": question.options.all(), "staged": staged,
            "page_range": range(1, staged.paper.total_pages + 1) if staged else [],
        })

    def post(self, request, pk):
        from .models import QuestionFigure
        question = self.get_question(request, pk)
        action = request.POST.get("action", "")
        try:
            if action in ("retarget", "delete"):
                figure = get_object_or_404(QuestionFigure.objects.select_related("option"), pk=_safe_id(request.POST.get("figure_id")), question=question)
                target = _bank_target(question, request.POST.get("target")) if action == "retarget" else None
                if figure.option_id and (not target or target.pk != figure.option_id) and not _can_remove_bank_figure(figure):
                    raise ValueError("This is the only content in that option. Add option text or another image first.")
                if action == "retarget":
                    figure.option = target
                    if target and figure.source == "question_crop":
                        figure.source = "manual_crop"
                    figure.save(update_fields=["option", "source"])
                else:
                    figure.delete()
            elif action in ("crop", "upload"):
                raw_target = request.POST.get("target", "")
                original_crop = raw_target == "__crop__"
                target = None if original_crop else _bank_target(question, raw_target)
                replace_id = request.POST.get("replace_id", "")
                figure = get_object_or_404(QuestionFigure.objects.select_related("option"), pk=_safe_id(replace_id), question=question) if replace_id else QuestionFigure(question=question)
                keep_crop = original_crop or (figure.source == "question_crop" and not target)
                if (replace_id and figure.option_id and (not target or target.pk != figure.option_id)
                        and not _can_remove_bank_figure(figure)):
                    raise ValueError("This is the only content in the old option. Add text or another image first.")
                if action == "upload":
                    png = _png_upload(request.FILES.get("image"))
                    if keep_crop and request.POST.get("page_number"):
                        try:
                            page_number = int(request.POST["page_number"])
                            if page_number < 1:
                                raise ValueError
                        except (TypeError, ValueError):
                            raise ValueError("Choose a valid page number for the original image.") from None
                    else:
                        page_number = figure.page_number or (question.primary_image_region or {}).get("page", 1)
                    figure.page_number, figure.bounding_box, figure.source = page_number, [], "question_crop" if keep_crop else "upload"
                else:
                    staged = _bank_source(request.user, question)
                    if not staged:
                        raise ValueError("An accessible source PDF is required for cropping.")
                    try:
                        page_number = int(request.POST.get("page_number", ""))
                        box = validate_box([request.POST.get(k, "") for k in ("x0", "y0", "x1", "y1")])
                    except (ValueError, TypeError):
                        raise ValueError("Choose a valid source page and crop rectangle.") from None
                    with _open_pdf(staged.paper) as doc:
                        if not 1 <= page_number <= len(doc):
                            raise ValueError("That source page does not exist.")
                        png = page_image(doc[page_number - 1], box, max_side=3200, scale=3) if keep_crop else page_image(doc[page_number - 1], box)
                    figure.page_number, figure.bounding_box, figure.source = page_number, box, "question_crop" if keep_crop else "manual_crop"
                figure.option = target
                figure.image.save(f"question_{question.pk}_manual.png", ContentFile(png), save=True)
            elif action == "clear_legacy_option":
                option = _bank_target(question, request.POST.get("target"))
                if not option or not option.image:
                    raise ValueError("This option has no legacy image.")
                if not option.option_text.strip() and not option.figures.exists():
                    raise ValueError("This is the only content in that option. Add text or another image first.")
                option.image = None
                option.save(update_fields=["image"])
            else:
                raise ValueError("Unknown image operation.")
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            if question.is_pdf_derived:
                question.answer_status = AnswerStatus.NEEDS_REVIEW
                question.key_verified = False
                question.save(update_fields=["answer_status", "key_verified"])
                messages.warning(request, "Image changes saved. Recheck the answer before this PDF question is graded in a new attempt.")
            else:
                messages.success(request, "Bank image changes saved. They may affect exams that use this question.")
        return redirect("questions:bank_figure_manage", pk=pk)
