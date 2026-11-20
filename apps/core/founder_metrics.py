"""Read-only founder aggregates, using recorded facts and explicit labels."""
from datetime import datetime, time, timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.accounts.models import User
from apps.exams.models import ExamAttempt, TestSeries
from apps.payments.models import Checkout, PackCheckout, PackPayment, PackRefund, Payment, Refund
from apps.questions.models import QuestionPaperUpload
from .models import ActivityTrackingState, AuditLog, FounderAccess, LoginEvent

CAPTURED = (Payment.Status.CAPTURED, Payment.Status.PARTIAL_REFUND, Payment.Status.REFUNDED)
REFUND_STATES = (Payment.Status.PARTIAL_REFUND, Payment.Status.REFUNDED)


def finance_summary(user=None):
    """Recorded INR totals, not verified live revenue or a per-row mode claim.

    Implemented gateways are test-only, but these historic tables have no
    per-payment mode field. Status can record a provider-observed refund before
    its individual refund arrives; only recorded refund rows are summed.
    """
    packs = PackPayment.objects.filter(status__in=CAPTURED)
    legacy = Payment.objects.filter(status__in=CAPTURED)
    pack_refunds = PackRefund.objects.all()
    legacy_refunds = Refund.objects.all()
    pack_checkouts = PackCheckout.objects.all()
    old_checkouts = Checkout.objects.all()
    if user is not None:
        packs = packs.filter(checkout__user=user)
        legacy = legacy.filter(checkout__user=user)
        pack_refunds = pack_refunds.filter(payment__checkout__user=user)
        legacy_refunds = legacy_refunds.filter(payment__checkout__user=user)
        pack_checkouts = pack_checkouts.filter(user=user)
        old_checkouts = old_checkouts.filter(user=user)
    pack_buyers = packs.order_by().values_list("checkout__user_id", flat=True).distinct()
    legacy_buyers = legacy.order_by().values_list("checkout__user_id", flat=True).distinct()
    buyer_count = pack_buyers.union(legacy_buyers).count()
    gross = (packs.filter(currency="INR").aggregate(total=Sum("amount_paise"))["total"] or 0)
    gross += (legacy.filter(currency="INR").aggregate(total=Sum("amount_paise"))["total"] or 0)
    refunded = (pack_refunds.filter(payment__currency="INR").aggregate(total=Sum("amount_paise"))["total"] or 0)
    refunded += (legacy_refunds.filter(payment__currency="INR").aggregate(total=Sum("amount_paise"))["total"] or 0)
    # This flag identifies only one known discrepancy, not all unreconciled
    # partial refunds; the provider is the source for final reconciliation.
    missing_refund_rows = packs.filter(status__in=REFUND_STATES, refunds__isnull=True).count()
    missing_refund_rows += legacy.filter(status__in=REFUND_STATES, refunds__isnull=True).count()
    return {
        "buyers": buyer_count,
        "captured_count": packs.count() + legacy.count(),
        "gross_paise": gross,
        "refund_paise": refunded,
        "refund_count": pack_refunds.count() + legacy_refunds.count(),
        "pending_checkouts": pack_checkouts.filter(status__in=("initiating", "created")).count()
                             + old_checkouts.filter(status__in=("initiating", "created")).count(),
        "uncertain_checkouts": pack_checkouts.filter(status="initiating").count()
                               + old_checkouts.filter(status="initiating").count(),
        "missing_refund_rows": missing_refund_rows,
    }


def activity_chart(today=None, *, now=None, tracking=None):
    """Recorded daily values and coverage; do not turn unknown history into zero."""
    now = now or timezone.now()
    today = today or timezone.localdate(now)
    first = today - timedelta(days=13)
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(first, time.min), tz)
    end = timezone.make_aware(datetime.combine(today + timedelta(days=1), time.min), tz)
    registrations = {
        item["day"]: item["total"] for item in
        User.objects.filter(date_joined__gte=start, date_joined__lt=end, date_joined__lte=now)
        .order_by().annotate(day=TruncDate("date_joined")).values("day").annotate(total=Count("id"))
    }
    sign_ins = {
        item["day"]: item["total"] for item in
        LoginEvent.objects.filter(occurred_at__gte=start, occurred_at__lt=end,
                                  occurred_at__lte=now, user__isnull=False).order_by()
        .annotate(day=TruncDate("occurred_at")).values("day")
        .annotate(total=Count("user_id", distinct=True))
    }
    if tracking is None:
        tracking = ActivityTrackingState.objects.filter(pk="sign_in").first()
    tracking_day = timezone.localtime(tracking.started_at, tz).date() if tracking else None
    values = [(first + timedelta(days=i), registrations.get(first + timedelta(days=i), 0),
               sign_ins.get(first + timedelta(days=i), 0)) for i in range(14)]
    maximum = max([1] + [number for _, reg, login in values for number in (reg, login)])
    rows = []
    for i, (day, registrations_count, login_count) in enumerate(values):
        if tracking_day is None:
            coverage = "unknown"
        elif day < tracking_day:
            coverage = "before"
        elif day == tracking_day:
            coverage = "partial"
        else:
            coverage = "tracked"
        # A restored/imported event can predate the marker; show the actual
        # recorded value but never imply that earlier coverage was complete.
        sign_in_available = coverage in ("partial", "tracked") or login_count > 0
        reg_h = round(88 * registrations_count / maximum)
        login_h = round(88 * login_count / maximum)
        rows.append({"day": day, "registrations": registrations_count, "sign_ins": login_count,
                     "sign_in_coverage": coverage, "sign_in_available": sign_in_available,
                     "x_reg": i * 49 + 5, "x_sign_in": i * 49 + 22,
                     "label_x": i * 49 + 20, "reg_height": reg_h, "reg_y": 98 - reg_h,
                     "sign_in_height": login_h, "sign_in_y": 98 - login_h})
    return rows


def overview_data():
    now = timezone.now()
    since = now - timedelta(days=7)
    people = User.objects.aggregate(
        students=Count("pk", filter=Q(role=User.Roles.STUDENT)),
        teachers=Count("pk", filter=Q(role=User.Roles.TEACHER)),
        students_enabled=Count("pk", filter=Q(role=User.Roles.STUDENT, is_active=True)),
        teachers_enabled=Count("pk", filter=Q(role=User.Roles.TEACHER, is_active=True)),
        jee=Count("pk", filter=Q(role=User.Roles.STUDENT, exam_track="jee")),
        neet=Count("pk", filter=Q(role=User.Roles.STUDENT, exam_track="neet")),
        unclassified=Count("pk", filter=Q(role=User.Roles.STUDENT) & ~Q(exam_track__in=("jee", "neet"))),
    )
    student_count = people["students"] or 0
    for key in ("jee", "neet", "unclassified"):
        people[key + "_width"] = round(100 * people[key] / student_count, 1) if student_count else 0
    # An event whose account was deleted is still a recorded event, but cannot
    # contribute to a distinct-person count because its identity is unknown.
    recent = LoginEvent.objects.filter(occurred_at__gte=since, occurred_at__lte=now)
    sign_ins = recent.aggregate(
        unique=Count("user_id", distinct=True), events=Count("pk"),
        students=Count("user_id", distinct=True, filter=Q(role_snapshot=User.Roles.STUDENT)),
        teachers=Count("user_id", distinct=True, filter=Q(role_snapshot=User.Roles.TEACHER)),
    )
    attempts = ExamAttempt.objects.aggregate(
        total=Count("pk"),
        in_progress=Count("pk", filter=Q(status=ExamAttempt.AttemptStatus.IN_PROGRESS)),
        private=Count("pk", filter=Q(classroom_session__isnull=True, exam__is_private_practice=True)),
        classroom=Count("pk", filter=Q(classroom_session__isnull=False)),
        series=Count("pk", filter=Q(classroom_session__isnull=True, exam__is_private_practice=False,
                                    exam__series_slot__isnull=False)),
        institutional=Count("pk", filter=Q(classroom_session__isnull=True, exam__is_private_practice=False,
                                           exam__series_slot__isnull=True)),
    )
    money = finance_summary()
    alerts = {
        "failed_papers": QuestionPaperUpload.objects.filter(status=QuestionPaperUpload.ProcessingStatus.FAILED).count(),
        "uncertain_checkouts": money["uncertain_checkouts"],
        "missing_refund_rows": money["missing_refund_rows"],
        "locked_founders": FounderAccess.objects.filter(is_enabled=True, locked_until__gt=now).count(),
        "unclassified": people["unclassified"],
        "security_review": ExamAttempt.objects.filter(status=ExamAttempt.AttemptStatus.TERMINATED,
                                                        submitted_at__gte=since, submitted_at__lte=now).count(),
    }
    alerts["total"] = sum(alerts.values())
    series = list(TestSeries.objects.order_by("track").annotate(
        assigned=Count("slots", filter=Q(slots__exam__isnull=False)), total_slots=Count("slots")))
    tracking = ActivityTrackingState.objects.filter(pk="sign_in").first()
    return {
        "now": now, "people": people, "sign_ins": sign_ins, "attempts": attempts,
        "money": money, "alerts": alerts, "chart": activity_chart(now=now, tracking=tracking),
        "series_summary": series, "tracking": tracking,
        "recent_audit": AuditLog.objects.select_related("user").order_by("-created_at", "-pk")[:5],
    }
