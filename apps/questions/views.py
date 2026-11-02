import json
import re
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404
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
from .models import ExtractedQuestion, Question, QuestionOption, QuestionPaperUpload


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

        qs = qs.select_related("category", "uploaded_by").annotate(
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
        paper.save()

        log_audit(
            self.request.user,
            "PDF_PAPER_UPLOADED",
            f"Uploaded question paper PDF '{paper.title}' ({paper.pdf_file.name})",
            request=self.request,
        )

        # Execute automatic question extraction
        extractor = PDFQuestionExtractor(paper)
        questions_count = extractor.extract()

        if questions_count > 0:
            messages.success(
                self.request,
                f"Paper '{paper.title}' successfully processed! {questions_count} questions extracted into review workspace.",
            )
        else:
            messages.warning(
                self.request,
                f"Paper '{paper.title}' uploaded, but no structured questions could be automatically identified. You can review the extraction logs or manually add questions.",
            )

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
        qs = paper.extracted_questions.all().order_by("question_number")

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
        return context


class QuestionPaperDownloadView(TeacherRequiredMixin, View):
    """
    Secure file streaming endpoint for uploaded question paper PDFs.
    Ensures that only authorized Admins and authorized Teachers can access or download
    the original PDF files. Blocks unauthorized candidates and students.
    """

    def get(self, request, pk):
        paper = get_object_or_404(QuestionPaperUpload, pk=pk)
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
        user = self.request.user
        if not user.is_admin_role and q.paper.uploaded_by != user:
            if not user.assigned_categories.filter(pk=q.paper.category_id).exists():
                raise PermissionDenied("You cannot edit this extracted question.")
        return q

    def form_valid(self, form):
        extracted_q = form.save(commit=False)

        # Process dynamic option inputs from POST data
        # Reads option_key_*, option_text_*
        options = []
        for key in ["A", "B", "C", "D", "E"]:
            opt_text = self.request.POST.get(f"option_text_{key}", "").strip()
            if opt_text:
                options.append({"key": key, "text": opt_text})

        if options:
            extracted_q.options = options
            # If at least 2 options now exist, auto-clear incomplete flag
            if len(options) >= 2 and extracted_q.flag_reason.startswith("Only"):
                extracted_q.is_flagged = False
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
        q = get_object_or_404(ExtractedQuestion, pk=pk)
        paper = q.paper
        if not request.user.is_admin_role and paper.uploaded_by != request.user:
            raise PermissionDenied("You cannot delete questions from this paper.")

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
        q = get_object_or_404(ExtractedQuestion, pk=pk)
        if not request.user.is_admin_role and q.paper.uploaded_by != request.user:
            raise PermissionDenied()

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
        q = get_object_or_404(ExtractedQuestion, pk=pk)
        paper = q.paper
        if not request.user.is_admin_role and paper.uploaded_by != request.user:
            raise PermissionDenied("You cannot approve questions from this paper.")

        if q.status == ExtractedQuestion.Status.APPROVED:
            messages.info(request, f"Question #{q.question_number} is already in the Question Bank.")
            return redirect("questions:paper_review", pk=paper.pk)

        # Check for duplicate question already existing in the Question Bank
        clean_text = q.question_text.strip()
        existing_bank_q = Question.objects.filter(
            category=paper.category,
            question_text__iexact=clean_text,
        ).first()

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
                question_text=q.question_text,
                explanation=q.explanation,
                positive_marks=q.positive_marks,
                negative_marks=q.negative_marks,
                source_paper=paper,
                creator=request.user,
            )

            if q.image:
                bank_q.image = q.image
                bank_q.save(update_fields=["image"])

            # Create options
            correct_opt = (q.correct_option or "").strip().upper()
            for idx, opt in enumerate(q.options):
                opt_key = opt.get("key", "").upper()
                QuestionOption.objects.create(
                    question=bank_q,
                    option_key=opt_key,
                    option_text=opt.get("text", ""),
                    is_correct=(opt_key == correct_opt and bool(correct_opt)),
                    order=idx,
                )

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
        paper = get_object_or_404(QuestionPaperUpload, pk=pk)
        if not request.user.is_admin_role and paper.uploaded_by != request.user:
            raise PermissionDenied("You cannot bulk approve this paper.")

        questions_to_approve = paper.extracted_questions.exclude(
            status=ExtractedQuestion.Status.APPROVED
        ).exclude(status=ExtractedQuestion.Status.REJECTED)

        count = 0
        duplicate_count = 0
        with transaction.atomic():
            for q in questions_to_approve:
                # Check for existing duplicate in Question Bank
                clean_text = q.question_text.strip()
                existing_bank_q = Question.objects.filter(
                    category=paper.category,
                    question_text__iexact=clean_text,
                ).first()

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
                    question_text=q.question_text,
                    explanation=q.explanation,
                    positive_marks=q.positive_marks,
                    negative_marks=q.negative_marks,
                    source_paper=paper,
                    creator=request.user,
                )
                if q.image:
                    bank_q.image = q.image
                    bank_q.save(update_fields=["image"])

                correct_opt = (q.correct_option or "").strip().upper()
                for idx, opt in enumerate(q.options):
                    opt_key = opt.get("key", "").upper()
                    QuestionOption.objects.create(
                        question=bank_q,
                        option_key=opt_key,
                        option_text=opt.get("text", ""),
                        is_correct=(opt_key == correct_opt and bool(correct_opt)),
                        order=idx,
                    )

                q.status = ExtractedQuestion.Status.APPROVED
                q.approved_question = bank_q
                q.is_flagged = False
                q.flag_reason = ""
                q.save()
                count += 1

            paper.questions_approved = paper.extracted_questions.filter(
                status=ExtractedQuestion.Status.APPROVED
            ).count()
            paper.status = QuestionPaperUpload.ProcessingStatus.COMPLETED
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
        paper = get_object_or_404(QuestionPaperUpload, pk=pk)
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
                q.save(update_fields=["correct_option"])
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
        qs = Question.objects.select_related("category", "source_paper", "creator").prefetch_related("options")

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
            question.creator = self.request.user
            question.save()

            opt_map = {
                "A": form.cleaned_data.get("option_text_A", "").strip(),
                "B": form.cleaned_data.get("option_text_B", "").strip(),
                "C": form.cleaned_data.get("option_text_C", "").strip(),
                "D": form.cleaned_data.get("option_text_D", "").strip(),
            }
            correct_key = form.cleaned_data.get("correct_option")

            idx = 0
            for key in ["A", "B", "C", "D"]:
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
            question = form.save()

            opt_map = {
                "A": form.cleaned_data.get("option_text_A", "").strip(),
                "B": form.cleaned_data.get("option_text_B", "").strip(),
                "C": form.cleaned_data.get("option_text_C", "").strip(),
                "D": form.cleaned_data.get("option_text_D", "").strip(),
            }
            correct_key = form.cleaned_data.get("correct_option")

            # Re-create options cleanly
            question.options.all().delete()
            idx = 0
            for key in ["A", "B", "C", "D"]:
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
        question = get_object_or_404(Question, pk=pk)
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
        user = self.request.user
        if not user.is_admin_role and q.creator != user:
            if not user.assigned_categories.filter(pk=q.category_id).exists():
                raise PermissionDenied()
        return q
