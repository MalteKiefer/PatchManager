"""SCIM 2.0 endpoints (RFC 7644) and the token administration page."""

import json
import secrets
from collections.abc import Callable
from functools import wraps
from typing import Any

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from patchmanager.accounts.decorators import permission_required
from patchmanager.accounts.models import User
from patchmanager.api.tokens import _matches, digest
from patchmanager.audit import services as audit
from patchmanager.core import ratelimit
from patchmanager.instance.gates import feature_required
from patchmanager.scim import filters, resources
from patchmanager.scim.models import ScimToken
from patchmanager.scim.resources import ScimError

CONTENT_TYPE = "application/scim+json"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
MAX_COUNT = 200
REQUESTS_PER_MINUTE = 600
SHOW_ONCE = "_pm_scim_token"

Handler = Callable[..., HttpResponse]


def _json(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> HttpResponse:
    response = HttpResponse(json.dumps(data, default=str), status=status, content_type=CONTENT_TYPE)
    for key, value in (headers or {}).items():
        response[key] = value
    return response


def _error(status: int, detail: str, scim_type: str = "") -> HttpResponse:
    body: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return _json(body, status)


def _authenticated(view: Handler) -> Handler:
    """Bearer SCIM token, rate limited; errors in SCIM form."""

    @csrf_exempt  # token authenticated, no cookies
    @wraps(view)
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        from patchmanager.instance import editions

        if not editions.enabled("scim"):
            return _error(404, "SCIM is not part of this edition.")
        header = request.headers.get("Authorization", "")
        parts = header.removeprefix("Bearer ").strip().split("_", 2)
        token = None
        if header.startswith("Bearer ") and len(parts) == 3 and parts[0] == "pmsc":
            candidate = ScimToken.objects.filter(prefix=parts[1], revoked_at__isnull=True).first()
            if candidate is not None and _matches(candidate.token_hash, "_".join(parts)):
                token = candidate
        if token is None:
            response = _error(401, "Authentication required.")
            response["WWW-Authenticate"] = 'Bearer realm="scim"'
            return response
        if not ratelimit.hit(f"scim:{token.pk}", limit=REQUESTS_PER_MINUTE, window=60):
            return _error(429, "Too many requests.")
        if (
            token.last_used_at is None
            or (timezone.now() - token.last_used_at).total_seconds() > 300
        ):
            ScimToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
        request.scim_token = token  # type: ignore[attr-defined]
        try:
            return view(request, *args, **kwargs)
        except ScimError as exc:
            return _error(exc.status, exc.detail, exc.scim_type)

    return wrapped


def _body(request: HttpRequest) -> dict[str, Any]:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError as exc:
        raise ScimError(400, "The body is not valid JSON.", "invalidSyntax") from exc
    if not isinstance(data, dict):
        raise ScimError(400, "The body must be a JSON object.", "invalidSyntax")
    return data


def _operations(request: HttpRequest) -> list[dict[str, Any]]:
    data = _body(request)
    operations = data.get("Operations")
    if resources.PATCH_SCHEMA not in data.get("schemas", []) or not isinstance(operations, list):
        raise ScimError(400, "Expected a PatchOp with Operations.", "invalidSyntax")
    return [o for o in operations if isinstance(o, dict)]


def _list(
    request: HttpRequest,
    rows: QuerySet[Any],
    attributes: dict[str, filters.Attribute],
    render_one: Callable[[HttpRequest, Any], dict[str, Any]],
) -> HttpResponse:
    text = request.GET.get("filter", "").strip()
    if text:
        try:
            rows = rows.filter(filters.parse(text, attributes)).distinct()
        except (filters.FilterError, ValidationError) as exc:
            raise ScimError(400, str(exc), "invalidFilter") from exc
    try:
        start = max(int(request.GET.get("startIndex", "1")), 1)
        count = min(max(int(request.GET.get("count", "100")), 0), MAX_COUNT)
    except ValueError as exc:
        raise ScimError(400, "startIndex and count must be numbers.", "invalidValue") from exc
    rows = rows.order_by("created_at", "pk")
    page = list(rows[start - 1 : start - 1 + count])
    return _json(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": rows.count(),
            "startIndex": start,
            "itemsPerPage": len(page),
            "Resources": [render_one(request, r) for r in page],
        }
    )


def _get(rows: QuerySet[Any], pk: str) -> Any:
    try:
        obj = rows.filter(pk=pk).first()
    except ValidationError:
        obj = None
    if obj is None:
        raise ScimError(404, "Resource not found.")
    return obj


def _one(
    request: HttpRequest,
    obj: Any,
    render_one: Callable[[HttpRequest, Any], dict[str, Any]],
    status: int = 200,
) -> HttpResponse:
    body = render_one(request, obj)
    return _json(
        body, status, {"ETag": body["meta"]["version"], "Location": body["meta"]["location"]}
    )


@_authenticated
def users(request: HttpRequest) -> HttpResponse:
    """GET (list, filter) or POST (create) Users."""
    token = request.scim_token  # type: ignore[attr-defined]
    if request.method == "POST":
        user = resources.create_user(request, _body(request), token)
        return _one(
            request, resources.users(token.domains).get(pk=user.pk), resources.user_json, 201
        )
    if request.method != "GET":
        raise ScimError(405, "Method not allowed.")
    return _list(
        request, resources.users(token.domains), resources.USER_FILTERS, resources.user_json
    )


@_authenticated
def user(request: HttpRequest, pk: str) -> HttpResponse:
    """GET, PUT, PATCH or DELETE one User."""
    token = request.scim_token  # type: ignore[attr-defined]
    obj = _get(resources.users(token.domains), pk)
    if request.method == "GET":
        return _one(request, obj, resources.user_json)
    resources.check_version(request, obj)
    if request.method == "PUT":
        resources.replace_user(request, obj, _body(request), token)
    elif request.method == "PATCH":
        resources.patch_user(request, obj, _operations(request), token)
    elif request.method == "DELETE":
        resources.delete_user(request, obj, token)
        return HttpResponse(status=204)
    else:
        raise ScimError(405, "Method not allowed.")
    return _one(request, resources.users(token.domains).get(pk=obj.pk), resources.user_json)


@_authenticated
def groups(request: HttpRequest) -> HttpResponse:
    """GET (list, filter) or POST (create) Groups."""
    token = request.scim_token  # type: ignore[attr-defined]
    if request.method == "POST":
        group = resources.create_group(request, _body(request), token)
        return _one(request, resources.groups(token).get(pk=group.pk), resources.group_json, 201)
    if request.method != "GET":
        raise ScimError(405, "Method not allowed.")
    return _list(request, resources.groups(token), resources.GROUP_FILTERS, resources.group_json)


@_authenticated
def group(request: HttpRequest, pk: str) -> HttpResponse:
    """GET, PUT, PATCH or DELETE one Group."""
    token = request.scim_token  # type: ignore[attr-defined]
    obj = _get(resources.groups(token), pk)
    if request.method == "GET":
        return _one(request, obj, resources.group_json)
    resources.check_version(request, obj)
    if request.method == "PUT":
        resources.replace_group(request, obj, _body(request), token)
    elif request.method == "PATCH":
        resources.patch_group(request, obj, _operations(request), token)
    elif request.method == "DELETE":
        resources.delete_group(request, obj, token)
        return HttpResponse(status=204)
    else:
        raise ScimError(405, "Method not allowed.")
    return _one(request, resources.groups(token).get(pk=obj.pk), resources.group_json)


@_authenticated
def service_provider_config(request: HttpRequest) -> HttpResponse:
    """What this server supports."""
    return _json(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": MAX_COUNT},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": True},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "Bearer token",
                    "primary": True,
                    "description": "SCIM token created under Administration",
                }
            ],
        }
    )


def _resource_type(request: HttpRequest, name: str, schema: str) -> dict[str, Any]:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": name,
        "name": name,
        "endpoint": f"/{name}s",
        "schema": schema,
        "meta": {
            "resourceType": "ResourceType",
            "location": request.build_absolute_uri(f"/scim/v2/ResourceTypes/{name}"),
        },
    }


@_authenticated
def resource_types(request: HttpRequest) -> HttpResponse:
    """User and Group."""
    items = [
        _resource_type(request, "User", resources.USER_SCHEMA),
        _resource_type(request, "Group", resources.GROUP_SCHEMA),
    ]
    return _json({"schemas": [LIST_SCHEMA], "totalResults": 2, "Resources": items})


def _attribute(name: str, kind: str = "string", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "type": kind,
        "multiValued": False,
        "required": False,
        "caseExact": False,
        "mutability": "readWrite",
        "returned": "default",
        "uniqueness": "none",
        **extra,
    }


@_authenticated
def schemas(request: HttpRequest) -> HttpResponse:
    """The supported attributes."""
    user_schema = {
        "id": resources.USER_SCHEMA,
        "name": "User",
        "attributes": [
            _attribute("userName", required=True, uniqueness="server"),
            _attribute("displayName"),
            _attribute(
                "name",
                "complex",
                subAttributes=[
                    _attribute("formatted"),
                    _attribute("givenName"),
                    _attribute("familyName"),
                ],
            ),
            _attribute(
                "emails",
                "complex",
                multiValued=True,
                subAttributes=[
                    _attribute("value"),
                    _attribute("type"),
                    _attribute("primary", "boolean"),
                ],
            ),
            _attribute("active", "boolean"),
            _attribute("externalId"),
            _attribute("groups", "complex", multiValued=True, mutability="readOnly"),
        ],
    }
    group_schema = {
        "id": resources.GROUP_SCHEMA,
        "name": "Group",
        "attributes": [
            _attribute("displayName", required=True, uniqueness="server"),
            _attribute("externalId"),
            _attribute(
                "members",
                "complex",
                multiValued=True,
                subAttributes=[_attribute("value"), _attribute("display")],
            ),
        ],
    }
    return _json(
        {"schemas": [LIST_SCHEMA], "totalResults": 2, "Resources": [user_schema, group_schema]}
    )


# ---------------------------------------------------------------- administration


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: permission_required logs in
    return request.user


class TokenForm(forms.Form):
    """A token for one provisioning client."""

    name = forms.CharField(label=gettext_lazy("Name"), max_length=100)
    domains = forms.CharField(
        label=gettext_lazy("Email domains"),
        required=False,
        help_text=gettext_lazy("Comma separated. Empty: all domains."),
    )

    def clean_domains(self) -> list[str]:
        """Lower case, unique, plausible."""
        values = sorted(
            {d.strip().lower() for d in self.cleaned_data["domains"].split(",") if d.strip()}
        )
        if any("@" in d or "." not in d for d in values):
            raise forms.ValidationError(_("Enter domains like example.com."))
        return values


@feature_required("scim")
@permission_required("system.scim.manage")
def token_list(request: HttpRequest) -> HttpResponse:
    """SCIM tokens; create one (shown once)."""
    form = TokenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        prefix = secrets.token_hex(6)
        plain = f"pmsc_{prefix}_{secrets.token_urlsafe(32)}"
        ScimToken.objects.create(
            name=form.cleaned_data["name"],
            domains=form.cleaned_data["domains"],
            prefix=prefix,
            token_hash=digest(plain),
            created_by=_actor(request),
        )
        audit.record(
            "scim.token.created", request=request, data={"name": form.cleaned_data["name"]}
        )
        request.session[SHOW_ONCE] = plain
        return redirect("scim:tokens")
    return render(
        request,
        "scim/tokens.html",
        {
            "form": form,
            "tokens": ScimToken.objects.all(),
            "new_token": request.session.pop(SHOW_ONCE, None),
            "base_url": request.build_absolute_uri("/scim/v2/"),
        },
    )


@feature_required("scim")
@permission_required("system.scim.manage")
@require_POST
def token_revoke(request: HttpRequest, pk: str) -> HttpResponse:
    """Stop a token from working."""
    token = get_object_or_404(ScimToken, pk=pk, revoked_at__isnull=True)
    ScimToken.objects.filter(pk=token.pk).update(revoked_at=timezone.now())
    audit.record("scim.token.revoked", request=request, data={"name": token.name})
    messages.success(request, _("Token revoked."))
    return redirect("scim:tokens")
