"""Small helpers shared by views."""

from typing import Any
from uuid import UUID


def parse_uuid(value: object) -> UUID | None:
    """Parse form or query input; invalid input means "nothing selected"."""
    try:
        return UUID(str(value))
    except ValueError:
        return None


def label_choices(field: Any) -> None:
    """Offer the rows of a lookup list (categories, room types) sorted by their shown name."""
    rows = field.queryset.model.by_label(field.queryset)
    empty = [("", field.empty_label)] if field.empty_label is not None else []
    field.choices = [*empty, *((row.key, row.label) for row in rows)]


def key_fields(fields: dict[str, Any], *names: str) -> dict[str, Any]:
    """Let ``names`` (foreign keys to lookup lists) be given by key text as well as by row."""
    return {(f"{k}_id" if k in names and isinstance(v, str) else k): v for k, v in fields.items()}
