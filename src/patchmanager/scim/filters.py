"""SCIM filter expressions (RFC 7644 section 3.4.2.2) to Django Q objects.

Only attributes listed in the resource's map can be filtered (an allow list), so
a filter can never reach other columns. Expressions are bounded in length and
nesting depth.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db.models import Q

MAX_LENGTH = 1000
MAX_DEPTH = 10
OPS = {"eq", "ne", "co", "sw", "ew", "pr", "gt", "ge", "lt", "le"}


class FilterError(ValueError):
    """The filter is invalid (SCIM ``invalidFilter``)."""


@dataclass(frozen=True)
class Attribute:
    """How a SCIM attribute maps to the model."""

    field: str
    kind: str = "string"  # string, bool, uuid, datetime


def _tokens(text: str) -> Iterator[tuple[str, Any]]:
    """Split a filter into strings, brackets and words in one linear pass."""
    position, end = 0, len(text)
    while position < end:
        char = text[position]
        if char.isspace():
            position += 1
        elif char in "()[]":
            yield char, char
            position += 1
        elif char == '"':
            value: list[str] = []
            position += 1
            while position < end and text[position] != '"':
                if text[position] == "\\" and position + 1 < end:
                    position += 1
                value.append(text[position])
                position += 1
            if position >= end:
                raise FilterError("Unterminated string in filter.")
            position += 1
            yield "value", "".join(value)
        else:
            start = position
            while position < end and not text[position].isspace() and text[position] not in '()[]"':
                position += 1
            yield "word", text[start:position]


class _Parser:
    def __init__(self, text: str, attributes: dict[str, Attribute]) -> None:
        if len(text) > MAX_LENGTH:
            raise FilterError("The filter is too long.")
        self.tokens = list(_tokens(text))
        self.position = 0
        self.attributes = attributes
        self.depth = 0

    def peek(self) -> tuple[str, Any] | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self) -> tuple[str, Any]:
        token = self.peek()
        if token is None:
            raise FilterError("The filter ends unexpectedly.")
        self.position += 1
        return token

    def keyword(self, word: str) -> bool:
        token = self.peek()
        if token and token[0] == "word" and str(token[1]).lower() == word:
            self.position += 1
            return True
        return False

    def expect(self, kind: str) -> None:
        if self.take()[0] != kind:
            raise FilterError(f"Expected {kind} in filter.")

    def parse(self) -> Q:
        q = self.disjunction("")
        if self.peek() is not None:
            raise FilterError("Unexpected text after the filter.")
        return q

    def nested(self, prefix: str, closing: str) -> Q:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise FilterError("The filter is nested too deeply.")
        q = self.disjunction(prefix)
        self.expect(closing)
        self.depth -= 1
        return q

    def disjunction(self, prefix: str) -> Q:
        q = self.conjunction(prefix)
        while self.keyword("or"):
            q |= self.conjunction(prefix)
        return q

    def conjunction(self, prefix: str) -> Q:
        q = self.negation(prefix)
        while self.keyword("and"):
            q &= self.negation(prefix)
        return q

    def negation(self, prefix: str) -> Q:
        if self.keyword("not"):
            self.expect("(")
            return ~self.nested(prefix, ")")
        return self.primary(prefix)

    def primary(self, prefix: str) -> Q:
        kind, value = self.take()
        if kind == "(":
            return self.nested(prefix, ")")
        if kind != "word":
            raise FilterError("Expected an attribute in filter.")
        path = (prefix + str(value)).lower()
        if self.peek() and self.peek()[0] == "[":  # type: ignore[index]  # valuePath: emails[value eq "x"]
            self.take()
            return self.nested(path + ".", "]")
        op_kind, op = self.take()
        op = str(op).lower()
        if op_kind != "word" or op not in OPS:
            raise FilterError("Unknown operator in filter.")
        attribute = self.attributes.get(path)
        if attribute is None:
            raise FilterError(f"Filtering by {path} is not supported.")
        if op == "pr":
            return _present(attribute)
        value_kind, raw = self.take()
        if value_kind not in ("value", "word"):
            raise FilterError("Expected a value in filter.")
        return _compare(attribute, op, raw if value_kind == "value" else _literal(str(raw)))


def _literal(word: str) -> Any:
    lowered = word.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered == "null":
        return None
    try:
        return float(word) if "." in word else int(word)
    except ValueError as exc:
        raise FilterError("Values must be quoted strings, numbers, true, false or null.") from exc


def _convert(attribute: Attribute, value: Any) -> Any:
    try:
        if attribute.kind == "bool":
            if isinstance(value, bool):
                return value
            return str(value).lower() == "true"
        if attribute.kind == "uuid":
            return UUID(str(value))
        if attribute.kind == "datetime":
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise FilterError(f"Invalid value for {attribute.field}.") from exc
    return value


def _present(attribute: Attribute) -> Q:
    q = Q(**{f"{attribute.field}__isnull": False})
    return q & ~Q(**{attribute.field: ""}) if attribute.kind == "string" else q


def _compare(attribute: Attribute, op: str, raw: Any) -> Q:
    value = _convert(attribute, raw)
    field = attribute.field
    text = attribute.kind == "string"
    if value is None:
        q = Q(**{f"{field}__isnull": True})
        return ~q if op == "ne" else q
    lookups = {
        "eq": "iexact" if text else "exact",
        "ne": "iexact" if text else "exact",
        "co": "icontains",
        "sw": "istartswith",
        "ew": "iendswith",
        "gt": "gt",
        "ge": "gte",
        "lt": "lt",
        "le": "lte",
    }
    if op in ("co", "sw", "ew") and not text:
        raise FilterError(f"{op} needs a text attribute.")
    q = Q(**{f"{field}__{lookups[op]}": value})
    return ~q if op == "ne" else q


def parse(text: str, attributes: dict[str, Attribute]) -> Q:
    """``text`` as a Q object over the mapped fields. Raises FilterError."""
    return _Parser(text, attributes).parse()
