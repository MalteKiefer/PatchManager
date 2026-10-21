from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "patchmanager.audit"
    label = "audit"

    def ready(self) -> None:
        from patchmanager.accounts import permissions

        permissions.register("audit.event", "Audit event", actions=("view", "export", "verify"))
