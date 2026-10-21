"""Small helpers shared by views."""

from uuid import UUID


def parse_uuid(value: object) -> UUID | None:
    """Parse form or query input; invalid input means "nothing selected"."""
    try:
        return UUID(str(value))
    except ValueError:
        return None
