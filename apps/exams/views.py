import random
from decimal import Decimal
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, ProtectedError, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View
from apps.core.permissions import AdminRequiredMixin, TeacherRequiredMixin
from apps.core.utils import log_audit
from apps.questions.models import Question
from .forms import CategoryForm, ExamForm, ExamRandomQuestionForm
from .models import Category, Exam, ExamQuestion


# ==============================================================================
# CATEGORY MANAGEMENT VIEWS
# ==============================================================================

class CategoryListView(AdminRequiredMixin, ListView):
    """
    Lists all dynamic examination categories with search, status filtering,
    and associated examination counts.
    """

    model = Category
    template_name = "exams/category_list.html"
    context_object_name = "categories"
    paginate_by = 10

    def get_queryset(self):
        qs = (
            Category.objects.annotate(
                total_exams=Count("exams"),
            )
            .order_by("display_order", "name")
        )
        search_query = self.request.GET.get("q", "").strip()
        if search_query:
            qs = qs.filter(name__icontains=search_query)

        status_filter = self.request.GET.get("status", "").strip()
        if status_filter == "active":
            qs = qs.filter(is_active=True)
        elif status_filter == "inactive":
            qs = qs.filter(is_active=False)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_query"] = self.request.GET.get("q", "")
        context["status_filter"] = self.request.GET.get("status", "")
        context["total_category_count"] = Category.objects.count()
        context["active_category_count"] = Category.objects.filter(is_active=True).count()
        return context


class CategoryCreateView(AdminRequiredMixin, CreateView):
    """Creates a new examination category."""

    model = Category
    form_class = CategoryForm
    template_name = "exams/category_form.html"
    success_url = reverse_lazy("exams:category_list")

    def form_valid(self, form):
        category = form.save()
        log_audit(
            self.request.user,
            "CATEGORY_CREATED",
            f"Created examination category '{category.name}'",
            request=self.request,
        )
        messages.success(self.request, f"Category '{category.name}' created successfully.")
        return super().form_valid(form)


class CategoryUpdateView(AdminRequiredMixin, UpdateView):
    """Updates an existing examination category."""

    model = Category
    form_class = CategoryForm
    template_name = "exams/category_form.html"
    success_url = reverse_lazy("exams:category_list")

    def form_valid(self, form):
        category = form.save()
        log_audit(
            self.request.user,
            "CATEGORY_UPDATED",
            f"Updated examination category '{category.name}'",
            request=self.request,
        )
        messages.success(self.request, f"Category '{category.name}' updated successfully.")
        return super().form_valid(form)


class CategoryToggleStatusView(AdminRequiredMixin, View):
    """Toggles category active/inactive status."""

    def post(self, request, pk):
        category = get_object_or_404(Category, pk=pk)
        category.is_active = not category.is_active
        category.save(update_fields=["is_active"])

        state_str = "activated" if category.is_active else "deactivated"
        log_audit(
            request.user,
            "CATEGORY_STATUS_TOGGLED",
            f"Category '{category.name}' {state_str}.",
            request=request,
        )
        messages.success(request, f"Category '{category.name}' has been {state_str}.")
        return redirect("exams:category_list")


class CategoryDeleteView(AdminRequiredMixin, View):
    """
    Deletes a category safely.
    Prevents deletion if any examinations are associated with it.
    """

    def post(self, request, pk):
        category = get_object_or_404(Category, pk=pk)

        # Validate if examinations exist
        exam_count = category.exams.count()
        if exam_count > 0:
            messages.error(
                request,
                f"Cannot delete category '{category.name}'. It is currently associated with {exam_count} examination(s). Archive or reassign the examinations first.",
            )
            return redirect("exams:category_list")

        cat_name = category.name
        try:
            category.delete()
            log_audit(
                request.user,
                "CATEGORY_DELETED",
                f"Deleted examination category '{cat_name}'",
                request=request,
            )
            messages.success(request, f"Category '{cat_name}' has been deleted successfully.")
        except ProtectedError:
            messages.error(
                request,
                f"Cannot delete category '{cat_name}' because other database objects reference it.",
            )

        return redirect("exams:category_list")


# ==============================================================================
# EXAMINATION MANAGEMENT & BUILDER VIEWS
# ==============================================================================

class ExamListView(TeacherRequiredMixin, ListView):
    """
    Lists examinations with status tabs (All, Drafts, Published, Scheduled, Expired),
    search, and category filters. Scoped by teacher permissions.
    """

    model = Exam
    template_name = "exams/exam_list.html"
    context_object_name = "exams"
    paginate_by = 10

    def get_queryset(self):
        user = self.request.user
        qs = Exam.objects.select_related("category", "creator").annotate(
            total_q_count=Count("exam_questions", distinct=True),
            total_att_count=Count("attempts", distinct=True),
        )

        if not user.is_admin_role:
            assigned = user.assigned_categories.all()
            if assigned.exists():
                qs = qs.filter(Q(creator=user) | Q(category__in=assigned))
            else:
                qs = qs.filter(creator=user)

        # Tab filtering
        tab = self.request.GET.get("tab", "all").strip().lower()
        now = timezone.now()

        if tab == "draft":
            qs = qs.filter(status=Exam.ExamStatus.DRAFT)
        elif tab == "published":
            qs = qs.filter(status=Exam.ExamStatus.PUBLISHED)
        elif tab == "scheduled":
            qs = qs.filter(
                Q(status=Exam.ExamStatus.SCHEDULED)
                | (Q(status=Exam.ExamStatus.PUBLISHED) & Q(start_time__gt=now))
            )
        elif tab == "expired":
            qs = qs.filter(end_time__isnull=False, end_time__lt=now)

        # Search filter
        search_query = self.request.GET.get("q", "").strip()
        if search_query:
            qs = qs.filter(
                Q(title__icontains=search_query)
                | Q(subject__icontains=search_query)
                | Q(target_exam__icontains=search_query)
            )

        cat_filter = self.request.GET.get("category", "").strip()
        if cat_filter:
            qs = qs.filter(category__slug=cat_filter)

        return qs.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        base_qs = Exam.objects.all()
        if not user.is_admin_role:
            assigned = user.assigned_categories.all()
            if assigned.exists():
                base_qs = base_qs.filter(Q(creator=user) | Q(category__in=assigned))
            else:
                base_qs = base_qs.filter(creator=user)

        now = timezone.now()
        context["tab"] = self.request.GET.get("tab", "all")
        context["count_all"] = base_qs.count()
        context["count_draft"] = base_qs.filter(status=Exam.ExamStatus.DRAFT).count()
        context["count_published"] = base_qs.filter(status=Exam.ExamStatus.PUBLISHED).count()
        context["count_scheduled"] = base_qs.filter(
            Q(status=Exam.ExamStatus.SCHEDULED)
            | (Q(status=Exam.ExamStatus.PUBLISHED) & Q(start_time__gt=now))
        ).count()
        context["count_expired"] = base_qs.filter(end_time__isnull=False, end_time__lt=now).count()

        if not user.is_admin_role:
            assigned = user.assigned_categories.filter(is_active=True)
            context["categories"] = assigned if assigned.exists() else Category.objects.filter(is_active=True)
        else:
            context["categories"] = Category.objects.filter(is_active=True)

        context["selected_category"] = self.request.GET.get("category", "")
        context["search_query"] = self.request.GET.get("q", "")
        return context


class ExamCreateView(TeacherRequiredMixin, CreateView):
    """
    Creates a new CBT examination in Draft mode and navigates
    directly to the interactive Question Builder.
    """

    model = Exam
    form_class = ExamForm
    template_name = "exams/exam_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        exam = form.save(commit=False)
        exam.creator = self.request.user
        exam.save()

        log_audit(
            self.request.user,
            "EXAM_CREATED",
            f"Created examination '{exam.title}' in '{exam.category.name}'",
            request=self.request,
        )
        messages.success(
            self.request,
            f"Examination '{exam.title}' created! Now add questions from the Question Bank.",
        )
        return redirect("exams:exam_questions", pk=exam.pk)


class ExamUpdateView(TeacherRequiredMixin, UpdateView):
    """
    Updates examination parameters, duration, scoring, and settings.
    Guards historical integrity if student attempts already exist.
    """

    model = Exam
    form_class = ExamForm
    template_name = "exams/exam_form.html"
    context_object_name = "exam"

    def get_object(self, queryset=None):
        exam = super().get_object(queryset)
        user = self.request.user
        if not user.is_admin_role and exam.creator != user:
            if not user.assigned_categories.filter(pk=exam.category_id).exists():
                raise PermissionDenied("You do not have permission to edit this examination.")
        return exam

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        exam = self.get_object()
        if exam.has_attempts and not self.request.user.is_admin_role:
            messages.warning(
                self.request,
                "Notice: This examination has candidate attempts. Historical scores will remain intact.",
            )

        updated_exam = form.save()
        log_audit(
            self.request.user,
            "EXAM_UPDATED",
            f"Updated examination '{updated_exam.title}'",
            request=self.request,
        )
        messages.success(self.request, f"Examination '{updated_exam.title}' updated successfully.")
        return redirect("exams:exam_list")


class ExamDeleteView(TeacherRequiredMixin, View):
    """
    Deletes an examination. Strictly blocked if student candidate attempts exist.
    """

    def post(self, request, pk):
        exam = get_object_or_404(Exam, pk=pk)
        user = request.user
        if not user.is_admin_role and exam.creator != user:
            if not user.assigned_categories.filter(pk=exam.category_id).exists():
                raise PermissionDenied("You cannot delete this examination.")

        # Historical protection check
        if exam.has_attempts:
            messages.error(
                request,
                f"Cannot delete '{exam.title}': Candidate attempts exist for this examination. Historical testing records must be preserved.",
            )
            return redirect("exams:exam_list")

        title = exam.title
        exam.delete()
        log_audit(
            request.user,
            "EXAM_DELETED",
            f"Deleted examination '{title}'",
            request=request,
        )
        messages.success(request, f"Examination '{title}' has been deleted.")
        return redirect("exams:exam_list")


class ExamDuplicateView(TeacherRequiredMixin, View):
    """
    Duplicates an examination, copying its configuration and all assigned questions
    into a fresh Draft copy ("Copy of ...") without any attached candidate attempts.
    """

    def post(self, request, pk):
        source_exam = get_object_or_404(Exam, pk=pk)
        user = request.user
        if not user.is_admin_role and source_exam.creator != user:
            if not user.assigned_categories.filter(pk=source_exam.category_id).exists():
                raise PermissionDenied("You cannot duplicate this examination.")

        with transaction.atomic():
            new_title = f"Copy of {source_exam.title}"[:250]
            new_exam = Exam.objects.create(
                title=new_title,
                category=source_exam.category,
                creator=request.user,
                status=Exam.ExamStatus.DRAFT,
                description=source_exam.description,
                subject=source_exam.subject,
                topics=source_exam.topics,
                target_exam=source_exam.target_exam,
                is_free=source_exam.is_free,
                duration_minutes=source_exam.duration_minutes,
                total_marks=source_exam.total_marks,
                passing_percentage=source_exam.passing_percentage,
                default_positive_marks=source_exam.default_positive_marks,
                default_negative_marks=source_exam.default_negative_marks,
                shuffle_questions=source_exam.shuffle_questions,
                shuffle_options=source_exam.shuffle_options,
                max_attempts=source_exam.max_attempts,
                show_result_immediately=source_exam.show_result_immediately,
                allow_review_answers=source_exam.allow_review_answers,
                instructions=source_exam.instructions,
            )

            # Copy all ExamQuestion records
            source_questions = source_exam.exam_questions.all()
            for eq in source_questions:
                ExamQuestion.objects.create(
                    exam=new_exam,
                    question=eq.question,
                    order=eq.order,
                    section=eq.section,
                    positive_marks=eq.positive_marks,
                    negative_marks=eq.negative_marks,
                )

            new_exam.recalculate_marks()

        log_audit(
            request.user,
            "EXAM_DUPLICATED",
            f"Duplicated exam #{source_exam.pk} to new exam #{new_exam.pk} ('{new_exam.title}')",
            request=request,
        )
        messages.success(
            request,
            f"Successfully cloned '{source_exam.title}' into new Draft exam with {source_questions.count()} questions!",
        )
        return redirect("exams:exam_questions", pk=new_exam.pk)


class ExamTogglePublishView(TeacherRequiredMixin, View):
    """
    Validates and toggles examination status between Published and Draft.
    Requires at least 1 question and valid duration before publishing.
    """

    def post(self, request, pk):
        exam = get_object_or_404(Exam, pk=pk)
        user = request.user
        if not user.is_admin_role and exam.creator != user:
            if not user.assigned_categories.filter(pk=exam.category_id).exists():
                raise PermissionDenied("You cannot publish or unpublish this examination.")

        if exam.status in [Exam.ExamStatus.PUBLISHED, Exam.ExamStatus.SCHEDULED]:
            # Unpublish to draft
            exam.status = Exam.ExamStatus.DRAFT
            exam.save(update_fields=["status"])
            log_audit(
                request.user,
                "EXAM_UNPUBLISHED",
                f"Unpublished exam '{exam.title}' to Draft",
                request=request,
            )
            messages.info(request, f"Examination '{exam.title}' has been moved to Draft.")
        else:
            # Validate before publishing
            if exam.total_questions_count == 0:
                messages.error(
                    request,
                    f"Cannot publish '{exam.title}': Please add at least 1 question to the examination first.",
                )
                return redirect("exams:exam_questions", pk=exam.pk)

            if exam.duration_minutes == 0:
                messages.error(
                    request,
                    f"Cannot publish '{exam.title}': Examination duration must be greater than 0 minutes.",
                )
                return redirect("exams:exam_edit", pk=exam.pk)

            # Determine if Scheduled or Published
            now = timezone.now()
            if exam.start_time and exam.start_time > now:
                exam.status = Exam.ExamStatus.SCHEDULED
                state_label = "Scheduled (Upcoming)"
            else:
                exam.status = Exam.ExamStatus.PUBLISHED
                state_label = "Published"

            exam.recalculate_marks()
            exam.save(update_fields=["status"])

            log_audit(
                request.user,
                "EXAM_PUBLISHED",
                f"Published exam '{exam.title}' ({state_label})",
                request=request,
            )
            messages.success(request, f"Examination '{exam.title}' is now {state_label} and active!")

        return redirect("exams:exam_list")


class ExamQuestionManageView(TeacherRequiredMixin, DetailView):
    """
    Interactive Question Builder for an examination.
    Displays currently assigned questions with reordering & scoring,
    and provides Question Bank selection drawer and Random Question sampler.
    """

    model = Exam
    template_name = "exams/exam_questions.html"
    context_object_name = "exam"

    def get_object(self, queryset=None):
        exam = super().get_object(queryset)
        user = self.request.user
        if not user.is_admin_role and exam.creator != user:
            if not user.assigned_categories.filter(pk=exam.category_id).exists():
                raise PermissionDenied("You do not have permission to manage this exam's questions.")
        return exam

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        exam = self.get_object()
        user = self.request.user

        context["exam_questions"] = (
            exam.exam_questions.select_related("question")
            .prefetch_related("question__options")
            .order_by("order", "id")
        )

        assigned_q_ids = exam.exam_questions.values_list("question_id", flat=True)
        available_qs = Question.objects.filter(category=exam.category).exclude(id__in=assigned_q_ids)

        if not user.is_admin_role:
            assigned_cats = user.assigned_categories.all()
            if assigned_cats.exists():
                available_qs = available_qs.filter(Q(creator=user) | Q(category__in=assigned_cats))
            else:
                available_qs = available_qs.filter(creator=user)

        q_search = self.request.GET.get("q_search", "").strip()
        if q_search:
            available_qs = available_qs.filter(
                Q(question_text__icontains=q_search)
                | Q(subject__icontains=q_search)
                | Q(topic__icontains=q_search)
            )

        diff_filter = self.request.GET.get("diff", "").strip()
        if diff_filter:
            available_qs = available_qs.filter(difficulty=diff_filter)

        context["available_questions"] = available_qs.prefetch_related("options").order_by("-created_at")[:50]
        context["available_total_count"] = available_qs.count()
        context["q_search"] = q_search
        context["diff_filter"] = diff_filter
        context["random_form"] = ExamRandomQuestionForm(
            initial={
                "positive_marks": exam.default_positive_marks,
                "negative_marks": exam.default_negative_marks,
                "subject": exam.subject,
            }
        )
        return context


class ExamAddQuestionsBulkView(TeacherRequiredMixin, View):
    """
    Adds selected questions from the Question Bank into an examination.
    """

    def post(self, request, pk):
        exam = get_object_or_404(Exam, pk=pk)
        user = request.user
        if not user.is_admin_role and exam.creator != user:
            if not user.assigned_categories.filter(pk=exam.category_id).exists():
                raise PermissionDenied("You cannot add questions to this exam.")

        if exam.has_attempts:
            messages.error(
                request,
                "Cannot modify questions: Candidate attempts already exist for this examination.",
            )
            return redirect("exams:exam_questions", pk=exam.pk)

        selected_ids = request.POST.getlist("selected_questions")
        if not selected_ids:
            messages.warning(request, "No questions were selected to add.")
            return redirect("exams:exam_questions", pk=exam.pk)

        added_count = 0
        with transaction.atomic():
            from django.db.models import Max
            current_max_order = (
                exam.exam_questions.aggregate(Max("order"))["order__max"] or 0
            )

            for q_id in selected_ids:
                try:
                    q = Question.objects.get(pk=int(q_id), category=exam.category)
                    if not exam.exam_questions.filter(question=q).exists():
                        current_max_order += 1
                        ExamQuestion.objects.create(
                            exam=exam,
                            question=q,
                            order=current_max_order,
                            section=request.POST.get("section", "General") or "General",
                            positive_marks=q.positive_marks or exam.default_positive_marks,
                            negative_marks=q.negative_marks or exam.default_negative_marks,
                        )
                        added_count += 1
                except (ValueError, Question.DoesNotExist):
                    continue

            exam.recalculate_marks()

        log_audit(
            request.user,
            "EXAM_QUESTIONS_ADDED",
            f"Added {added_count} questions to examination '{exam.title}'",
            request=request,
        )
        messages.success(request, f"Successfully added {added_count} question(s) to '{exam.title}'!")
        return redirect("exams:exam_questions", pk=exam.pk)


class ExamAddRandomQuestionsView(TeacherRequiredMixin, View):
    """
    Selects N questions randomly from the Question Bank matching criteria
    and appends them to the examination.
    """

    def post(self, request, pk):
        exam = get_object_or_404(Exam, pk=pk)
        user = request.user
        if not user.is_admin_role and exam.creator != user:
            if not user.assigned_categories.filter(pk=exam.category_id).exists():
                raise PermissionDenied()

        if exam.has_attempts:
            messages.error(
                request,
                "Cannot modify questions: Candidate attempts already exist for this examination.",
            )
            return redirect("exams:exam_questions", pk=exam.pk)

        form = ExamRandomQuestionForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Please check the random selection criteria.")
            return redirect("exams:exam_questions", pk=exam.pk)

        count = form.cleaned_data["count"]
        subject = form.cleaned_data.get("subject", "").strip()
        topic = form.cleaned_data.get("topic", "").strip()
        difficulty = form.cleaned_data.get("difficulty", "").strip()
        section = form.cleaned_data.get("section", "General") or "General"
        pos_marks = form.cleaned_data.get("positive_marks", exam.default_positive_marks)
        neg_marks = form.cleaned_data.get("negative_marks", exam.default_negative_marks)

        assigned_q_ids = exam.exam_questions.values_list("question_id", flat=True)
        pool = Question.objects.filter(category=exam.category).exclude(id__in=assigned_q_ids)

        if subject:
            pool = pool.filter(subject__icontains=subject)
        if topic:
            pool = pool.filter(topic__icontains=topic)
        if difficulty:
            pool = pool.filter(difficulty=difficulty)

        available_count = pool.count()
        if available_count == 0:
            messages.warning(request, "No matching unassigned questions found in the Question Bank.")
            return redirect("exams:exam_questions", pk=exam.pk)

        sample_size = min(count, available_count)
        pool_ids = list(pool.values_list("id", flat=True))
        sampled_ids = random.sample(pool_ids, sample_size)

        added = 0
        with transaction.atomic():
            from django.db.models import Max
            current_max_order = (
                exam.exam_questions.aggregate(Max("order"))["order__max"] or 0
            )
            for q_id in sampled_ids:
                current_max_order += 1
                q = Question.objects.get(pk=q_id)
                ExamQuestion.objects.create(
                    exam=exam,
                    question=q,
                    order=current_max_order,
                    section=section,
                    positive_marks=pos_marks,
                    negative_marks=neg_marks,
                )
                added += 1

            exam.recalculate_marks()

        log_audit(
            request.user,
            "EXAM_RANDOM_QUESTIONS_ADDED",
            f"Randomly added {added} questions to exam '{exam.title}'",
            request=request,
        )
        messages.success(
            request,
            f"Successfully added {added} random question(s) matching your criteria to '{exam.title}'!",
        )
        return redirect("exams:exam_questions", pk=exam.pk)


class ExamRemoveQuestionView(TeacherRequiredMixin, View):
    """
    Removes a single question from an examination.
    """

    def post(self, request, pk, question_pk):
        exam = get_object_or_404(Exam, pk=pk)
        user = request.user
        if not user.is_admin_role and exam.creator != user:
            if not user.assigned_categories.filter(pk=exam.category_id).exists():
                raise PermissionDenied()

        if exam.has_attempts:
            messages.error(
                request,
                "Cannot remove question: Candidate examination attempts already exist.",
            )
            return redirect("exams:exam_questions", pk=exam.pk)

        eq = get_object_or_404(ExamQuestion, exam=exam, question_id=question_pk)
        eq.delete()
        exam.recalculate_marks()

        messages.info(request, "Question removed from this examination.")
        return redirect("exams:exam_questions", pk=exam.pk)


class ExamPreviewView(TeacherRequiredMixin, DetailView):
    """
    High-fidelity teacher and admin examination preview before publishing.
    Renders instructions, full questions with KaTeX equations, options,
    and verified answer keys.
    """

    model = Exam
    template_name = "exams/exam_preview.html"
    context_object_name = "exam"

    def get_object(self, queryset=None):
        exam = super().get_object(queryset)
        user = self.request.user
        if not user.is_admin_role and exam.creator != user:
            if not user.assigned_categories.filter(pk=exam.category_id).exists():
                raise PermissionDenied("You do not have permission to preview this examination.")
        return exam

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        exam = self.get_object()
        context["exam_questions"] = (
            exam.exam_questions.select_related("question")
            .prefetch_related("question__options")
            .order_by("order", "id")
        )
        context["total_questions"] = exam.total_questions_count
        context["calculated_marks"] = exam.calculated_total_marks
        return context
