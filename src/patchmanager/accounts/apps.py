from django.apps import AppConfig
from django.db.models.signals import m2m_changed, post_delete, post_save


class AccountsConfig(AppConfig):
    name = "patchmanager.accounts"
    label = "accounts"

    def ready(self) -> None:
        from patchmanager.accounts import permissions, policy
        from patchmanager.accounts.models import Assignment, Group, Role

        permissions.register(
            "accounts.user",
            "User",
            extra_actions=("lock", "reset_mfa", "revoke_sessions", "impersonate"),
        )
        permissions.register("accounts.group", "Group")
        permissions.register("accounts.role", "Role")
        permissions.register(
            "accounts.assignment", "Role assignment", actions=("view", "add", "delete")
        )
        permissions.register("system.settings", "System settings", actions=("view", "change"))

        def invalidate(**kwargs: object) -> None:
            policy.bump_version()

        for model in (Assignment, Role, Group):
            post_save.connect(
                invalidate, sender=model, dispatch_uid=f"policy-save-{model.__name__}", weak=False
            )
            post_delete.connect(
                invalidate, sender=model, dispatch_uid=f"policy-delete-{model.__name__}", weak=False
            )
        for through in (Group.members.through, Group.parents.through):
            m2m_changed.connect(
                invalidate,
                sender=through,
                dispatch_uid=f"policy-m2m-{through.__name__}",
                weak=False,
            )
