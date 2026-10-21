"""PostgreSQL Row Level Security: policies and per request tenant context.

Policies read two settings:

* ``patchmanager.tenant_ids``: comma separated tenant UUIDs the session may see.
* ``patchmanager.all_tenants``: ``on`` for system work and global admins.

Both default to empty, so a connection without context sees no tenant rows.
Tables use ``FORCE ROW LEVEL SECURITY`` so the policies also bind the table
owner. Only superusers and ``BYPASSRLS`` roles escape them, and the
application never connects as either.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from django.db import connection
from django.db.migrations.operations.base import Operation

_VISIBLE = (
    "(current_setting('patchmanager.all_tenants', true) = 'on'"
    " OR {column}::text = ANY (string_to_array("
    "nullif(current_setting('patchmanager.tenant_ids', true), ''), ',')))"
)


def policy_sql(table: str, column: str = "tenant_id") -> list[str]:
    """Return the statements that enable and force RLS on ``table``."""
    visible = _VISIBLE.format(column=column)
    return [
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY',
        f'CREATE POLICY tenant_isolation ON "{table}" USING {visible} WITH CHECK {visible}',
    ]


class EnableRowLevelSecurity(Operation):
    """Migration operation: isolate ``model_name``'s table by tenant column."""

    reduces_to_sql = True
    reversible = True

    def __init__(self, model_name: str, column: str = "tenant_id") -> None:
        """Remember the model and the tenant column."""
        self.model_name = model_name
        self.column = column

    def state_forwards(self, app_label: str, state: Any) -> None:
        """No model state change."""

    def database_forwards(
        self, app_label: str, schema_editor: Any, from_state: Any, to_state: Any
    ) -> None:
        """Create the policy."""
        table = to_state.apps.get_model(app_label, self.model_name)._meta.db_table
        for sql in policy_sql(table, self.column):
            schema_editor.execute(sql)

    def database_backwards(
        self, app_label: str, schema_editor: Any, from_state: Any, to_state: Any
    ) -> None:
        """Drop the policy and disable RLS."""
        table = from_state.apps.get_model(app_label, self.model_name)._meta.db_table
        schema_editor.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        schema_editor.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        schema_editor.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

    def describe(self) -> str:
        """Human readable summary for ``migrate --plan``."""
        return f"Enable row level security on {self.model_name}"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """Serialise for migration files."""
        return self.__class__.__qualname__, [self.model_name], {"column": self.column}


def set_context(tenant_ids: Iterable[UUID | str], *, all_tenants: bool = False) -> None:
    """Set the session's tenant visibility. Overwrites any previous value."""
    ids = ",".join(str(tid) for tid in tenant_ids)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('patchmanager.tenant_ids', %s, false),"
            " set_config('patchmanager.all_tenants', %s, false)",
            [ids, "on" if all_tenants else "off"],
        )


def clear_context() -> None:
    """Remove all tenant visibility from the session."""
    set_context([])


def _current() -> tuple[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT coalesce(current_setting('patchmanager.tenant_ids', true), ''),"
            " coalesce(current_setting('patchmanager.all_tenants', true), 'off')"
        )
        ids, all_tenants = cursor.fetchone()
    return ids, all_tenants


@contextmanager
def tenant_context(
    tenant_ids: Iterable[UUID | str], *, all_tenants: bool = False
) -> Iterator[None]:
    """Run a block with the given visibility, then restore the previous one."""
    previous_ids, previous_all = _current()
    set_context(tenant_ids, all_tenants=all_tenants)
    try:
        yield
    finally:
        set_context([i for i in previous_ids.split(",") if i], all_tenants=previous_all == "on")


def system_context() -> Any:
    """Visibility of all tenants, for migrations, jobs and global admins."""
    return tenant_context([], all_tenants=True)
