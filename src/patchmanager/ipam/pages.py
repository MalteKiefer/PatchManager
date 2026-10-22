"""Pages of DHCP scopes and DNS zones (through the shared record pages), and exports."""

import ipaddress
import json
from typing import Any

from django import forms
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from patchmanager.accounts import policy
from patchmanager.core import crud
from patchmanager.directory.pages import TenantForm
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


def zone_file(zone: DNSZone) -> str:
    """The zone in the master file format (RFC 1035)."""
    origin = zone.name.rstrip(".") + "."
    mail = (zone.admin_email or f"hostmaster@{zone.name}").replace("@", ".").rstrip(".") + "."
    primary = (zone.primary_server or f"ns1.{zone.name}").rstrip(".") + "."
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
        lines.append(f"{record.name} {ttl}IN {record.type} {priority}{value}")
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
