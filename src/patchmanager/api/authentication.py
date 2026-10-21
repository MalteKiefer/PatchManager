"""Bearer token authentication that also sets the database tenant context."""

from typing import Any

from django.utils.translation import gettext_lazy as _
from rest_framework import authentication, exceptions
from rest_framework.request import Request

from patchmanager.accounts import policy
from patchmanager.api import tokens
from patchmanager.tenancy import rls


class TokenAuthentication(authentication.BaseAuthentication):
    """``Authorization: Bearer <token>`` with personal or OAuth tokens."""

    keyword = "Bearer"

    def authenticate(self, request: Request) -> tuple[Any, Any] | None:
        """Return (user, token) or None when no bearer token is sent."""
        header = authentication.get_authorization_header(request).decode("latin-1")
        if not header.startswith(self.keyword + " "):
            return None
        token = tokens.authenticate(header[len(self.keyword) + 1 :].strip(), request._request)
        if token is None or not policy._usable(token.user):  # same rule as sessions
            raise exceptions.AuthenticationFailed(_("Invalid or expired token."))
        # The request middleware ran before authentication: give RLS this user's visibility now.
        all_tenants, tenant_ids = policy.accessible_tenants(token.user)
        if token.tenant_id is not None:
            visible = all_tenants or token.tenant_id in tenant_ids
            all_tenants, tenant_ids = False, {token.tenant_id} if visible else set()
        rls.set_context(tenant_ids, all_tenants=all_tenants)
        rls.set_actor(
            token.user.pk,
            f"{token.user} (API: {token.name})",
            getattr(request._request, "request_id", ""),
        )
        return token.user, token

    def authenticate_header(self, request: Request) -> str:
        """Ask for a bearer token on 401."""
        return f'{self.keyword} realm="api"'
