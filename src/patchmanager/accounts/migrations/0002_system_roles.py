from typing import Any

from django.db import migrations

ROLES = [
    ("super_admin", "Super Admin", "Full access to the instance, including system settings.", True),
    (
        "tenant_owner",
        "Tenant Owner",
        "Everything inside a tenant, including its members and roles.",
        False,
    ),
    ("admin", "Admin", "Manage all documentation objects; no identity or system settings.", False),
    ("editor", "Editor", "Create, change and delete documentation objects.", False),
    ("viewer", "Viewer", "Read only access to documentation objects.", False),
    ("auditor", "Auditor", "Read only access including the audit log.", False),
]


def create(apps: Any, schema_editor: Any) -> None:
    role = apps.get_model("accounts", "Role")
    for key, name, description, requires_mfa in ROLES:
        role.objects.update_or_create(
            key=key,
            defaults={
                "name": name,
                "description": description,
                "is_system": True,
                "requires_mfa": requires_mfa,
            },
        )


def remove(apps: Any, schema_editor: Any) -> None:
    apps.get_model("accounts", "Role").objects.filter(is_system=True).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [migrations.RunPython(create, remove)]
