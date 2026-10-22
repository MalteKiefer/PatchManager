"""Webhook pages of the current tenant."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.instance.gates import feature_required
from patchmanager.integrations import services
from patchmanager.integrations.models import Delivery, Webhook
from patchmanager.tenancy.models import Tenant

SHOW_ONCE = "_pm_webhook_secret"


class WebhookForm(forms.Form):
    """URL and the events to send."""

    name = forms.CharField(label=gettext_lazy("Name"), max_length=100)
    url = forms.URLField(label=gettext_lazy("URL"), max_length=500, assume_scheme="https")
    events = forms.CharField(
        label=gettext_lazy("Events"),
        initial="*",
        help_text=gettext_lazy(
            "Comma separated, for example dcim.device.*, cabling.cable.insert or *"
        ),
    )
    active = forms.BooleanField(label=gettext_lazy("Active"), required=False, initial=True)


def _user(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _tenant(request: HttpRequest) -> Tenant:
    tenant = getattr(request, "tenant", None)
    if not isinstance(tenant, Tenant) or not policy.has_perm(
        request.user, "integrations.webhook.manage", tenant
    ):
        raise PermissionDenied
    return tenant


def _hook(request: HttpRequest, pk: str) -> Webhook:
    hook = get_object_or_404(Webhook, pk=pk)  # RLS bounds the tenant
    if not policy.has_perm(request.user, "integrations.webhook.manage", hook):
        raise Http404
    return hook


def _events(value: str) -> list[str]:
    return [e.strip() for e in value.split(",")]


@feature_required("webhooks")
@login_required
def webhook_list(request: HttpRequest) -> HttpResponse:
    """Webhooks of the tenant; create one."""
    tenant = _tenant(request)
    form = WebhookForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            hook, secret = services.create(
                request,
                _user(request),
                tenant,
                name=form.cleaned_data["name"],
                url=form.cleaned_data["url"],
                events=_events(form.cleaned_data["events"]),
                active=form.cleaned_data["active"],
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            request.session[SHOW_ONCE] = secret
            return redirect("integrations:webhook", hook.pk)
    return render(
        request,
        "integrations/webhooks.html",
        {
            "form": form,
            "webhooks": Webhook.objects.filter(tenant=tenant),
            "can_manage": policy.has_perm(request.user, "integrations.webhook.manage", tenant),
        },
    )


@feature_required("webhooks")
@login_required
def webhook_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Settings, secret (once) and recent deliveries."""
    hook = _hook(request, pk)
    form = WebhookForm(
        request.POST or None,
        initial={
            "name": hook.name,
            "url": hook.url,
            "events": ", ".join(hook.events),
            "active": hook.active,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.update(
                request,
                _user(request),
                hook,
                name=form.cleaned_data["name"],
                url=form.cleaned_data["url"],
                events=_events(form.cleaned_data["events"]),
                active=form.cleaned_data["active"],
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Webhook saved."))
            return redirect("integrations:webhook", hook.pk)
    return render(
        request,
        "integrations/webhook.html",
        {
            "hook": hook,
            "form": form,
            "secret": request.session.pop(SHOW_ONCE, None),
            "deliveries": hook.deliveries.all()[:50],
            "can_manage": policy.has_perm(request.user, "integrations.webhook.manage", hook.tenant),
        },
    )


@feature_required("webhooks")
@login_required
@require_POST
def webhook_action(request: HttpRequest, pk: str, action: str) -> HttpResponse:
    """Ping, rotate the secret or delete."""
    hook = _hook(request, pk)
    try:
        if action == "ping":
            services.ping(request, _user(request), hook)
            messages.success(request, _("Test event queued."))
        elif action == "rotate":
            request.session[SHOW_ONCE] = services.rotate(request, _user(request), hook)
        elif action == "delete":
            services.delete(request, _user(request), hook)
            messages.success(request, _("Webhook deleted."))
            return redirect("integrations:webhooks")
        else:
            raise Http404
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    return redirect("integrations:webhook", hook.pk)


@feature_required("webhooks")
@login_required
@require_POST
def redeliver(request: HttpRequest, pk: str) -> HttpResponse:
    """Queue one delivery again."""
    delivery = get_object_or_404(Delivery.objects.select_related("webhook__tenant"), pk=pk)
    try:
        services.redeliver(request, _user(request), delivery)
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    messages.success(request, _("Delivery queued again."))
    return redirect("integrations:webhook", delivery.webhook_id)
