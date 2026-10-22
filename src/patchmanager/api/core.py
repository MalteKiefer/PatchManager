"""Shared API plumbing: errors, scopes, pagination, filters and visibility."""

from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import F, QuerySet, TextField, Value
from django.db.models.functions import Cast, Coalesce, Replace
from django.http import Http404
from django.utils.translation import gettext as _
from rest_framework import exceptions, pagination, permissions
from rest_framework.request import Request
from rest_framework.response import Response

from patchmanager.accounts import policy
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.api.models import ApiToken
from patchmanager.core.fields import LtreeField
from patchmanager.core.utils import parse_uuid


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """Domain errors as API errors: permission problems 403, the rest 400."""
    from rest_framework.views import (
        exception_handler as drf_handler,
    )  # DRF imports this module early

    if isinstance(exc, PermissionDeniedError):
        exc = exceptions.PermissionDenied(_("You do not have permission to do that."))
    elif isinstance(exc, ServiceError):
        exc = exceptions.ValidationError({"detail": str(exc)})
    return drf_handler(exc, context)


class TokenScopes(permissions.BasePermission):
    """Read scope for safe methods, write scope for changes. Sessions are not limited."""

    message = "This token lacks the required scope."

    def has_permission(self, request: Request, view: Any) -> bool:
        """Check the token's scopes against the method."""
        from patchmanager.instance import editions

        if not editions.enabled("api"):
            self.message = _("The REST API is not part of this edition.")
            return False
        if not (request.user and request.user.is_authenticated):
            return False
        if isinstance(request.auth, ApiToken):
            needed = "read" if request.method in permissions.SAFE_METHODS else "write"
            return needed in request.auth.scopes
        return True


class Pagination(pagination.LimitOffsetPagination):
    """``?limit=`` (default 100, at most 1000) and ``?offset=``."""

    default_limit = 100
    max_limit = 1000


def filtered(queryset: QuerySet[Any], params: Any, spec: dict[str, str]) -> QuerySet[Any]:
    """Apply simple query parameter filters; lookups ending in ``_id`` take UUIDs."""
    for param, lookup in spec.items():
        value = params.get(param)
        if value in (None, ""):
            continue
        if lookup.endswith("_id"):
            uuid = parse_uuid(value)
            if uuid is None:
                raise exceptions.ValidationError({param: _("Not a valid id.")})
            value = uuid
        try:
            queryset = queryset.filter(**{lookup: value})
        except (ValueError, DjangoValidationError) as exc:
            raise exceptions.ValidationError({param: [_("Not a valid value.")]}) from exc
    return queryset


def tenant_scope(queryset: QuerySet[Any], site_field: str | None = None) -> QuerySet[Any]:
    """Annotate ``scope``: the site path if set, else the tenant path (as for VLANs and WLANs)."""
    tenant_path = Cast(Replace(Cast("tenant_id", TextField()), Value("-"), Value("")), LtreeField())
    scope = Coalesce(F(f"{site_field}__path"), tenant_path) if site_field else tenant_path
    annotated: QuerySet[Any] = queryset.annotate(scope=Cast(scope, LtreeField()))
    return annotated


def visible(
    user: Any, permission: str, queryset: QuerySet[Any], *, path_field: str = "path"
) -> QuerySet[Any]:
    """Rows the user may access with ``permission``."""
    return policy.scope_queryset(user, permission, queryset, path_field=path_field)


def lookup(
    user: Any, model: Any, value: Any, permission: str | None, field: str, *, judge: Any = None
) -> Any:
    """A referenced object the user may see, else a validation error naming ``field``.

    ``permission`` None relies on Row Level Security alone (tenants); ``judge``
    picks the object the permission applies to (ports: their device).
    """
    pk = parse_uuid(value)
    obj = model._default_manager.filter(pk=pk).first() if pk else None  # RLS bounds the tenant
    if obj is None or (
        permission and not policy.has_perm(user, permission, judge(obj) if judge else obj)
    ):
        raise exceptions.ValidationError({field: [_("Not found.")]})
    return obj


def not_found() -> Http404:
    """Uniform 404."""
    return Http404()
