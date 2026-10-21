"""Run an import: every row through the services, all or nothing, with a dry run."""

from dataclasses import dataclass
from typing import Any

from django.db import DatabaseError, transaction
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.audit import services as audit
from patchmanager.exchange.tables import TABLES, Table
from patchmanager.tenancy.models import Tenant


@dataclass
class Result:
    """Outcome of one row (line numbers count the header as line 1)."""

    line: int
    outcome: str  # created, updated, unchanged or error
    message: str = ""


def check_columns(table: Table, rows: list[dict[str, str]]) -> None:
    """The file must have the required columns and no unknown ones."""
    if table.handler is None:
        raise ServiceError(_("This table cannot be imported."))
    if not rows:
        raise ServiceError(_("The file has no rows."))
    header = set(rows[0])
    missing = [c for c in table.required if c not in header]
    unknown = sorted(header - set(table.columns))
    if missing:
        raise ServiceError(_("Missing columns: %(cols)s.") % {"cols": ", ".join(missing)})
    if unknown:
        raise ServiceError(_("Unknown columns: %(cols)s.") % {"cols": ", ".join(unknown)})


def run(
    request: HttpRequest,
    actor: User,
    tenant: Tenant,
    kind: str,
    rows: list[dict[str, str]],
    *,
    apply: bool,
) -> list[Result]:
    """Process all rows; keep the changes only if ``apply`` and no row failed."""
    table = TABLES[kind]
    check_columns(table, rows)
    assert table.handler is not None  # noqa: S101  # nosec B101: checked above
    results = []
    with transaction.atomic(), audit.deferred() as events:
        for line, row in enumerate(rows, start=2):
            missing = [c for c in table.required if not row.get(c)]
            if missing:
                results.append(
                    Result(line, "error", _("Empty: %(cols)s.") % {"cols": ", ".join(missing)})
                )
                continue
            try:
                with transaction.atomic():
                    results.append(Result(line, table.handler(request, actor, tenant, row)))
            except PermissionDeniedError:
                results.append(Result(line, "error", _("You do not have permission to do that.")))
            except ServiceError as exc:
                results.append(Result(line, "error", str(exc)))
            except DatabaseError:  # a database rule refused the row
                results.append(Result(line, "error", _("The database refused this row.")))
        failed = any(r.outcome == "error" for r in results)
        if not apply or failed:
            transaction.set_rollback(True)
    if apply and not failed:
        audit.flush(events)  # per row events, written once the batch succeeded
    if apply and not failed:
        audit.record(
            "exchange.imported",
            request=request,
            tenant_id=tenant.pk,
            data=summary(results) | {"kind": kind},
        )
    return results


def summary(results: list[Result]) -> dict[str, Any]:
    """Counts per outcome."""
    counts: dict[str, Any] = {}
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    return counts
