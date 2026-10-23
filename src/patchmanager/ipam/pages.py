"""Pages of DHCP scopes and DNS zones (through the shared record pages), and exports."""

import ipaddress
import json
from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from patchmanager.accounts import policy
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.core import crud
from patchmanager.core.fields import path_label
from patchmanager.directory.pages import TenantForm
from patchmanager.ipam import dns_import
from patchmanager.ipam.models import DHCPReservation, DHCPScope, DNSRecord, DNSZone


class ScopeForm(TenantForm):
    """A DHCP scope."""

    tenant_fields = ("prefix", "server")

    class Meta:
        model = DHCPScope
        fields = [
            "name",
            "prefix",
            "server",
            "start",
            "end",
            "gateway",
            "dns_servers",
            "domain_name",
            "lease_seconds",
            "relays",
            "options",
            "enabled",
            "description",
        ]
        widgets = {
            "options": forms.Textarea(attrs={"rows": 3, "class": "font-mono"}),
            "description": forms.Textarea(attrs={"rows": 2}),
        }


class ReservationForm(TenantForm):
    """A DHCP reservation."""

    tenant_fields = ("scope",)

    class Meta:
        model = DHCPReservation
        fields = ["scope", "mac_address", "address", "hostname", "description"]


class ZoneForm(TenantForm):
    """A DNS zone."""

    tenant_fields = ("server",)

    class Meta:
        model = DNSZone
        fields = [
            "name",
            "primary_server",
            "admin_email",
            "default_ttl",
            "serial",
            "server",
            "description",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}


class RecordForm(TenantForm):
    """A DNS record."""

    tenant_fields = ("zone",)

    class Meta:
        model = DNSRecord
        fields = ["zone", "name", "type", "value", "ttl", "priority"]


def _scope_extra(request: HttpRequest, scope: Any) -> dict[str, Any]:
    network = ipaddress.ip_network(str(scope.prefix.prefix))
    first = ipaddress.ip_address(str(scope.start).split("/")[0])
    last = ipaddress.ip_address(str(scope.end).split("/")[0])
    return {
        "export_url": reverse("ipam:dhcp-export", args=[scope.pk]),
        "export_label": _("Kea configuration"),
        "facts": [
            (_("Prefix"), str(network)),
            (_("Addresses in the range"), int(last) - int(first) + 1),
            (_("Reservations"), scope.reservations.count()),
            (_("Options"), ", ".join(f"{c}={v}" for c, v in scope.option_list()) or "-"),
        ],
    }


def _zone_extra(request: HttpRequest, zone: Any) -> dict[str, Any]:
    return {
        "export_url": reverse("ipam:dns-export", args=[zone.pk]),
        "export_label": _("Zone file"),
    }


crud.register(
    crud.Resource(
        key="dhcp",
        model=DHCPScope,
        form=ScopeForm,
        title=gettext_lazy("DHCP scopes"),
        new=gettext_lazy("New DHCP scope"),
        group="services",
        columns=(
            (gettext_lazy("Name"), "name"),
            (gettext_lazy("Prefix"), "prefix"),
            (gettext_lazy("First address"), "start"),
            (gettext_lazy("Last address"), "end"),
            (gettext_lazy("DHCP server"), "server"),
            (gettext_lazy("Enabled"), "enabled"),
        ),
        children=("dhcp-reservations",),
        context=_scope_extra,
        template="ipam/_facts.html",
    )
)
crud.register(
    crud.Resource(
        key="dhcp-reservations",
        model=DHCPReservation,
        form=ReservationForm,
        title=gettext_lazy("Reservations"),
        new=gettext_lazy("New reservation"),
        group="services",
        columns=(
            (gettext_lazy("Address"), "address"),
            (gettext_lazy("MAC address"), "mac_address"),
            (gettext_lazy("Hostname"), "hostname"),
            (gettext_lazy("Description"), "description"),
        ),
        parent=("scope", "dhcp"),
        hidden=True,
        order=("address",),
    )
)
crud.register(
    crud.Resource(
        key="dns",
        model=DNSZone,
        form=ZoneForm,
        title=gettext_lazy("DNS zones"),
        new=gettext_lazy("New DNS zone"),
        group="services",
        columns=(
            (gettext_lazy("Name"), "name"),
            (gettext_lazy("Primary name server"), "primary_server"),
            (gettext_lazy("DNS server"), "server"),
            (gettext_lazy("Serial"), "serial"),
        ),
        children=("dns-records",),
        context=_zone_extra,
        actions=((gettext_lazy("Import zone"), "ipam:dns-import"),),
    )
)
crud.register(
    crud.Resource(
        key="dns-records",
        model=DNSRecord,
        form=RecordForm,
        title=gettext_lazy("Records"),
        new=gettext_lazy("New record"),
        group="services",
        columns=(
            (gettext_lazy("Name"), "name"),
            (gettext_lazy("Type"), "type"),
            (gettext_lazy("Value"), "value"),
            (gettext_lazy("Priority"), "priority"),
            (gettext_lazy("TTL (seconds)"), "ttl"),
        ),
        parent=("zone", "dns"),
        hidden=True,
        order=("name", "type"),
    )
)


# --- Exports ---------------------------------------------------------------------


def _quote(value: str) -> str:
    """A TXT value as one quoted string (quotes and backslashes escaped)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _word(value: str) -> str:
    """One token of a zone file: no white space, control characters or comment marks."""
    return "".join(c for c in value if c.isprintable() and not c.isspace() and c not in ';"()')


def zone_file(zone: DNSZone) -> str:
    """The zone in the master file format (RFC 1035)."""
    origin = _word(zone.name).rstrip(".") + "."
    mail = _word(zone.admin_email or f"hostmaster@{zone.name}").replace("@", ".", 1)
    mail = mail.rstrip(".") + "."
    primary = _word(zone.primary_server or f"ns1.{zone.name}").rstrip(".") + "."
    lines = [
        f"$ORIGIN {origin}",
        f"$TTL {zone.default_ttl}",
        f"@ IN SOA {primary} {mail} ({zone.serial} 3600 900 1209600 300)",
    ]
    for record in zone.records.all():
        value = (
            _quote(record.value)
            if record.type in ("TXT", "CAA") and not record.value.startswith('"')
            else record.value
        )
        priority = f"{record.priority} " if record.priority is not None else ""
        ttl = f"{record.ttl} " if record.ttl else ""
        if record.type not in ("TXT", "CAA", "SRV"):
            value = _word(value)
        lines.append(f"{_word(record.name)} {ttl}IN {record.type} {priority}{value}")
    return "\n".join(lines) + "\n"


def kea_subnet(scope: DHCPScope) -> dict[str, Any]:
    """The scope as a Kea DHCPv4/v6 subnet."""
    options: list[dict[str, Any]] = []
    if scope.gateway:
        options.append({"name": "routers", "data": str(scope.gateway).split("/")[0]})
    if scope.dns_servers:
        options.append({"name": "domain-name-servers", "data": scope.dns_servers.replace(" ", "")})
    if scope.domain_name:
        options.append({"name": "domain-name", "data": scope.domain_name})
    options += [{"code": code, "data": value} for code, value in scope.option_list()]
    return {
        "subnet": str(scope.prefix.prefix),
        "pools": [{"pool": f"{str(scope.start).split('/')[0]} - {str(scope.end).split('/')[0]}"}],
        "valid-lifetime": scope.lease_seconds,
        "option-data": options,
        "reservations": [
            {
                "hw-address": r.mac_address,
                "ip-address": str(r.address).split("/")[0],
                **({"hostname": r.hostname} if r.hostname else {}),
            }
            for r in scope.reservations.all()
        ],
    }


def _download(text: str, name: str, content_type: str) -> HttpResponse:
    response = HttpResponse(text, content_type=f"{content_type}; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


@login_required
def dns_export(request: HttpRequest, pk: str) -> HttpResponse:
    """A zone as a BIND zone file."""
    zone = get_object_or_404(DNSZone, pk=pk)
    if not policy.has_perm(request.user, "ipam.dnszone.view", zone):
        raise Http404
    return _download(zone_file(zone), f"{slugify(zone.name) or 'zone'}.zone", "text/plain")


@login_required
def dhcp_export(request: HttpRequest, pk: str) -> HttpResponse:
    """A scope as a Kea subnet (JSON)."""
    scope = get_object_or_404(DHCPScope.objects.select_related("prefix"), pk=pk)
    if not policy.has_perm(request.user, "ipam.dhcpscope.view", scope):
        raise Http404
    family = "subnet6" if ":" in str(scope.prefix.prefix) else "subnet4"
    text = json.dumps({family: [kea_subnet(scope)]}, indent=2)
    return _download(text, f"{slugify(scope.name) or 'scope'}.json", "application/json")


# --- Import ------------------------------------------------------------------------


class ZoneImportForm(forms.Form):
    """A zone file, or a provider with its API token."""

    name = forms.CharField(
        label=gettext_lazy("Zone"),
        max_length=253,
        help_text=gettext_lazy("For example example.com."),
    )
    source = forms.ChoiceField(
        label=gettext_lazy("Source"),
        choices=[("file", gettext_lazy("zone file"))]
        + [(key, label) for key, (label, _fetch) in dns_import.PROVIDERS.items()],
    )
    content = forms.CharField(
        label=gettext_lazy("Zone file"),
        widget=forms.Textarea(attrs={"rows": 10, "spellcheck": "false", "class": "font-mono"}),
        required=False,
        strip=False,
        help_text=gettext_lazy("Paste it here, or choose a file."),
    )
    file = forms.FileField(label=gettext_lazy("or a file"), required=False)
    token = forms.CharField(
        label=gettext_lazy("API token"),
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text=gettext_lazy(
            "Used once to read the zone; it is not stored. A read-only token is enough."
        ),
    )
    replace = forms.BooleanField(
        label=gettext_lazy("Replace the documented records of this zone"), required=False
    )

    def clean(self) -> dict[str, Any]:
        """A zone file for "zone file", a token for a provider."""
        data = super().clean() or {}
        if data.get("source") == "file":
            upload = data.get("file")
            if upload is not None:
                if upload.size > dns_import.MAX_BYTES:
                    raise forms.ValidationError(_("The file is not a zone file of at most 10 MB."))
                try:
                    data["content"] = upload.read().decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise forms.ValidationError(_("The file must be UTF-8 text.")) from exc
            if not data.get("content", "").strip():
                raise forms.ValidationError(_("Paste a zone file or choose one."))
        elif not data.get("token"):
            raise forms.ValidationError(_("Choose a provider and enter its token."))
        return data


@login_required
@require_http_methods(["GET", "POST"])
def dns_import_view(request: HttpRequest) -> HttpResponse:
    """Import a zone from a file or from a DNS provider."""
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    actor: Any = request.user
    if not all(
        policy.has_perm(actor, f"ipam.dnszone.{action}", scope_path=path_label(tenant.pk))
        for action in ("add", "import")
    ):
        raise PermissionDenied
    form = ZoneImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            if data["source"] == "file":
                zone = dns_import.parse_zone_file(data["content"], data["name"])
            else:
                zone = dns_import.fetch(data["source"], data["name"], data["token"])
            result = dns_import.save(
                request, actor, tenant, zone, replace=data["replace"], source=data["source"]
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except (ServiceError, ValidationError) as exc:
            form = ZoneImportForm({k: v for k, v in request.POST.items() if k != "token"})
            form.is_valid()
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(request, _("%(n)s records imported.") % {"n": result.records})
            for note in result.skipped[:10]:
                messages.warning(request, note)
            return redirect("objects:detail", "dns", result.zone.pk)
    return render(
        request,
        "core/create.html",
        {
            "title": _("Import a DNS zone"),
            "intro": _(
                "Read a zone file, or a zone of deSEC, Cloudflare or Hetzner DNS through their API."
            ),
            "form": form,
            "back_url": reverse("objects:index", args=["dns"]),
            "multipart": True,
            "submit": _("Import"),
        },
    )
