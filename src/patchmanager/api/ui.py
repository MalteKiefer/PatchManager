"""Pages for personal API tokens and OAuth clients."""

from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from patchmanager.accounts.decorators import permission_required
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.api import tokens
from patchmanager.api.models import SCOPES, ApiClient, ApiToken

SHOW_ONCE = "_pm_api_secret"
DAYS = [
    (7, gettext_lazy("7 days")),
    (30, gettext_lazy("30 days")),
    (90, gettext_lazy("90 days")),
    (365, gettext_lazy("1 year")),
]


class LimitsMixin(forms.Form):
    """Optional limits: object types and one location (both narrow the token further)."""

    object_types = forms.MultipleChoiceField(
        label=gettext_lazy("Only these kinds of objects"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=gettext_lazy("Leave all empty to allow every kind you may use yourself."),
    )
    scope = forms.ChoiceField(
        label=gettext_lazy("Only this location and what is in it"),
        required=False,
        help_text=gettext_lazy("A site, building, floor or room; empty means everywhere."),
    )

    def limit_choices(self, actor: Any) -> None:
        """Object types of the registry and the locations the user may see."""
        from patchmanager.accounts import permissions, policy
        from patchmanager.locations.models import Building, Floor, Room, Site

        types: Any = self.fields["object_types"]
        types.choices = [(ot.code, f"{ot.label} ({ot.code})") for ot in permissions.object_types()]
        self.locations: dict[str, Any] = {}
        for model in (Site, Building, Floor, Room):
            for node in policy.scope_queryset(
                actor, f"{model.policy_object_type}.view", model.objects.all()
            ).order_by("path")[:2000]:
                self.locations[f"{model.__name__.lower()}:{node.pk}"] = node
        scope: Any = self.fields["scope"]
        scope.choices = [("", _("everywhere"))] + [
            (key, " / ".join([*(a.name for a in node.ancestors()[1:]), node.name]))
            for key, node in sorted(self.locations.items(), key=lambda kv: str(kv[1].path))
        ]

    def chosen_scope(self) -> Any:
        """The chosen location object (or None)."""
        return self.locations.get(self.cleaned_data.get("scope") or "")


class TokenForm(LimitsMixin):
    """A new personal token."""

    name = forms.CharField(label=gettext_lazy("Name"), max_length=100)
    scopes = forms.MultipleChoiceField(
        label=gettext_lazy("Access"),
        choices=SCOPES,
        initial=["read"],
        widget=forms.CheckboxSelectMultiple,
    )
    days = forms.TypedChoiceField(
        label=gettext_lazy("Expires after"), choices=DAYS, coerce=int, initial=90
    )
    tenant = forms.ChoiceField(label=gettext_lazy("Tenant"), required=False)

    def __init__(self, *args: object, tenants: list[object], **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.tenants = {str(t.pk): t for t in tenants}  # type: ignore[attr-defined]
        field = self.fields["tenant"]
        assert isinstance(field, forms.ChoiceField)  # noqa: S101  # nosec B101: declared above
        field.choices = [("", _("All my tenants")), *((k, str(t)) for k, t in self.tenants.items())]


class ClientForm(LimitsMixin):
    """A new OAuth client."""

    name = forms.CharField(label=gettext_lazy("Name"), max_length=100)
    scopes = forms.MultipleChoiceField(
        label=gettext_lazy("Access"),
        choices=SCOPES,
        initial=["read"],
        widget=forms.CheckboxSelectMultiple,
    )


def _user(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


@login_required
def token_list(request: HttpRequest) -> HttpResponse:
    """The user's personal tokens."""
    user = _user(request)
    if user.is_service:
        raise PermissionDenied
    return render(
        request,
        "api/tokens.html",
        {
            "new_secret": request.session.pop(SHOW_ONCE, None),
            "tokens": user.api_tokens.filter(kind=ApiToken.Kind.PERSONAL).select_related("tenant"),
        },
    )


@login_required
def token_create(request: HttpRequest) -> HttpResponse:
    """Page that creates a personal token; the list shows it once."""
    user = _user(request)
    if user.is_service:
        raise PermissionDenied
    tenants = list(getattr(request, "available_tenants", []))
    form = TokenForm(request.POST or None, tenants=tenants)
    form.limit_choices(user)
    if request.method == "POST" and form.is_valid():
        tenant = (
            form.tenants.get(form.cleaned_data["tenant"]) if form.cleaned_data["tenant"] else None
        )
        try:
            _token, plain = tokens.create_token(
                request,
                user,
                name=form.cleaned_data["name"],
                scopes=form.cleaned_data["scopes"],
                days=form.cleaned_data["days"],
                tenant=tenant,
                object_types=form.cleaned_data["object_types"],
                scope=form.chosen_scope(),
            )
        except (ServiceError, PermissionDeniedError) as exc:
            messages.error(request, str(exc))
        else:
            request.session[SHOW_ONCE] = {"token": plain}
            return redirect("api-ui:tokens")
    return render(
        request,
        "core/create.html",
        {"title": _("New token"), "form": form, "back_url": reverse("api-ui:tokens")},
    )


@login_required
@require_POST
def token_revoke(request: HttpRequest, pk: str) -> HttpResponse:
    """Revoke one of the user's tokens."""
    token = get_object_or_404(ApiToken, pk=pk, user=request.user)
    tokens.revoke_token(request, _user(request), token)
    messages.success(request, _("Token revoked."))
    return redirect("api-ui:tokens")


@permission_required("accounts.apiclient.manage")
def client_list(request: HttpRequest) -> HttpResponse:
    """OAuth clients."""
    return render(
        request,
        "api/clients.html",
        {
            "new_secret": request.session.pop(SHOW_ONCE, None),
            "clients": ApiClient.objects.select_related("user"),
        },
    )


@permission_required("accounts.apiclient.manage")
def client_create(request: HttpRequest) -> HttpResponse:
    """Page that creates an OAuth client; the list shows its secret once."""
    form = ClientForm(request.POST or None)
    form.limit_choices(_user(request))
    if request.method == "POST" and form.is_valid():
        try:
            client, secret = tokens.create_client(
                request,
                _user(request),
                name=form.cleaned_data["name"],
                scopes=form.cleaned_data["scopes"],
                object_types=form.cleaned_data["object_types"],
                scope=form.chosen_scope(),
            )
        except (ServiceError, PermissionDeniedError) as exc:
            messages.error(request, str(exc))
        else:
            request.session[SHOW_ONCE] = {"client_id": client.client_id, "client_secret": secret}
            return redirect("api-ui:clients")
    return render(
        request,
        "core/create.html",
        {"title": _("New API client"), "form": form, "back_url": reverse("api-ui:clients")},
    )


@permission_required("accounts.apiclient.manage")
@require_POST
def client_action(request: HttpRequest, pk: str, action: str) -> HttpResponse:
    """Rotate the secret of, or revoke, a client."""
    client = get_object_or_404(ApiClient, pk=pk, revoked_at__isnull=True)
    if action == "rotate":
        secret = tokens.rotate_secret(request, _user(request), client)
        request.session[SHOW_ONCE] = {"client_id": client.client_id, "client_secret": secret}
    elif action == "revoke":
        tokens.revoke_client(request, _user(request), client)
        messages.success(request, _("Client revoked."))
    else:
        raise PermissionDenied
    return redirect("api-ui:clients")
