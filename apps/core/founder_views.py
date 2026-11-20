"""Private, read-only founder workspace. No file/answer/credit mutation paths."""
from datetime import timedelta

from django.core.paginator import Paginator
from django.db import OperationalError, connection
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from apps.accounts.models import User
from apps.exams.models import ClassroomActivation, ClassroomSession, ExamAttempt, ExamSecurityLog, TestSeries
from apps.exams.series import series_keys_confirmed, slot_ready
from apps.payments.models import (Checkout, CreditGrant, CreditUse, PackCheckout, PackPayment,
                                  PackRefund, Payment, Refund)
from apps.payments.packs import PRODUCTS
from apps.questions.models import QuestionPaperUpload
from .founder_metrics import CAPTURED, finance_summary, overview_data
from .founder_security import (FounderConfigurationError, FounderRequiredMixin, SESSION_KEY,
                               founder_audit, verify_step_up)
from .models import ActivityTrackingState, AuditLog, LoginEvent


def page(queryset, request, parameter, size=20):
    return Paginator(queryset, size).get_page(request.GET.get(parameter, 1))


def founder_context(section, **values):
    return {"section": section, "now": timezone.now(), **values}


class FounderVerifyView(FounderRequiredMixin, View):
    require_step_up = False

    def get(self, request):
        if self.founder_unlocked:
            return redirect("founder:overview")
        locked = self.founder_access.locked_until
        return render(request, "founder/verify.html", founder_context(
            "verify", locked_until=locked if locked and locked > timezone.now() else None))

    def post(self, request):
        if self.founder_unlocked:
            return redirect("founder:overview")
        try:
            ok, retry = verify_step_up(request, request.POST.get("password", ""), request.POST.get("code", ""))
        except FounderConfigurationError:
            return HttpResponse("Founder authentication is unavailable. Check the server configuration.", status=503)
        except OperationalError as exc:
            if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                raise
            response = HttpResponse("Verification is busy. Please try again shortly.", status=503)
            response["Retry-After"] = "1"
            return response
        if ok:
            return redirect("founder:overview")
        response = render(request, "founder/verify.html", founder_context(
            "verify", error="Verification was not accepted. Check your password and use a fresh authenticator code.",
            retry=retry), status=429 if retry else 200)
        if retry:
            response["Retry-After"] = str(retry)
        return response


class FounderLockView(FounderRequiredMixin, View):
    require_step_up = False

    def post(self, request):
        if request.session.pop(SESSION_KEY, None):
            founder_audit(request.user, "FOUNDER_LOCKED", "Founder workspace was locked in this session.")
        return redirect("founder:verify")


class FounderOverviewView(FounderRequiredMixin, View):
    def get(self, request):
        founder_audit(request.user, "FOUNDER_OVERVIEW_VIEWED", "Opened the founder overview.")
        return render(request, "founder/overview.html", founder_context("overview", **overview_data()))


class FounderAccountsView(FounderRequiredMixin, View):
    def get(self, request):
        founder_audit(request.user, "FOUNDER_ACCOUNTS_VIEWED", "Opened the account directory.")
        role = request.GET.get("role", "")
        if role not in ("", *User.Roles.values):
            role = ""
        users = User.objects.order_by("-date_joined", "-pk")
        if role:
            users = users.filter(role=role)
        return render(request, "founder/accounts.html", founder_context(
            "accounts", users=page(users, request, "page", 25), role=role, roles=User.Roles.choices,
            search_mode=False))

    def post(self, request):
        # Search terms are not put into the URL or the audit trail.
        query = request.POST.get("q", "").strip()
        founder_audit(request.user, "FOUNDER_ACCOUNT_SEARCH", "Searched the account directory; search terms are not recorded.")
        if not 2 <= len(query) <= 120:
            return render(request, "founder/accounts.html", founder_context(
                "accounts", roles=User.Roles.choices, search_mode=True, search_error="Enter 2–120 characters to search.", results=[]))
        condition = (Q(username__icontains=query) | Q(email__icontains=query)
                     | Q(first_name__icontains=query) | Q(last_name__icontains=query))
        if query.isascii() and query.isdecimal() and len(query) <= 18:
            condition |= Q(pk=int(query))
        results = list(User.objects.filter(condition).order_by("-date_joined", "-pk")[:51])
        return render(request, "founder/accounts.html", founder_context(
            "accounts", roles=User.Roles.choices, search_mode=True, results=results[:50], more_results=len(results) > 50))


def attempt_kind(attempt):
    if attempt.classroom_session_id:
        return "Classroom"
    if attempt.exam.is_private_practice:
        return "Private practice"
    if getattr(attempt.exam, "series_slot", None):
        return "Test series"
    return "Other / institutional"


class FounderAccountDetailView(FounderRequiredMixin, View):
    def get(self, request, pk):
        account = get_object_or_404(User, pk=pk)
        founder_audit(request.user, "FOUNDER_ACCOUNT_VIEWED", f"Opened support metadata for account #{account.pk}.")
        attempts = page(ExamAttempt.objects.filter(student=account).select_related(
            "exam", "exam__series_slot").order_by("-started_at", "-pk"), request, "attempt_page")
        for attempt in attempts:
            attempt.founder_kind = attempt_kind(attempt)
        grants = page(CreditGrant.objects.filter(user=account).order_by("-created_at", "-pk"), request, "grant_page")
        uses = page(CreditUse.objects.filter(user=account).select_related("grant").order_by("-created_at", "-pk"),
                    request, "use_page")
        logins = page(LoginEvent.objects.filter(user=account).order_by("-occurred_at", "-pk"), request, "login_page")
        papers = page(QuestionPaperUpload.objects.filter(uploaded_by=account).order_by("-created_at", "-pk"),
                      request, "paper_page")
        activations = page(ClassroomActivation.objects.filter(teacher=account).select_related("credit_use", "credit_use__grant")
                           .annotate(session_count=Count("sessions", distinct=True)).order_by("-starts_at", "-pk"),
                           request, "activation_page")
        classrooms = page(ClassroomSession.objects.filter(teacher=account)
                          .annotate(participant_count=Count("participants", distinct=True),
                                    attempt_count=Count("attempts", distinct=True))
                          .order_by("-created_at", "-pk"), request, "classroom_page")
        return render(request, "founder/account_detail.html", founder_context(
            "accounts", account=account, attempts=attempts, grants=grants, uses=uses, logins=logins,
            papers=papers, activations=activations, classrooms=classrooms,
            money=finance_summary(account), tracking=ActivityTrackingState.objects.filter(pk="sign_in").first()))


class FounderPaymentsView(FounderRequiredMixin, View):
    def get(self, request):
        value = request.GET.get("account", "")
        account = None
        if value:
            if not value.isascii() or not value.isdecimal() or len(value) > 18:
                raise Http404()
            account = get_object_or_404(User, pk=int(value))
        description = f"Opened payment and credit records for account #{account.pk}." if account else "Opened the payment and credit ledger."
        founder_audit(request.user, "FOUNDER_FINANCE_VIEWED", description)
        pack_checkouts = PackCheckout.objects.select_related("user").order_by("-created_at", "-pk")
        legacy_checkouts = Checkout.objects.select_related("user", "plan").order_by("-created_at", "-pk")
        packs = PackPayment.objects.select_related("checkout", "checkout__user").order_by("-created_at", "-pk")
        legacy = Payment.objects.select_related("checkout", "checkout__user", "checkout__plan").order_by("-captured_at", "-pk")
        pack_refunds = PackRefund.objects.select_related("payment", "payment__checkout", "payment__checkout__user").order_by("-processed_at", "-pk")
        legacy_refunds = Refund.objects.select_related("payment", "payment__checkout", "payment__checkout__user").order_by("-processed_at", "-pk")
        grants = CreditGrant.objects.select_related("user").order_by("-created_at", "-pk")
        uses = CreditUse.objects.select_related("user", "grant").order_by("-created_at", "-pk")
        if account:
            pack_checkouts = pack_checkouts.filter(user=account)
            legacy_checkouts = legacy_checkouts.filter(user=account)
            packs = packs.filter(checkout__user=account)
            legacy = legacy.filter(checkout__user=account)
            pack_refunds = pack_refunds.filter(payment__checkout__user=account)
            legacy_refunds = legacy_refunds.filter(payment__checkout__user=account)
            grants = grants.filter(user=account)
            uses = uses.filter(user=account)
        return render(request, "founder/payments.html", founder_context(
            "payments", account=account, money=finance_summary(account),
            pack_checkouts=page(pack_checkouts, request, "pack_checkout_page"),
            legacy_checkouts=page(legacy_checkouts, request, "legacy_checkout_page"),
            packs=page(packs, request, "pack_page"), legacy=page(legacy, request, "legacy_page"),
            pack_refunds=page(pack_refunds, request, "pack_refund_page"),
            legacy_refunds=page(legacy_refunds, request, "legacy_refund_page"),
            grants=page(grants, request, "grant_page"), uses=page(uses, request, "use_page"),
            products=PRODUCTS.values()))


class FounderSeriesView(FounderRequiredMixin, View):
    def get(self, request):
        founder_audit(request.user, "FOUNDER_SERIES_VIEWED", "Opened test series readiness and participation.")
        groups = []
        for series in TestSeries.objects.order_by("track"):
            slots = list(series.slots.select_related("series", "exam").order_by("position"))
            rows = []
            for slot in slots:
                # The status check reads keys privately; it never renders them.
                content_ready = slot_ready(slot) if slot.exam_id else False
                keys_ready = bool(content_ready and series_keys_confirmed(slot.exam))
                rows.append({"slot": slot, "content_ready": content_ready, "keys_ready": keys_ready,
                             "ready": content_ready and keys_ready,
                             "safe_title": (slot.exam.title if slot.exam_id and not slot.exam.is_private_practice
                                            and not slot.exam.is_classroom_only else "")})
            positions = [slot.position for slot in slots]
            attempts = ExamAttempt.objects.filter(exam__series_slot__series=series, classroom_session__isnull=True)
            purchases = PackPayment.objects.filter(status__in=CAPTURED, checkout__product=f"series_{series.track}")
            groups.append({"series": series, "rows": rows, "assigned": sum(bool(s.exam_id) for s in slots),
                           "ready": sum(row["ready"] for row in rows),
                           "on_sale": len(slots) == 16 and positions == list(range(1, 17)) and all(r["ready"] for r in rows),
                           "attempts": attempts.count(), "students": attempts.values("student_id").distinct().count(),
                           "buyers": purchases.values("checkout__user_id").distinct().count(),
                           "product": PRODUCTS.get(f"series_{series.track}")})
        return render(request, "founder/series.html", founder_context("series", groups=groups))


class FounderActivityView(FounderRequiredMixin, View):
    def get(self, request):
        founder_audit(request.user, "FOUNDER_ACTIVITY_VIEWED", "Opened the recorded activity and audit history.")
        action = request.GET.get("action", "")[:80]
        audit = AuditLog.objects.select_related("user").order_by("-created_at", "-pk")
        if action:
            audit = audit.filter(action=action)
        security = ExamSecurityLog.objects.select_related("attempt", "attempt__student").order_by("-timestamp", "-pk")
        logins = LoginEvent.objects.select_related("user").order_by("-occurred_at", "-pk")
        return render(request, "founder/activity.html", founder_context(
            "activity", audit=page(audit, request, "audit_page", 25),
            security=page(security, request, "security_page"), logins=page(logins, request, "login_page"),
            actions=AuditLog.objects.order_by("action").values_list("action", flat=True).distinct(), action=action,
            tracking=ActivityTrackingState.objects.filter(pk="sign_in").first(),
            recent_security_count=ExamSecurityLog.objects.filter(
                timestamp__gte=timezone.now() - timedelta(days=7), timestamp__lte=timezone.now()).count()))
