from django.contrib import admin
from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "description", "ip_address")
    list_filter = ("action", "created_at")
    search_fields = ("action", "description", "user__username", "ip_address")
    readonly_fields = ("created_at", "user", "action", "description", "ip_address")

