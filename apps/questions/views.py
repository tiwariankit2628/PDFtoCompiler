import json
import re
from django.contrib import messages
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.utils import timezone
from django.utils.cache import patch_cache_control
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View

from apps.core.permissions import TeacherRequiredMixin
from apps.core.utils import log_audit
from .extractor import PDFQuestionExtractor
from .answer_key_validation import edited_answer_status, staged_question_signature
from .forms import (
    AnswerKeyImportForm,
    ExtractedQuestionEditForm,
    QuestionPaperUploadForm,
)
from .models import (AnswerStatus, ExtractedFigure, ExtractedQuestion, Question, QuestionFigure, QuestionOption, QuestionPaperUpload)
from .presentation import (answer_key_preview_questions, decorate_staged_question, missing_crop_pages,
                           primary_question_crops, unavailable_crop_pages, user_diagnostic)


def _approval_error(question):
    """Keep known-bad extraction results out of the question bank."""
    if question.is_flagged:
        return "Review this flagged question and clear the flag before approval."
    crops = primary_question_crops(question, image_first=question.paper.image_first, private=False)
    usable_crop = bool(crops and not unavailable_crop_pages(crops, private=False)
                       and not missing_crop_pages(question, crops))
    if not question.question_text.strip() and not usable_crop:
        return "Add a question statement or a complete original question image."
    options = question.options if isinstance(question.options, list) else []
    if any(not isinstance(option, dict) for option in options):
        return "The options are invalid. Edit them before approval."
    keys = [option.get("key", "").strip().upper() if isinstance(option.get("key"), str) else "" for option in options]
    if not all(keys) or len(keys) != len(set(keys)):
        return "Option labels must be present and unique."
    image_keys = set(question.figures.exclude(option_key="").values_list("option_key", flat=True))
    if image_keys - set(keys):
        return "An image is assigned to an option that no longer exists."
    if len(options) < 2 or any(
        not isinstance(option.get("text"), str) or (not option["text"].strip() and key not in image_keys
            and not (usable_crop and option.get("visible_in_crop") is True))
        for option, key in zip(options, keys)
    ):
        return "At least two options need text, an image, or checked labels in a complete original crop."
    if question.correct_option and question.correct_option.strip().upper() not in keys:
        return "The answer key does not match an option."
    return ""


def _bank_question_type(staged):
    """Map a staged detected type to a supported bank question type.

    Essay/subjective has no bank equivalent and stays single-correct; the
    reviewer must convert it manually before it is useful.
    """
    mapping = {
        ExtractedQuestion.DetectedType.MULTIPLE: Question.QuestionType.MCQ_MULTIPLE,
        ExtractedQuestion.DetectedType.NUMERICAL: Question.QuestionType.NUMERICAL,
    }
    return mapping.get(staged.detected_type, Question.QuestionType.MCQ_SINGLE)


def _infer_language(staged):
    """Conservative metadata hint; the teacher can change it in the bank."""
    text = " ".join([staged.question_text] + [o.get("text", "") for o in staged.options])
    has_hindi = bool(re.search(r"[\u0900-\u097f]", text))
    # Avoid counting LaTeX command names such as \frac as English prose.
    prose = re.sub(r"\\[A-Za-z]+", "", text)
    has_english = bool(re.search(r"[A-Za-z]{2,}", prose))
    return Question.Language.BILINGUAL if has_hindi and has_english else Question.Language.HINDI if has_hindi else Question.Language.ENGLISH


def _confirmed_staging_key(question):
    """Never promote a suggestion, or a non-single-correct answer, as a key."""
    if (question.detected_type == ExtractedQuestion.DetectedType.SINGLE
            and question.answer_status == AnswerStatus.MANUALLY_VERIFIED):
        return (question.correct_option or "").strip().upper()
    return ""


def _display_gradeable_bank_key(question):
    """PDF answer flags are only a grading key after a single-answer check."""
    if (question.question_type != Question.QuestionType.MCQ_SINGLE
            or sum(option.is_correct for option in question.options.all()) != 1):
        return False
    if not question.is_pdf_derived:
        return True  # retain ordinary manually-authored single-choice behavior
    return question.answer_status == AnswerStatus.MANUALLY_VERIFIED


def _equivalent_bank_question(question):
    """Only link an existing question when doing so cannot discard content."""
    if question.image or question.figures.exists() or question.tables or question.source_regions or question.is_image_first:
        return None
    correct = _confirmed_staging_key(question)
    staged = [(o["key"].strip().upper(), o.get("text", ""), o["key"].strip().upper() == correct and bool(correct))
              for o in question.options]
    for existing in Question.objects.filter(is_private_practice=False, is_classroom_snapshot=False, category=question.paper.category, question_text__iexact=question.question_text.strip()):
        if (existing.image or existing.figures.exists() or existing.is_image_first or not existing.is_pdf_derived
                or bool(existing.answer_status == AnswerStatus.MANUALLY_VERIFIED) != bool(correct)
                or existing.question_type != _bank_question_type(question)
                or existing.explanation != question.explanation
                or existing.positive_marks != question.positive_marks
                or existing.negative_marks != question.negative_marks
                or existing.subject != (question.paper.subject or "General")
                or existing.topic != (question.paper.target_exam or "")
                or existing.language != _infer_language(question)):
            continue
        opts = list(existing.options.all())
        if any(opt.image for opt in opts):
            continue
        if [(o.option_key, o.option_text, o.is_correct) for o in opts] == staged:
            return existing
    return None


def _create_bank_options_and_figures(staged, bank):
    correct = _confirmed_staging_key(staged)
    options = {}
    for index, opt in enumerate(staged.options):
        key = opt["key"].strip().upper()
        options[key] = QuestionOption.objects.create(
            question=bank, option_key=key, option_text=opt.get("text", ""),
            is_correct=(key == correct and bool(correct)), order=index,
        )
    for figure in staged.figures.all():
        QuestionFigure.objects.create(
            question=bank, option=options.get(figure.option_key) if figure.option_key else None,
            image=figure.image.name, order=figure.order, page_number=figure.page_number,
            bounding_box=figure.bounding_box, source=figure.source,
        )


class QuestionPaperListView(TeacherRequiredMixin, ListView):
    """
    Lists uploaded question papers with status, question counts, and quick actions.
    Scoped: Teachers see papers they uploaded or categories permitted; Admins see all.
    """

    model = QuestionPaperUpload
    template_name = "questions/paper_list.html"
    context_object_name = "papers"
    paginate_by = 10

    def get_queryset(self):
        user = self.request.user
        if user.is_admin_role:
            qs = QuestionPaperUpload.objects.all()
        else:
            # Teacher: papers uploaded by teacher or in assigned categories
            assigned_cats = user.assigned_categories.all()
            if assigned_cats.exists():
                qs = QuestionPaperUpload.objects.filter(
                    Q(uploaded_by=user) | Q(category__in=assigned_cats)
                )
            else:
                qs = QuestionPaperUpload.objects.filter(uploaded_by=user)

        qs = qs.filter(is_student_private=False, is_classroom_private=False).select_related("category", "uploaded_by").annotate(
            total_extracted=Count("extracted_questions")
        ).order_by("-created_at")

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(title__icontains=query)
                | Q(subject__icontains=query)
                | Q(target_exam__icontains=query)
            )

        status_filter = self.request.GET.get("status", "").strip()
        if status_filter:
            qs = qs.filter(status=status_filter)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_query"] = self.request.GET.get("q", "")
        context["status_filter"] = self.request.GET.get("status", "")
        context["total_papers_count"] = self.get_queryset().count()
        return context


class QuestionPaperUploadView(TeacherRequiredMixin, CreateView):
    """
    Handles PDF question paper upload, validates integrity, and initiates
    automatic PyMuPDF text, equation, and diagram extraction.
    """

    model = QuestionPaperUpload
    form_class = QuestionPaperUploadForm
    template_name = "questions/paper_upload.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        paper = form.save(commit=False)
        paper.uploaded_by = self.request.user
        paper.file_size_bytes = form.cleaned_data["pdf_file"].size
        paper.total_pages = getattr(form, "pdf_page_count", 0)
        # Upload is local-only (enforced by the form). Cloud/MinerU provider code
        # and migrations are preserved for future use but are not offered here.
        paper.extraction_mode = QuestionPaperUpload.ExtractionMode.LOCAL
        paper.save()

        log_audit(
            self.request.user,
            "PDF_PAPER_UPLOADED",
            f"Uploaded question paper PDF '{paper.title}' ({paper.pdf_file.name})",
            request=self.request,
        )

        extractor = PDFQuestionExtractor(paper)
        questions_count = extractor.extract()
        if questions_count > 0:
            messages.success(self.request, f"Paper '{paper.title}' processed: {questions_count} questions are ready for review.")
        else:
            messages.warning(self.request, "No structured questions could be identified. Review the extraction log or add questions manually.")

        return redirect("questions:paper_review", pk=paper.pk)


class QuestionPaperReviewView(TeacherRequiredMixin, DetailView):
    """
    Interactive review workspace for an uploaded paper.
    Displays extracted questions, options, diagram previews, answer key status,
    and bulk approval controls.
    """

    model = QuestionPaperUpload
    template_name = "questions/paper_review.html"
    context_object_name = "paper"

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        patch_cache_control(response, private=True, no_store=True)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response

    def get_object(self, queryset=None):
        paper = super().get_object(queryset)
        if paper.uses_private_storage:
            raise Http404()
        user = self.request.user
        # Authorization check: Admin or paper uploader or permitted category
        if not user.is_admin_role and paper.uploaded_by != user:
            if not user.assigned_categories.filter(pk=paper.category_id).exists():
                raise PermissionDenied("You do not have permission to view or review this question paper.")
        return paper

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        paper = self.get_object()

        # Questions filter
        filter_mode = self.request.GET.get("filter", "all")
        qs = paper.extracted_questions.prefetch_related("figures").order_by("question_number")

        if filter_mode == "flagged":
            qs = qs.filter(is_flagged=True)
        elif filter_mode == "pending":
            qs = qs.filter(status=ExtractedQuestion.Status.EXTRACTED)
        elif filter_mode == "approved":
            qs = qs.filter(status=ExtractedQuestion.Status.APPROVED)

        questions = list(qs)
        for question in questions:
            decorate_staged_question(question, image_first=paper.image_first, private=False)
        context["questions"] = questions
        context["filter_mode"] = filter_mode
        context["total_extracted"] = paper.extracted_questions.count()
        context["flagged_count"] = paper.extracted_questions.filter(is_flagged=True).count()
        context["approved_count"] = paper.extracted_questions.filter(
            status=ExtractedQuestion.Status.APPROVED
        ).count()
        context["answer_key_form"] = AnswerKeyImportForm()
        context["can_manage_answer_keys"] = self.request.user.is_admin_role or paper.uploaded_by_id == self.request.user.pk
        if context["can_manage_answer_keys"]:
            eligible = paper.extracted_questions.exclude(status=ExtractedQuestion.Status.REJECTED).order_by("question_number", "id")
            context["answer_key_preview_questions"] = answer_key_preview_questions(eligible, exclude_approved=True)
        context["unassigned_figure_count"] = paper.figures.filter(question__isnull=True).count()
        context["extraction_warnings"] = [
            user_diagnostic(line.removeprefix("Warning: "))
            for line in paper.extraction_log.splitlines()
            if line.startswith("Warning: ")
        ]
        context["extraction_log_display"] = user_diagnostic(paper.extraction_log)
        return context


class QuestionPaperDownloadView(TeacherRequiredMixin, View):
    """
    Secure file streaming endpoint for uploaded question paper PDFs.
    Ensures that only authorized Admins and authorized Teachers can access or download
    the original PDF files. Blocks unauthorized candidates and students.
    """

    def get(self, request, pk):
        paper = get_object_or_404(QuestionPaperUpload, pk=pk, is_student_private=False, is_classroom_private=False)
        user = request.user
        if not user.is_admin_role and paper.uploaded_by != user:
            if not user.assigned_categories.filter(pk=paper.category_id).exists():
                raise PermissionDenied("You do not have permission to access or download this question paper.")

        if not paper.pdf_file:
            raise Http404("Original PDF file not found on disk.")

        try:
            file_handle = paper.pdf_file.open("rb")
        except FileNotFoundError:
            raise Http404("PDF document was not found in storage.")

        filename = paper.pdf_file.name.split("/")[-1]
        response = FileResponse(file_handle, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response


class ExtractedQuestionEditView(TeacherRequiredMixin, UpdateView):
    """
    Edit question statement, options, diagram, and manual answer key.
    """

    model = ExtractedQuestion
    form_class = ExtractedQuestionEditForm
    template_name = "questions/question_edit.html"
    context_object_name = "question"

    def get_object(self, queryset=None):
        q = super().get_object(queryset)
        if (isinstance(q, ExtractedQuestion) and q.paper.uses_private_storage) or (isinstance(q, Question) and (q.is_private_practice or q.is_classroom_snapshot)):
            raise Http404()
        user = self.request.user
        if not user.is_admin_role and q.paper.uploaded_by != user:
            if not user.assigned_categories.filter(pk=q.paper.category_id).exists():
                raise PermissionDenied("You cannot edit this extracted question.")
        return q

    def form_valid(self, form):
        extracted_q = form.save(commit=False)
        if extracted_q.status == ExtractedQuestion.Status.APPROVED:
            messages.warning(self.request, "This question is already in the bank. Edit the bank item instead.")
            return redirect("questions:question_edit", pk=extracted_q.approved_question_id) if extracted_q.approved_question_id else redirect("questions:paper_review", pk=extracted_q.paper_id)

        # Process dynamic option inputs from POST data
        # Reads option_key_*, option_text_*
        options = []
        crop_keys = set(form.cleaned_data.get("visible_crop_choices") or [])
        for key in ["A", "B", "C", "D", "E"]:
            opt_text = self.request.POST.get(f"option_text_{key}", "").strip()
            if opt_text or key in crop_keys or extracted_q.figures.filter(option_key=key).exists():
                option = {"key": key, "text": opt_text}
                if key in crop_keys:
                    option["visible_in_crop"] = True
                options.append(option)

        if (extracted_q.detected_type == ExtractedQuestion.DetectedType.SINGLE and extracted_q.correct_option
                and extracted_q.correct_option not in {option["key"] for option in options}):
            form.add_error("correct_option", "Choose an existing option, or leave the key blank.")
            return self.form_invalid(form)

        # Allow all options to be cleared as well as changed. The reviewer
        # controls the flag in the form; unrelated warnings must not be
        # silently cleared merely because two options were entered.
        extracted_q.options = options
        extracted_q.answer_status = edited_answer_status(form.previous_key, form.previous_answer_status,
            extracted_q.correct_option, form.cleaned_data["confirm_answer_key"], AnswerStatus.MANUALLY_VERIFIED,
            content_changed=(form.previous_content != staged_question_signature(extracted_q) or "image" in form.changed_data))
        if extracted_q.detected_type != ExtractedQuestion.DetectedType.SINGLE:
            extracted_q.answer_status = AnswerStatus.NEEDS_REVIEW
        if not extracted_q.is_flagged:
            extracted_q.flag_reason = ""

        # Set status to reviewed if was in extracted
        if extracted_q.status == ExtractedQuestion.Status.EXTRACTED:
            extracted_q.status = ExtractedQuestion.Status.REVIEWED

        extracted_q.save()
        if extracted_q.correct_option and extracted_q.answer_status != AnswerStatus.MANUALLY_VERIFIED:
            messages.warning(self.request, "Question saved. Its answer remains unconfirmed and will not be copied as a grading key unless checked.")
        else:
            messages.success(self.request, f"Question #{extracted_q.question_number} updated successfully.")
        return redirect("questions:paper_review", pk=extracted_q.paper.pk)


class ExtractedQuestionDeleteView(TeacherRequiredMixin, View):
    """Deletes an incorrectly extracted question from the staging workspace."""

    def post(self, request, pk):
        q = get_object_or_404(ExtractedQuestion, pk=pk, paper__is_student_private=False, paper__is_classroom_private=False)
        paper = q.paper
        if not request.user.is_admin_role and paper.uploaded_by != request.user:
            raise PermissionDenied("You cannot delete questions from this paper.")

        if q.status == ExtractedQuestion.Status.APPROVED:
            messages.warning(request, "This item is already approved; its staging record is kept as source provenance.")
            return redirect("questions:paper_review", pk=paper.pk)
        q_num = q.question_number
        q.delete()
        # Decrement counter
        paper.questions_found = paper.extracted_questions.count()
        paper.save(update_fields=["questions_found"])

        messages.info(request, f"Question #{q_num} has been discarded from review workspace.")
        return redirect("questions:paper_review", pk=paper.pk)


class ExtractedQuestionFlagToggleView(TeacherRequiredMixin, View):
    """Flags or unflags a question for manual inspection."""

    def post(self, request, pk):
        q = get_object_or_404(ExtractedQuestion, pk=pk, paper__is_student_private=False, paper__is_classroom_private=False)
        if not request.user.is_admin_role and q.paper.uploaded_by != request.user:
            raise PermissionDenied()

        if q.status == ExtractedQuestion.Status.APPROVED:
            messages.warning(request, "This item is already approved. Edit the bank item instead.")
            return redirect("questions:paper_review", pk=q.paper.pk)
        q.is_flagged = not q.is_flagged
        if not q.is_flagged:
            q.flag_reason = ""
        else:
            q.flag_reason = "Flagged by reviewer."
        q.save(update_fields=["is_flagged", "flag_reason"])

        state = "flagged for review" if q.is_flagged else "unflagged"
        messages.success(request, f"Question #{q.question_number} {state}.")
        return redirect("questions:paper_review", pk=q.paper.pk)


class ExtractedQuestionApproveView(TeacherRequiredMixin, View):
    """
    Approves a single question and pushes it into the permanent Question Bank.
    """

    def post(self, request, pk):
        q = get_object_or_404(ExtractedQuestion, pk=pk, paper__is_student_private=False, paper__is_classroom_private=False)
        paper = q.paper
        if not request.user.is_admin_role and paper.uploaded_by != request.user:
            raise PermissionDenied("You cannot approve questions from this paper.")

        if q.status == ExtractedQuestion.Status.APPROVED:
            messages.info(request, f"Question #{q.question_number} is already in the Question Bank.")
            return redirect("questions:paper_review", pk=paper.pk)
        if q.status == ExtractedQuestion.Status.REJECTED:
            messages.warning(request, f"Question #{q.question_number} was rejected and cannot be approved.")
            return redirect("questions:paper_review", pk=paper.pk)
        error = _approval_error(q)
        if error:
            messages.warning(request, f"Question #{q.question_number}: {error}")
            return redirect("questions:paper_review", pk=paper.pk)

        # Check for duplicate question already existing in the Question Bank
        existing_bank_q = _equivalent_bank_question(q)

        if existing_bank_q:
            # Prevent accidental duplicate import - link existing item
            q.status = ExtractedQuestion.Status.APPROVED
            q.approved_question = existing_bank_q
            q.is_flagged = False
            q.flag_reason = ""
            q.save()
            messages.info(
                request,
                f"Question #{q.question_number} matches an existing question in the Question Bank (ID #{existing_bank_q.pk}). Linked without creating a duplicate.",
            )
            return redirect("questions:paper_review", pk=paper.pk)

        # Create permanent Question Bank item inside an atomic transaction
        with transaction.atomic():
            bank_q = Question.objects.create(
                category=paper.category,
                subject=paper.subject or "General",
                topic=paper.target_exam or "",
                question_text=q.question_text, tables=q.tables, source_regions=q.source_regions, primary_image_region=q.primary_image_region,
                language=_infer_language(q),
                question_type=_bank_question_type(q), is_image_first=q.is_image_first, is_pdf_derived=True,
                answer_status=AnswerStatus.MANUALLY_VERIFIED if _confirmed_staging_key(q) else AnswerStatus.NEEDS_REVIEW,
                explanation=q.explanation,
                positive_marks=q.positive_marks,
                negative_marks=q.negative_marks,
                source_paper=paper,
                creator=request.user,
            )

            if q.image:
                bank_q.image = q.image
                bank_q.save(update_fields=["image"])

            _create_bank_options_and_figures(q, bank_q)

            q.status = ExtractedQuestion.Status.APPROVED
            q.approved_question = bank_q
            q.is_flagged = False
            q.flag_reason = ""
            q.save()

            # Update paper counter
            paper.questions_approved = paper.extracted_questions.filter(
                status=ExtractedQuestion.Status.APPROVED
            ).count()
            paper.save(update_fields=["questions_approved"])

        log_audit(
            request.user,
            "QUESTION_APPROVED",
            f"Approved Question #{q.question_number} from paper '{paper.title}' into Question Bank.",
            request=request,
        )
        messages.success(
            request, f"Question #{q.question_number} approved and saved to Question Bank!"
        )
        return redirect("questions:paper_review", pk=paper.pk)


class QuestionPaperBulkApproveView(TeacherRequiredMixin, View):
    """
    Bulk approves all valid non-rejected questions in a paper into the permanent Question Bank.
    Detects and prevents duplicate imports into the Question Bank.
    """

    def post(self, request, pk):
        paper = get_object_or_404(QuestionPaperUpload, pk=pk, is_student_private=False, is_classroom_private=False)
        if not request.user.is_admin_role and paper.uploaded_by != request.user:
            raise PermissionDenied("You cannot bulk approve this paper.")

        questions_to_approve = paper.extracted_questions.exclude(
            status=ExtractedQuestion.Status.APPROVED
        ).exclude(status=ExtractedQuestion.Status.REJECTED)

        count = 0
        duplicate_count = 0
        skipped_count = 0
        with transaction.atomic():
            for q in questions_to_approve:
                if _approval_error(q):
                    skipped_count += 1
                    continue
                # Check for existing duplicate in Question Bank
                existing_bank_q = _equivalent_bank_question(q)

                if existing_bank_q:
                    q.status = ExtractedQuestion.Status.APPROVED
                    q.approved_question = existing_bank_q
                    q.is_flagged = False
                    q.flag_reason = ""
                    q.save()
                    duplicate_count += 1
                    continue

                # Create question in bank
                bank_q = Question.objects.create(
                    category=paper.category,
                    subject=paper.subject or "General",
                    topic=paper.target_exam or "",
                    question_text=q.question_text, tables=q.tables, source_regions=q.source_regions, primary_image_region=q.primary_image_region,
                    language=_infer_language(q),
                    question_type=_bank_question_type(q), is_image_first=q.is_image_first, is_pdf_derived=True,
                    answer_status=AnswerStatus.MANUALLY_VERIFIED if _confirmed_staging_key(q) else AnswerStatus.NEEDS_REVIEW,
                    explanation=q.explanation,
                    positive_marks=q.positive_marks,
                    negative_marks=q.negative_marks,
                    source_paper=paper,
                    creator=request.user,
                )
                if q.image:
                    bank_q.image = q.image
                    bank_q.save(update_fields=["image"])

                _create_bank_options_and_figures(q, bank_q)

                q.status = ExtractedQuestion.Status.APPROVED
                q.approved_question = bank_q
                q.is_flagged = False
                q.flag_reason = ""
                q.save()
                count += 1

            paper.questions_approved = paper.extracted_questions.filter(
                status=ExtractedQuestion.Status.APPROVED
            ).count()
            pending_review = paper.extracted_questions.exclude(
                status__in=[ExtractedQuestion.Status.APPROVED, ExtractedQuestion.Status.REJECTED]
            ).exists()
            paper.status = (
                QuestionPaperUpload.ProcessingStatus.EXTRACTED
                if pending_review else QuestionPaperUpload.ProcessingStatus.COMPLETED
            )
            paper.save(update_fields=["questions_approved", "status"])

        log_audit(
            request.user,
            "PAPER_BULK_APPROVED",
            f"Bulk approved {count} questions ({duplicate_count} duplicates linked) from '{paper.title}' into Question Bank.",
            request=request,
        )
        msg = f"Successfully approved {count} new questions into the permanent Question Bank!"
        if duplicate_count > 0:
            msg += f" ({duplicate_count} existing duplicate questions were automatically linked without redundancy)."
        messages.success(request, msg)
        if skipped_count:
            messages.warning(request, f"{skipped_count} flagged or incomplete question(s) were skipped. Review them before approval.")
        return redirect("questions:paper_review", pk=paper.pk)


class AnswerKeyImportView(TeacherRequiredMixin, View):
    """Apply only explicitly checked, fully validated keys to staging questions.

    Approval copies staging into the bank; changing an already approved record
    here would not update the bank and could mislead its owner, so it is rejected.
    """

    def post(self, request, pk):
        with transaction.atomic():
            paper = get_object_or_404(QuestionPaperUpload.objects.select_for_update(), pk=pk,
                                      is_student_private=False, is_classroom_private=False)
            if not request.user.is_admin_role and paper.uploaded_by_id != request.user.pk:
                raise PermissionDenied("You cannot import answer keys for this paper.")
            if paper.status in (paper.ProcessingStatus.PENDING, paper.ProcessingStatus.PROCESSING):
                raise Http404("Wait for extraction to finish before editing this paper.")
            questions = list(paper.extracted_questions.select_for_update().exclude(
                status=ExtractedQuestion.Status.REJECTED).order_by("question_number", "id"))
            form = AnswerKeyImportForm(request.POST, questions=questions)
            if not form.is_valid():
                # Re-render, retaining typed values and validation messages;
                # nothing is partially applied to the paper or bank.
                view = QuestionPaperReviewView()
                view.setup(request, pk=pk)
                view.object = paper
                context = view.get_context_data()
                context.update(answer_key_form=form, answer_key_open=True)
                response = render(request, view.template_name, context)
                patch_cache_control(response, private=True, no_store=True)
                return response
            for question in questions:
                if question.question_number in form.answer_keys:
                    question.correct_option = form.answer_keys[question.question_number]
                    question.answer_status = AnswerStatus.MANUALLY_VERIFIED
                    question.save(update_fields=["correct_option", "answer_status"])
        log_audit(request.user, "PAPER_ANSWER_KEYS_REVIEWED", f"Checked staging keys for paper {paper.pk}.", request=request)
        messages.success(request, f"Applied checked keys to {len(form.answer_keys)} staging question(s). Approved bank questions were not changed.")
        response = redirect("questions:paper_review", pk=paper.pk)
        patch_cache_control(response, private=True, no_store=True)
        return response


class QuestionBankListView(TeacherRequiredMixin, ListView):
    """
    Displays permanent Question Bank items with category, subject,
    language, and difficulty filters. Scoped by teacher permissions.
    """

    model = Question
    template_name = "questions/question_bank.html"
    context_object_name = "questions"
    paginate_by = 12

    def get_queryset(self):
        qs = Question.objects.filter(is_private_practice=False, is_classroom_snapshot=False).select_related("category", "source_paper", "creator").prefetch_related("options")

        user = self.request.user
        if not user.is_admin_role:
            assigned = user.assigned_categories.all()
            if assigned.exists():
                qs = qs.filter(Q(creator=user) | Q(category__in=assigned))
            else:
                qs = qs.filter(creator=user)

        cat_filter = self.request.GET.get("category", "").strip()
        if cat_filter:
            qs = qs.filter(category__slug=cat_filter)

        diff_filter = self.request.GET.get("difficulty", "").strip()
        if diff_filter:
            qs = qs.filter(difficulty=diff_filter)

        lang_filter = self.request.GET.get("language", "").strip()
        if lang_filter:
            qs = qs.filter(language=lang_filter)

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(question_text__icontains=query)
                | Q(subject__icontains=query)
                | Q(topic__icontains=query)
                | Q(chapter__icontains=query)
            )

        return qs.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.exams.models import Category
        user = self.request.user
        if not user.is_admin_role:
            assigned = user.assigned_categories.filter(is_active=True)
            context["categories"] = assigned if assigned.exists() else Category.objects.filter(is_active=True)
        else:
            context["categories"] = Category.objects.filter(is_active=True)

        context["selected_category"] = self.request.GET.get("category", "")
        context["selected_difficulty"] = self.request.GET.get("difficulty", "")
        context["selected_language"] = self.request.GET.get("language", "")
        context["search_query"] = self.request.GET.get("q", "")
        context["total_bank_count"] = self.get_queryset().count()
        for question in context["questions"]:
            question.display_gradeable_key = _display_gradeable_bank_key(question)
        return context


class QuestionCreateView(TeacherRequiredMixin, CreateView):
    """
    Allows authorized Teachers and Admins to manually author questions
    with LaTeX math formulas, MCQ options, and diagram image attachments.
    """

    model = Question
    from .forms import QuestionBankForm

    form_class = QuestionBankForm
    template_name = "questions/question_form.html"
    success_url = reverse_lazy("questions:question_bank")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        with transaction.atomic():
            question = form.save(commit=False)
            question.answer_status = AnswerStatus.MANUALLY_VERIFIED if form.cleaned_data.get("correct_option") else AnswerStatus.NEEDS_REVIEW
            question.creator = self.request.user
            question.save()

            opt_map = {
                "A": form.cleaned_data.get("option_text_A", "").strip(),
                "B": form.cleaned_data.get("option_text_B", "").strip(),
                "C": form.cleaned_data.get("option_text_C", "").strip(),
                "D": form.cleaned_data.get("option_text_D", "").strip(),
                "E": form.cleaned_data.get("option_text_E", "").strip(),
            }
            correct_key = form.cleaned_data.get("correct_option")

            idx = 0
            for key in ["A", "B", "C", "D", "E"]:
                text = opt_map[key]
                if text:
                    QuestionOption.objects.create(
                        question=question,
                        option_key=key,
                        option_text=text,
                        is_correct=(key == correct_key),
                        order=idx,
                    )
                    idx += 1

            log_audit(
                self.request.user,
                "QUESTION_CREATED",
                f"Created Question Bank item #{question.pk} in '{question.category.name}'",
                request=self.request,
            )
            messages.success(self.request, "Question successfully created in Question Bank!")
            return redirect("questions:question_bank")


class QuestionUpdateView(TeacherRequiredMixin, UpdateView):
    """
    Allows editing statement, options, marks, explanation, and images
    of an existing Question Bank item.
    """

    model = Question
    from .forms import QuestionBankForm

    form_class = QuestionBankForm
    template_name = "questions/question_form.html"
    context_object_name = "question"

    def get_object(self, queryset=None):
        q = super().get_object(queryset)
        if (isinstance(q, ExtractedQuestion) and q.paper.uses_private_storage) or (isinstance(q, Question) and (q.is_private_practice or q.is_classroom_snapshot)):
            raise Http404()
        user = self.request.user
        if not user.is_admin_role and q.creator != user:
            if not user.assigned_categories.filter(pk=q.category_id).exists():
                raise PermissionDenied("You do not have permission to edit this question.")
        return q

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        question = self.get_object()
        has_attempts = question.exam_instances.filter(exam__attempts__isnull=False).exists()
        if has_attempts and not self.request.user.is_admin_role:
            messages.warning(
                self.request,
                "Notice: This question is part of examinations with candidate attempts. Edits have been applied.",
            )

        with transaction.atomic():
            question = form.save(commit=False)
            opt_map = {
                "A": form.cleaned_data.get("option_text_A", "").strip(),
                "B": form.cleaned_data.get("option_text_B", "").strip(),
                "C": form.cleaned_data.get("option_text_C", "").strip(),
                "D": form.cleaned_data.get("option_text_D", "").strip(),
                "E": form.cleaned_data.get("option_text_E", "").strip(),
            }
            correct_key = form.cleaned_data.get("correct_option")
            existing = {opt.option_key: opt for opt in question.options.all()}
            if question.is_pdf_derived:
                new_choices = tuple(
                    (key, text) for key, text in opt_map.items()
                    if text or key in form.crop_only_keys or (key in existing and (existing[key].image or existing[key].figures.exists()))
                )
                content = (question.question_type, (question.question_text or "").strip(),
                           question.tables or [], new_choices)
                question.answer_status = edited_answer_status(
                    form.previous_key, form.previous_answer_status, correct_key,
                    form.cleaned_data.get("confirm_answer_key"), AnswerStatus.MANUALLY_VERIFIED,
                    content_changed=(content != form.previous_question_content or "image" in form.changed_data),
                )
                if question.question_type != Question.QuestionType.MCQ_SINGLE:
                    question.answer_status = AnswerStatus.NEEDS_REVIEW
                # Public PDF keys use the teacher-confirmation status, never a
                # stale private-practice verification flag.
                question.key_verified = False
            else:
                question.answer_status = AnswerStatus.MANUALLY_VERIFIED if correct_key else AnswerStatus.NEEDS_REVIEW
            question.save()
            form.save_m2m()

            # Update options in place: candidate responses and figure
            # associations refer to their IDs and must not be silently lost.
            for idx, key in enumerate(["A", "B", "C", "D", "E"]):
                text = opt_map[key]
                opt = existing.get(key)
                has_image = bool(opt and (opt.image or opt.figures.exists()))
                keep_in_crop = key in getattr(form, "crop_only_keys", set())
                if opt and not text and not has_image and not keep_in_crop and opt.selected_in_responses.exists():
                    # Preserve historical selections and their original text.
                    text = opt.option_text
                    messages.warning(self.request, f"Option {key} was retained because candidates have selected it.")
                if text or has_image or keep_in_crop:
                    if opt:
                        opt.option_text, opt.is_correct, opt.order = text, key == correct_key, idx
                        opt.save(update_fields=["option_text", "is_correct", "order"])
                    else:
                        QuestionOption.objects.create(question=question, option_key=key,
                                                      option_text=text, is_correct=(key == correct_key), order=idx)
                elif opt:
                    opt.delete()

            log_audit(
                self.request.user,
                "QUESTION_UPDATED",
                f"Updated Question Bank item #{question.pk}",
                request=self.request,
            )
            if question.is_pdf_derived and correct_key and question.answer_status != AnswerStatus.MANUALLY_VERIFIED:
                messages.warning(self.request, "Question saved. Its answer is unconfirmed and will be ungraded until checked.")
            else:
                messages.success(self.request, f"Question #{question.pk} updated successfully!")
            return redirect("questions:question_bank")


class QuestionDeleteView(TeacherRequiredMixin, View):
    """
    Deletes a question from Question Bank with protection against deleting
    questions that are locked in examinations with student attempts.
    """

    def post(self, request, pk):
        question = get_object_or_404(Question, pk=pk, is_private_practice=False, is_classroom_snapshot=False)
        user = request.user
        if not user.is_admin_role and question.creator != user:
            if not user.assigned_categories.filter(pk=question.category_id).exists():
                raise PermissionDenied("You do not have permission to delete this question.")

        # Protected against deleting if student attempts exist
        if question.exam_instances.filter(exam__attempts__isnull=False).exists():
            messages.error(
                request,
                "Cannot delete question: Candidate examination attempts already exist for it. Historical testing data is preserved.",
            )
            return redirect("questions:question_bank")

        # Protected if assigned to an exam
        if question.exam_instances.exists():
            messages.warning(
                request,
                "Cannot delete question: It is currently assigned to one or more examinations. Please remove it from the examination(s) first.",
            )
            return redirect("questions:question_bank")

        q_id = question.pk
        question.delete()
        log_audit(
            request.user,
            "QUESTION_DELETED",
            f"Deleted Question #{q_id}",
            request=request,
        )
        messages.success(request, f"Question #{q_id} deleted successfully.")
        return redirect("questions:question_bank")


class QuestionPreviewView(TeacherRequiredMixin, DetailView):
    """
    Detailed modal or standalone preview of a Question Bank item with KaTeX equation rendering.
    """

    model = Question
    template_name = "questions/question_preview.html"
    context_object_name = "question"

    def get_object(self, queryset=None):
        q = super().get_object(queryset)
        if (isinstance(q, ExtractedQuestion) and q.paper.uses_private_storage) or (isinstance(q, Question) and (q.is_private_practice or q.is_classroom_snapshot)):
            raise Http404()
        user = self.request.user
        if not user.is_admin_role and q.creator != user:
            if not user.assigned_categories.filter(pk=q.category_id).exists():
                raise PermissionDenied()
        q.display_gradeable_key = _display_gradeable_bank_key(q)
        decorate_staged_question(q, image_first=True, private=False)
        return q
