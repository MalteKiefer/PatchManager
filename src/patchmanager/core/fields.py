"""PostgreSQL ``ltree`` paths for permission scopes, and ``cidr``/``inet`` for IPAM."""

import ipaddress
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Lookup
from django.utils.translation import gettext_lazy as _


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


class _NetworkField(models.Field):  # type: ignore[type-arg]
    """Text in Python, a native PostgreSQL network type in the database."""

    db_column_type = ""
    message: Any = ""

    def db_type(self, connection: Any) -> str:
        """Return the native column type."""
        return self.db_column_type

    def parse(self, value: str) -> str:
        """Canonical text form; raises ``ValueError`` for invalid input."""
        raise NotImplementedError

    def to_python(self, value: Any) -> Any:
        """Validate and normalise (``2001:DB8::/32`` becomes ``2001:db8::/32``)."""
        if value is None or value == "":
            return value
        try:
            return self.parse(str(value).strip())
        except ValueError as exc:
            raise ValidationError(self.message, code="invalid") from exc

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> Any:
        """The driver may return text or ``ipaddress`` objects."""
        return None if value is None else str(value)

    def get_prep_value(self, value: Any) -> Any:
        """Store the canonical form."""
        return None if value is None or value == "" else self.to_python(value)

    def formfield(self, **kwargs: Any) -> Any:
        """A plain text input; the model field validates."""
        return forms.CharField(max_length=50, **kwargs)


class CidrField(_NetworkField):
    """An IPv4 or IPv6 network such as ``10.0.0.0/24`` (PostgreSQL ``cidr``)."""

    description = "IP network"
    db_column_type = "cidr"
    message = _("Enter a network such as 10.0.0.0/24 or 2001:db8::/48 (host bits zero).")

    def parse(self, value: str) -> str:
        """Networks must not have host bits set."""
        return str(ipaddress.ip_network(value, strict=True))


class InetField(_NetworkField):
    """An address with its prefix length such as ``10.0.0.5/24`` (PostgreSQL ``inet``)."""

    description = "IP address with prefix length"
    db_column_type = "inet"
    message = _("Enter an address such as 10.0.0.5/24 or 2001:db8::5/64.")

    def parse(self, value: str) -> str:
        """Without a length an address is a single host (/32 or /128)."""
        return str(ipaddress.ip_interface(value))


class _NetworkLookup(Lookup):
    operator = ""
    cast = "inet"

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        """Render ``lhs <op> rhs::inet``."""
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        return f"{lhs} {self.operator} {rhs}::{self.cast}", (*lhs_params, *rhs_params)


class NetWithin(_NetworkLookup):
    """``value << network``: strictly inside."""

    lookup_name = "net_within"
    operator = "<<"


class NetWithinOrEqual(_NetworkLookup):
    """``value <<= network``: inside or the same network."""

    lookup_name = "net_within_or_equal"
    operator = "<<="


class NetContains(_NetworkLookup):
    """``network >> value``: strictly contains."""

    lookup_name = "net_contains"
    operator = ">>"


class NetContainsOrEqual(_NetworkLookup):
    """``network >>= value``: contains or is the same network."""

    lookup_name = "net_contains_or_equal"
    operator = ">>="


for _field in (CidrField, InetField):
    for _lookup in (NetWithin, NetWithinOrEqual, NetContains, NetContainsOrEqual):
        _field.register_lookup(_lookup)
