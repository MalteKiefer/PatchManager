"""SAML 2.0 service provider with pysaml2 (signatures checked by xmlsec1).

Strict settings: signed assertions are required, unsolicited responses are
refused (InResponseTo must match a request of this session), clock skew is
one minute, and the IdP's metadata is pasted by a super admin (no fetching).
"""

import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext as _
from saml2 import BINDING_HTTP_POST, BINDING_HTTP_REDIRECT
from saml2.client import Saml2Client
from saml2.config import SPConfig
from saml2.metadata import create_metadata_string
from saml2.s_utils import UnsupportedBinding
from saml2.saml import NAMEID_FORMAT_EMAILADDRESS, NAMEID_FORMAT_PERSISTENT, NameID

from patchmanager.accounts.services import ServiceError
from patchmanager.sso.models import IdentityProvider
from patchmanager.sso.services import Assertion

# The IdP posts the response cross site, where browsers do not send the Lax session
# cookie. A cookie of its own carries a random key; the outstanding request ids are
# kept on the server under it and consumed on use, and assertion ids are single use.
COOKIE = "__Host-pm_saml" if not settings.DEBUG else "pm_saml"
COOKIE_SECONDS = 600
USED_SECONDS = 3600  # assertion ids are remembered longer than assertions live


def xmlsec_binary() -> str:
    """The xmlsec1 next to the interpreter (pixi environment), else on PATH."""
    local = Path(sys.prefix) / "bin" / "xmlsec1"
    found = str(local) if local.exists() else shutil.which("xmlsec1")
    if not found:
        raise ServiceError(_("SAML is not available: xmlsec1 is missing."))
    return found


def _urls(request: HttpRequest, provider: IdentityProvider) -> dict[str, str]:
    def absolute(name: str) -> str:
        return request.build_absolute_uri(reverse(name, args=[provider.slug]))

    return {
        "entity": absolute("sso:saml-metadata"),
        "acs": absolute("sso:saml-acs"),
        "sls": absolute("sso:saml-sls"),
    }


def client(request: HttpRequest, provider: IdentityProvider) -> Saml2Client:
    """A pysaml2 client configured for ``provider``."""
    urls = _urls(request, provider)
    settings: dict[str, Any] = {
        "entityid": urls["entity"],
        "xmlsec_binary": xmlsec_binary(),
        "accepted_time_diff": 60,
        "allow_unknown_attributes": True,
        "service": {
            "sp": {
                "endpoints": {
                    "assertion_consumer_service": [(urls["acs"], BINDING_HTTP_POST)],
                    "single_logout_service": [(urls["sls"], BINDING_HTTP_REDIRECT)],
                },
                "allow_unsolicited": False,
                "want_assertions_signed": True,
                "want_response_signed": False,
                "authn_requests_signed": False,
                "logout_requests_signed": False,
                "name_id_format": provider.config.get("nameid_format") or NAMEID_FORMAT_PERSISTENT,
            }
        },
        "metadata": {"inline": [provider.config.get("idp_metadata", "")]},
    }
    try:
        config = SPConfig()
        config.load(settings)
        return Saml2Client(config=config)
    except Exception as exc:  # pysaml2 raises many types for broken metadata
        raise ServiceError(_("The SAML configuration of this provider is invalid.")) from exc


def _idp(saml: Saml2Client) -> str:
    entities = list(saml.metadata.identity_providers())
    if len(entities) != 1:
        raise ServiceError(_("The IdP metadata must describe exactly one identity provider."))
    return str(entities[0])


def _state_key(request: HttpRequest) -> str | None:
    key = request.COOKIES.get(COOKIE, "")
    return (
        f"saml:outstanding:{key}"
        if len(key) == 43 and all(c.isalnum() or c in "-_" for c in key)
        else None
    )


def _outstanding(request: HttpRequest) -> dict[str, str]:
    key = _state_key(request)
    value = cache.get(key) if key else None
    return value if isinstance(value, dict) else {}


def start(request: HttpRequest, provider: IdentityProvider) -> HttpResponse:
    """Redirect with a new AuthnRequest; its id is remembered to check InResponseTo."""
    saml = client(request, provider)
    request_id, info = saml.prepare_for_authenticate(
        entityid=_idp(saml), binding=BINDING_HTTP_REDIRECT
    )
    outstanding = dict(list(_outstanding(request).items())[-4:]) | {
        request_id: str(provider.pk)
    }  # a few tabs
    previous = _state_key(request)
    key = secrets.token_urlsafe(32)  # always a fresh key: the cookie never echoes client input
    cache.set(f"saml:outstanding:{key}", outstanding, COOKIE_SECONDS)
    if previous:
        cache.delete(previous)
    response = HttpResponseRedirect(str(dict(info["headers"])["Location"]))
    response.set_cookie(
        COOKIE,
        key,
        max_age=COOKIE_SECONDS,
        secure=not settings.DEBUG,
        httponly=True,
        samesite="None" if not settings.DEBUG else "Lax",
        path="/",
    )
    return response


def _session_index(response: Any) -> str:
    statements = response.assertion.authn_statement or []
    return str(statements[0].session_index or "") if statements else ""


def _first(ava: dict[str, list[Any]], name: str) -> str:
    values = ava.get(name) or []
    return str(values[0]) if values else ""


def finish(request: HttpRequest, provider: IdentityProvider) -> Assertion:
    """Check the posted response (signature, audience, time, InResponseTo) and read the person."""
    outstanding = {k: v for k, v in _outstanding(request).items() if v == str(provider.pk)}
    if not outstanding:
        raise ServiceError(_("This sign in expired. Please try again."))
    saml = client(request, provider)
    try:
        response = saml.parse_authn_request_response(
            request.POST.get("SAMLResponse", ""), BINDING_HTTP_POST, outstanding=outstanding
        )
    except Exception as exc:  # invalid signature, audience, time, unknown request ...
        raise ServiceError(_("The sign in could not be verified.")) from exc
    if response is None or response.assertion is None:
        raise ServiceError(_("The sign in could not be verified."))
    remaining = {k: v for k, v in _outstanding(request).items() if k != response.in_response_to}
    cache.set(str(_state_key(request)), remaining, COOKIE_SECONDS)  # each request is answered once
    if not cache.add(f"saml:assertion:{response.assertion.id}", 1, USED_SECONDS):
        raise ServiceError(_("The sign in could not be verified."))  # the same assertion again
    name_id = response.get_subject()
    ava = response.get_identity()
    cfg = provider.config
    email = _first(ava, cfg.get("email_attribute") or "mail")
    if not email and name_id.format == NAMEID_FORMAT_EMAILADDRESS:
        email = name_id.text
    return Assertion(
        subject=str(name_id.text),
        email=email,
        name=_first(ava, cfg.get("name_attribute") or "displayName"),
        groups=[str(g) for g in ava.get(cfg.get("groups_attribute") or "groups", [])],
        email_verified=True,  # the IdP signed it
        logout={
            "kind": "saml",
            "name_id": name_id.text,
            "format": name_id.format or "",
            "session_index": _session_index(response),
        },
    )


def metadata(request: HttpRequest, provider: IdentityProvider) -> str:
    """Metadata of this service provider, for the IdP."""
    saml = client(request, provider)
    return str(create_metadata_string(None, config=saml.config, valid=24 * 7).decode())


def logout_url(
    request: HttpRequest, provider: IdentityProvider, data: dict[str, Any]
) -> str | None:
    """SP initiated single logout (HTTP Redirect), if the IdP offers it."""
    saml = client(request, provider)
    idp = _idp(saml)
    try:
        destination = saml.metadata.single_logout_service(idp, BINDING_HTTP_REDIRECT, "idpsso")[0][
            "location"
        ]
    except IndexError, KeyError, TypeError, UnsupportedBinding:
        return None
    name_id = NameID(text=data.get("name_id", ""), format=data.get("format") or None)
    _request_id, logout_request = saml.create_logout_request(
        destination,
        idp,
        name_id=name_id,
        session_indexes=[data["session_index"]] if data.get("session_index") else None,
    )
    info = saml.apply_binding(BINDING_HTTP_REDIRECT, str(logout_request), destination)
    return str(dict(info["headers"])["Location"])
