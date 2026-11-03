import json
import re
from django.contrib import messages
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View

from apps.core.permissions import TeacherRequiredMixin
from apps.core.utils import log_audit
from .extractor import PDFQuestionExtractor
from .forms import (
    AnswerKeyImportForm,
    ExtractedQuestionEditForm,
    QuestionPaperUploadForm,
)
from .models import (AnswerStatus, ExtractedFigure, ExtractedQuestion, Question, QuestionFigure, QuestionOption, QuestionPaperUpload)


def _approval_error(question):
    """Keep known-bad extraction results out of the question bank."""
    if question.is_flagged:
        return "Review this flagged question and clear the flag before approval."
    if not question.question_text.strip():
        return "A question statement is required."
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
        not isinstance(option.get("text"), str) or (not option["text"].strip() and key not in image_keys)
        for option, key in zip(options, keys)
    ):
        return "At least two options with text or an image are required."
    if question.correct_option and question.correct_option.strip().upper() not in keys:
        return "The answer key does not match an option."
    return ""


def _infer_language(staged):
    """Conservative metadata hint; the teacher can change it in the bank."""
    text = " ".join([staged.question_text] + [o.get("text", "") for o in staged.options])
    has_hindi = bool(re.search(r"[\u0900-\u097f]", text))
    # Avoid counting LaTeX command names such as \frac as English prose.
    prose = re.sub(r"\\[A-Za-z]+", "", text)
    has_english = bool(re.search(r"[A-Za-z]{2,}", prose))
    return Question.Language.BILINGUAL if has_hindi and has_english else Question.Language.HINDI if has_hindi else Question.Language.ENGLISH


def _equivalent_bank_question(question):
    """Only link an existing question when doing so cannot discard content."""
    if question.image or question.figures.exists() or question.tables:
        return None
    correct = question.correct_option.strip().upper()
    staged = [(o["key"].strip().upper(), o.get("text", ""), o["key"].strip().upper() == correct and bool(correct))
              for o in question.options]
    for existing in Question.objects.filter(is_private_practice=False, category=question.paper.category, question_text__iexact=question.question_text.strip()):
        if (existing.image or existing.figures.exists()
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
    correct = (staged.correct_option or "").strip().upper()
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

        qs = qs.filter(is_student_private=False).select_related("category", "uploaded_by").annotate(
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
        paper.extraction_mode = form.cleaned_data["extraction_mode"]
        if paper.extraction_mode in (QuestionPaperUpload.ExtractionMode.CLOUD, QuestionPaperUpload.ExtractionMode.GEMINI):
            paper.cloud_consent_at = timezone.now()
        paper.save()

        log_audit(
            self.request.user,
            "PDF_PAPER_UPLOADED",
            f"Uploaded question paper PDF '{paper.title}' ({paper.pdf_file.name})",
            request=self.request,
        )

        if paper.extraction_mode in (QuestionPaperUpload.ExtractionMode.CLOUD, QuestionPaperUpload.ExtractionMode.GEMINI):
            from .tasks import extract_cloud_paper

            # Enqueue after commit so a worker never sees an uncommitted upload.
            def enqueue():
                try:
                    extract_cloud_paper.delay(paper.pk)
                except Exception:
                    QuestionPaperUpload.objects.filter(pk=paper.pk).update(
                        status=QuestionPaperUpload.ProcessingStatus.FAILED,
                        extraction_log="Could not queue cloud extraction. Check the worker/broker configuration; no local fallback was run.",
                    )
            transaction.on_commit(enqueue)
            if settings.CELERY_TASK_ALWAYS_EAGER:
                messages.info(self.request, "Cloud extraction is running in the request in development mode. Review the result and diagnostics below.")
            else:
                messages.info(self.request, "Cloud extraction was queued. Refresh this page to see its status; review all results before approval.")
        else:
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

    def get_object(self, queryset=None):
        paper = super().get_object(queryset)
        if paper.is_student_private:
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

        context["questions"] = qs
        context["filter_mode"] = filter_mode
        context["total_extracted"] = paper.extracted_questions.count()
        context["flagged_count"] = paper.extracted_questions.filter(is_flagged=True).count()
        context["approved_count"] = paper.extracted_questions.filter(
            status=ExtractedQuestion.Status.APPROVED
        ).count()
        context["answer_key_form"] = AnswerKeyImportForm()
        context["unassigned_figure_count"] = paper.figures.filter(question__isnull=True).count()
        context["extraction_warnings"] = [
            line.removeprefix("Warning: ")
            for line in paper.extraction_log.splitlines()
            if line.startswith("Warning: ")
        ]
        return context


class QuestionPaperDownloadView(TeacherRequiredMixin, View):
    """
    Secure file streaming endpoint for uploaded question paper PDFs.
    Ensures that only authorized Admins and authorized Teachers can access or download
    the original PDF files. Blocks unauthorized candidates and students.
    """

    def get(self, request, pk):
        paper = get_object_or_404(QuestionPaperUpload, pk=pk, is_student_private=False)
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
        if (isinstance(q, ExtractedQuestion) and q.paper.is_student_private) or (isinstance(q, Question) and q.is_private_practice):
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
        for key in ["A", "B", "C", "D", "E"]:
            opt_text = self.request.POST.get(f"option_text_{key}", "").strip()
            if opt_text or extracted_q.figures.filter(option_key=key).exists():
                options.append({"key": key, "text": opt_text})

        # Allow all options to be cleared as well as changed. The reviewer
        # controls the flag in the form; unrelated warnings must not be
        # silently cleared merely because two options were entered.
        extracted_q.options = options
        extracted_q.answer_status = AnswerStatus.MANUALLY_VERIFIED if extracted_q.correct_option else AnswerStatus.NEEDS_REVIEW
        if not extracted_q.is_flagged:
            extracted_q.flag_reason = ""

        # Set status to reviewed if was in extracted
        if extracted_q.status == ExtractedQuestion.Status.EXTRACTED:
            extracted_q.status = ExtractedQuestion.Status.REVIEWED

        extracted_q.save()
        messages.success(
            self.request, f"Question #{extracted_q.question_number} updated successfully."
        )
        return redirect("questions:paper_review", pk=extracted_q.paper.pk)


class ExtractedQuestionDeleteView(TeacherRequiredMixin, View):
    """Deletes an incorrectly extracted question from the staging workspace."""

    def post(self, request, pk):
        q = get_object_or_404(ExtractedQuestion, pk=pk, paper__is_student_private=False)
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
        q = get_object_or_404(ExtractedQuestion, pk=pk, paper__is_student_private=False)
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
        q = get_object_or_404(ExtractedQuestion, pk=pk, paper__is_student_private=False)
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
                answer_status=AnswerStatus.MANUALLY_VERIFIED if q.correct_option else AnswerStatus.NEEDS_REVIEW,
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
        paper = get_object_or_404(QuestionPaperUpload, pk=pk, is_student_private=False)
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
                    answer_status=AnswerStatus.MANUALLY_VERIFIED if q.correct_option else AnswerStatus.NEEDS_REVIEW,
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
    """
    Parses and maps an answer key text string to questions in the paper.
    Supports formats like:
    1: A, 2: B, 3: C
    or
    1-A\n2-B\n3-C
    """

    def post(self, request, pk):
        paper = get_object_or_404(QuestionPaperUpload, pk=pk, is_student_private=False)
        if not request.user.is_admin_role and paper.uploaded_by != request.user:
            raise PermissionDenied("You cannot import answer keys for this paper.")

        key_text = request.POST.get("answer_key_text", "").strip()
        if not key_text:
            messages.error(request, "Please enter an answer key string.")
            return redirect("questions:paper_review", pk=paper.pk)

        # Regex to find pairs of (question_number, answer_letter)
        # e.g. "1: A" or "1. B" or "1 - C" or "1 A"
        pairs = re.findall(r"(\d+)\s*[:.\-]?\s*([A-Da-d1-4])", key_text)

        updated_count = 0
        norm_map = {"1": "A", "2": "B", "3": "C", "4": "D"}

        for q_num_str, ans_str in pairs:
            q_num = int(q_num_str)
            ans = norm_map.get(ans_str, ans_str.upper())

            # Find matching question in paper
            matched_qs = paper.extracted_questions.filter(question_number=q_num)
            for q in matched_qs:
                q.correct_option = ans
                q.answer_status = AnswerStatus.MANUALLY_VERIFIED
                q.save(update_fields=["correct_option", "answer_status"])
                updated_count += 1

        if updated_count > 0:
            messages.success(
                request, f"Successfully applied answer keys to {updated_count} question(s)!"
            )
        else:
            messages.warning(
                request, "Could not map any answer keys. Check format e.g. '1: A, 2: B, 3: C'."
            )

        return redirect("questions:paper_review", pk=paper.pk)


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
        qs = Question.objects.filter(is_private_practice=False).select_related("category", "source_paper", "creator").prefetch_related("options")

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
        if (isinstance(q, ExtractedQuestion) and q.paper.is_student_private) or (isinstance(q, Question) and q.is_private_practice):
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
            question.answer_status = AnswerStatus.MANUALLY_VERIFIED if form.cleaned_data.get("correct_option") else AnswerStatus.NEEDS_REVIEW
            question.save()
            form.save_m2m()

            opt_map = {
                "A": form.cleaned_data.get("option_text_A", "").strip(),
                "B": form.cleaned_data.get("option_text_B", "").strip(),
                "C": form.cleaned_data.get("option_text_C", "").strip(),
                "D": form.cleaned_data.get("option_text_D", "").strip(),
                "E": form.cleaned_data.get("option_text_E", "").strip(),
            }
            correct_key = form.cleaned_data.get("correct_option")

            # Update options in place: candidate responses and figure
            # associations refer to their IDs and must not be silently lost.
            existing = {opt.option_key: opt for opt in question.options.all()}
            for idx, key in enumerate(["A", "B", "C", "D", "E"]):
                text = opt_map[key]
                opt = existing.get(key)
                has_image = bool(opt and (opt.image or opt.figures.exists()))
                if opt and not text and not has_image and opt.selected_in_responses.exists():
                    # Preserve historical selections and their original text.
                    text = opt.option_text
                    messages.warning(self.request, f"Option {key} was retained because candidates have selected it.")
                if text or has_image:
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
            messages.success(self.request, f"Question #{question.pk} updated successfully!")
            return redirect("questions:question_bank")


class QuestionDeleteView(TeacherRequiredMixin, View):
    """
    Deletes a question from Question Bank with protection against deleting
    questions that are locked in examinations with student attempts.
    """

    def post(self, request, pk):
        question = get_object_or_404(Question, pk=pk, is_private_practice=False)
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
        if (isinstance(q, ExtractedQuestion) and q.paper.is_student_private) or (isinstance(q, Question) and q.is_private_practice):
            raise Http404()
        user = self.request.user
        if not user.is_admin_role and q.creator != user:
            if not user.assigned_categories.filter(pk=q.category_id).exists():
                raise PermissionDenied()
        return q
