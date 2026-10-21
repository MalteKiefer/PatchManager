"""Attribute conditions on assignments (ABAC lite).

A condition list is a JSON array of clauses that must all hold::

    [{"attr": "status", "op": "not_in", "value": ["approved"]},
     {"attr": "created_by", "op": "is_actor"}]

Each clause can be evaluated against an object and translated into a
``Q`` filter for list queries, so single object checks and scoped querysets
always agree.
"""

from collections.abc import Callable
from typing import Any

from django.db.models import Q

from patchmanager.accounts.permissions import get_object_type

OPERATORS = frozenset({"eq", "ne", "in", "not_in", "contains", "is_actor"})


class InvalidConditionError(ValueError):
    """A condition list does not match the schema."""


def validate(conditions: Any, object_type: str | None) -> list[dict[str, Any]]:
    """Return ``conditions`` if well formed for ``object_type``, else raise."""
    if not isinstance(conditions, list):
        raise InvalidConditionError("conditions must be a list")
    allowed = get_object_type(object_type).condition_attrs if object_type else frozenset()
    for clause in conditions:
        if not isinstance(clause, dict) or set(clause) - {"attr", "op", "value"}:
            raise InvalidConditionError(f"malformed clause: {clause!r}")
        if clause.get("op") not in OPERATORS:
            raise InvalidConditionError(f"unknown operator: {clause.get('op')!r}")
        if clause.get("attr") not in allowed:
            raise InvalidConditionError(f"attribute not allowed: {clause.get('attr')!r}")
        needs_list = clause["op"] in {"in", "not_in"}
        if needs_list and not isinstance(clause.get("value"), list):
            raise InvalidConditionError("in/not_in need a list value")
        if clause["op"] != "is_actor" and "value" not in clause:
            raise InvalidConditionError("value missing")
    return conditions


_CHECKS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda actual, expected: actual == expected,
    "ne": lambda actual, expected: actual != expected,
    "in": lambda actual, expected: actual in expected,
    "not_in": lambda actual, expected: actual not in expected,
    "contains": lambda actual, expected: expected in (actual or ()),
}


def matches(conditions: list[dict[str, Any]], obj: Any, actor: Any) -> bool:
    """True if ``obj`` satisfies every clause."""
    for clause in conditions:
        if clause["op"] == "is_actor":
            ok = getattr(obj, f"{clause['attr']}_id", None) == actor.pk
        else:
            ok = _CHECKS[clause["op"]](getattr(obj, clause["attr"], None), clause["value"])
        if not ok:
            return False
    return True


def to_q(conditions: list[dict[str, Any]], actor: Any) -> Q:
    """Translate clauses into a queryset filter."""
    q = Q()
    for clause in conditions:
        attr, op, expected = clause["attr"], clause["op"], clause.get("value")
        if op == "is_actor":
            q &= Q(**{f"{attr}_id": actor.pk})
        elif op == "eq":
            q &= Q(**{attr: expected})
        elif op == "ne":
            q &= ~Q(**{attr: expected})
        elif op == "in":
            q &= Q(**{f"{attr}__in": expected})
        elif op == "not_in":
            q &= ~Q(**{f"{attr}__in": expected})
        else:
            q &= Q(**{f"{attr}__contains": [expected]})
    return q
