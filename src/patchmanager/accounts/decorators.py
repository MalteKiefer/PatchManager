"""View guards that delegate to the policy service."""

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.http.response import HttpResponseBase

from patchmanager.accounts import policy

View = Callable[..., HttpResponseBase]


def permission_required(*perms: str, any_scope: bool = False) -> Callable[[View], View]:
    """Require all ``perms`` (globally, or with ``any_scope`` in at least one tenant)."""

    def decorator(view: View) -> View:
        @login_required
        @wraps(view)
        def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
            for perm in perms:
                if policy.has_perm(request.user, perm):
                    continue
                if any_scope and any(
                    policy.has_perm(request.user, perm, t)
                    for t in getattr(request, "available_tenants", [])
                ):
                    continue
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
