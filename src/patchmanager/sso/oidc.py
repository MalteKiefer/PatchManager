"""OpenID Connect: authorization code flow with PKCE, state and nonce.

Discovery documents and key sets come from the configured issuer, which only
super admins set. ID tokens are verified with joserfc: signature from the
issuer's keys (no ``none``, no symmetric algorithms), issuer, audience, expiry,
nonce and, with several audiences, the authorized party.
"""

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import requests
from django.core.cache import cache
from django.http import HttpRequest
from django.urls import reverse
from django.utils.translation import gettext as _
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from patchmanager.accounts.services import ServiceError
from patchmanager.core import crypto
from patchmanager.sso.models import IdentityProvider
from patchmanager.sso.services import Assertion

TIMEOUT = 10
ALGORITHMS = [
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
]
STATE = "_pm_oidc"  # session: state, nonce, verifier and provider of the flow in progress


def _get(url: str) -> dict[str, Any]:
    try:
        response = requests.get(url, timeout=TIMEOUT, headers={"Accept": "application/json"})
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ServiceError(_("The identity provider is not reachable.")) from exc
    if not isinstance(data, dict):
        raise ServiceError(_("The identity provider answered unexpectedly."))
    return data


def discovery(provider: IdentityProvider) -> dict[str, Any]:
    """The issuer's metadata (cached one hour)."""
    issuer = str(provider.config.get("issuer", "")).rstrip("/")
    key = f"oidc:discovery:{provider.pk}:{issuer}"
    data = cache.get(key)
    if data is None:
        data = _get(f"{issuer}/.well-known/openid-configuration")
        if data.get("issuer", "").rstrip("/") != issuer:
            raise ServiceError(_("The issuer in the metadata does not match the configuration."))
        cache.set(key, data, 3600)
    return dict(data)


def _keys(provider: IdentityProvider, refresh: bool = False) -> KeySet:
    key = f"oidc:jwks:{provider.pk}"
    data = None if refresh else cache.get(key)
    if data is None:
        data = _get(discovery(provider)["jwks_uri"])
        cache.set(key, data, 3600)
    return KeySet.import_key_set(data)  # type: ignore[arg-type]  # validated by joserfc


def redirect_uri(request: HttpRequest, provider: IdentityProvider) -> str:
    """Callback URL to register at the provider."""
    return request.build_absolute_uri(reverse("sso:oidc-callback", args=[provider.slug]))


def start(request: HttpRequest, provider: IdentityProvider, login_hint: str = "") -> str:
    """URL of the provider's authorization endpoint for a new flow."""
    meta = discovery(provider)
    verifier = secrets.token_urlsafe(48)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    request.session[STATE] = {
        "provider": str(provider.pk),
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
    }
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    params = {
        "response_type": "code",
        "client_id": provider.config.get("client_id", ""),
        "redirect_uri": redirect_uri(request, provider),
        "scope": provider.config.get("scopes") or "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint[:254]
    return f"{meta['authorization_endpoint']}?{urlencode(params)}"


def _verify(provider: IdentityProvider, id_token: str, nonce: str) -> dict[str, Any]:
    client_id = provider.config.get("client_id", "")
    for refresh in (False, True):  # a new signing key appears: fetch the key set once more
        try:
            token = jwt.decode(id_token, _keys(provider, refresh=refresh), algorithms=ALGORITHMS)
            break
        except JoseError as exc:
            if refresh:
                raise ServiceError(_("The sign in could not be verified.")) from exc
    claims = dict(token.claims)
    registry = jwt.JWTClaimsRegistry(
        leeway=60,
        iss={"essential": True, "value": discovery(provider)["issuer"]},
        aud={"essential": True},
        exp={"essential": True},
        iat={"essential": True},
        sub={"essential": True},
        nonce={"essential": True, "value": nonce},
    )
    try:
        registry.validate(claims)
    except JoseError as exc:
        raise ServiceError(_("The sign in could not be verified.")) from exc
    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if client_id not in audiences or (len(audiences) > 1 and claims.get("azp") != client_id):
        raise ServiceError(_("The sign in could not be verified."))
    return claims


def finish(request: HttpRequest, provider: IdentityProvider) -> Assertion:
    """Handle the callback: check state, redeem the code with the verifier, verify the ID token."""
    flow = request.session.pop(STATE, None)
    if request.GET.get("error"):
        raise ServiceError(_("The identity provider refused the sign in."))
    if (
        not flow
        or flow["provider"] != str(provider.pk)
        or not secrets.compare_digest(flow["state"], request.GET.get("state", ""))
    ):
        raise ServiceError(_("This sign in expired. Please try again."))
    meta = discovery(provider)
    client_id = provider.config.get("client_id", "")
    secret = crypto.decrypt(provider.encrypted_secret) if provider.encrypted_secret else ""
    try:
        response = requests.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": request.GET.get("code", ""),
                "redirect_uri": redirect_uri(request, provider),
                "code_verifier": flow["verifier"],
                "client_id": client_id,
            },
            auth=(client_id, secret) if secret else None,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        tokens = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ServiceError(_("The identity provider refused the sign in.")) from exc
    if not isinstance(tokens, dict) or not tokens.get("id_token"):
        raise ServiceError(_("The identity provider refused the sign in."))
    claims = _verify(provider, tokens["id_token"], flow["nonce"])
    cfg = provider.config
    email = claims.get(cfg.get("email_claim") or "email", "")
    if not email and tokens.get("access_token") and meta.get("userinfo_endpoint"):
        try:
            info = requests.get(
                meta["userinfo_endpoint"],
                timeout=TIMEOUT,
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            ).json()
        except (requests.RequestException, ValueError) as exc:
            raise ServiceError(_("The identity provider is not reachable.")) from exc
        if isinstance(info, dict) and info.get("sub") == claims["sub"]:
            claims = {**info, **claims}
            email = claims.get(cfg.get("email_claim") or "email", "")
    groups = claims.get(cfg.get("groups_claim") or "groups", [])
    return Assertion(
        subject=str(claims["sub"]),
        email=str(email or ""),
        name=str(claims.get(cfg.get("name_claim") or "name", "") or ""),
        groups=[str(g) for g in groups] if isinstance(groups, list) else [],
        email_verified=bool(claims.get("email_verified")) or bool(cfg.get("trust_email")),
        logout={"kind": "oidc", "id_token": tokens["id_token"]},
    )


def logout_url(
    request: HttpRequest, provider: IdentityProvider, data: dict[str, Any]
) -> str | None:
    """RP initiated logout at the provider, if it supports it."""
    try:
        endpoint = discovery(provider).get("end_session_endpoint")
    except ServiceError:
        return None
    if not endpoint:
        return None
    params = {
        "id_token_hint": data.get("id_token", ""),
        "client_id": provider.config.get("client_id", ""),
        "post_logout_redirect_uri": request.build_absolute_uri(reverse("accounts:login")),
    }
    return f"{endpoint}?{urlencode(params)}"
