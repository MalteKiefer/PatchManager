from django.apps import AppConfig


class TenancyConfig(AppConfig):
    name = "patchmanager.tenancy"
    label = "tenancy"

    def ready(self) -> None:
        from django.db.backends.signals import connection_created

        from patchmanager.accounts import permissions

        permissions.register("tenancy.tenant", "Tenant", extra_actions=("archive",))

        def reset_context(connection: object, **kwargs: object) -> None:
            """A fresh connection never inherits tenant visibility."""
            with connection.cursor() as cursor:  # type: ignore[attr-defined]
                cursor.execute(
                    "SELECT set_config('patchmanager.tenant_ids', '', false),"
                    " set_config('patchmanager.all_tenants', 'off', false)"
                )

        connection_created.connect(reset_context, dispatch_uid="tenancy-reset-context", weak=False)
