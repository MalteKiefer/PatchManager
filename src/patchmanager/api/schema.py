"""OpenAPI description of the token authentication (loaded by ApiConfig.ready)."""

from typing import Any

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class TokenScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
    """Document the bearer scheme."""

    target_class = "patchmanager.api.authentication.TokenAuthentication"
    name = "bearer"

    def get_security_definition(self, auto_schema: Any) -> dict[str, Any]:
        """Bearer tokens: personal tokens or OAuth client credential access tokens."""
        return {
            "type": "http",
            "scheme": "bearer",
            "description": "Personal API token (pmat_...) or an OAuth 2.0 access token"
            " from /api/oauth/token/.",
        }
