from django.db.models import Q
from django.views.generic import ListView
from apps.core.models import AuditLog
from apps.core.permissions import SuperAdminRequiredMixin


class AuditLogListView(SuperAdminRequiredMixin, ListView):
    """
    Super Admin view to review platform-wide audit and governance activity.
    """

    model = AuditLog
    template_name = "core/audit_log_list.html"
    context_object_name = "logs"
    paginate_by = 20

    def get_queryset(self):
        qs = AuditLog.objects.select_related("user").order_by("-created_at")
        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(action__icontains=query)
                | Q(description__icontains=query)
                | Q(user__username__icontains=query)
                | Q(ip_address__icontains=query)
            )

        action_filter = self.request.GET.get("action", "").strip()
        if action_filter:
            qs = qs.filter(action=action_filter)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_query"] = self.request.GET.get("q", "")
        context["action_filter"] = self.request.GET.get("action", "")
        context["total_logs"] = AuditLog.objects.count()
        context["distinct_actions"] = (
            AuditLog.objects.values_list("action", flat=True).distinct().order_by("action")
        )
        return context

