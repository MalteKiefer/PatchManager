"""PostgreSQL ``ltree`` support for hierarchical permission scopes."""

from typing import Any

from django.db import models
from django.db.models import Lookup


class LtreeField(models.TextField):  # type: ignore[type-arg]
    """Dotted label path, for example ``<tenant hex>.<site hex>``."""

    description = "PostgreSQL ltree path"

    def db_type(self, connection: Any) -> str:
        """Return the native column type."""
        return "ltree"


@LtreeField.register_lookup
class DescendantOf(Lookup):
    """``path <@ value``: path equals value or lies below it."""

    lookup_name = "descendant_of"

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        """Render ``lhs <@ rhs::ltree``."""
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        return f"{lhs} <@ {rhs}::ltree", (*lhs_params, *rhs_params)


def path_label(pk: Any) -> str:
    """Return the ltree label for a UUID primary key (hex, no dashes)."""
    return str(pk).replace("-", "")
