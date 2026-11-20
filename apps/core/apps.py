from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    verbose_name = "Core & Public Portal"

    def ready(self):
        from . import signals  # noqa: F401 -- connects the successful-sign-in receiver

