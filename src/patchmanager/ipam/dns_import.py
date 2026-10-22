"""Import DNS zones: zone files (RFC 1035) and the APIs of DNS providers.

Provider tokens are used for the one request and never stored or logged. Only the
fixed API hosts of the providers are contacted, over verified TLS, with time and
size limits.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

import urllib3
from django.db import transaction
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.audit import services as audit
from patchmanager.core.fields import path_label
from patchmanager.ipam.models import DNSRecord, DNSZone

MAX_BYTES = 10 * 1024 * 1024
MAX_RECORDS = 20_000
MAX_PAGES = 50
TYPES = {t.value for t in DNSRecord.Type}
TIMEOUT = urllib3.Timeout(total=30, connect=10)


@dataclass
class Record:
    """One record as read, relative to the zone."""

    name: str
    type: str
    value: str
    ttl: int | None = None
    priority: int | None = None


@dataclass
class Zone:
    """A zone as read."""

    name: str
    records: list[Record] = field(default_factory=list)
    primary_server: str = ""
    admin_email: str = ""
    serial: int | None = None
    default_ttl: int | None = None
    skipped: list[str] = field(default_factory=list)


# --- Zone files -----------------------------------------------------------------

_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[^\s"]+')


def _relative(name: str, origin: str) -> str:
    """An owner name relative to the zone (``@`` for the zone itself)."""
    if name == "@":
        return "@"
    if name.endswith("."):
        absolute = name.rstrip(".").lower()
        if absolute == origin:
            return "@"
        if absolute.endswith("." + origin):
            return absolute[: -len(origin) - 1]
        return name  # outside the zone: kept absolute
    return name


def _entries(text: str) -> list[tuple[list[str], int, bool]]:
    """Logical lines as tokens (parentheses joined, comments removed), with "owner given"."""
    entries: list[tuple[list[str], int, bool]] = []
    tokens: list[str] = []
    depth = 0
    start = 0
    owner_given = True
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        found = []
        for token in _TOKEN.findall(line):
            if token.startswith(";"):
                break
            if ";" in token and not token.startswith('"'):
                found.append(token.split(";", 1)[0])
                break
            found.append(token)
        if not tokens:
            start = number
            owner_given = bool(line) and not line[0].isspace()
        for token in found:
            for part in re.split(r"([()])", token) if not token.startswith('"') else [token]:
                if part == "(":
                    depth += 1
                elif part == ")":
                    depth = max(0, depth - 1)
                elif part:
                    tokens.append(part)
        if depth == 0 and tokens:
            entries.append((tokens, start, owner_given))
            tokens = []
    return entries


def parse_zone_file(text: str, name: str) -> Zone:
    """Read a zone file for the zone ``name``."""
    if len(text) > MAX_BYTES or "\x00" in text:
        raise ServiceError(_("The file is not a zone file of at most 10 MB."))
    origin = name.rstrip(".").lower()
    zone = Zone(origin)
    ttl: int | None = None
    owner = "@"
    for tokens, number, owner_given in _entries(text):
        if tokens[0].upper() == "$ORIGIN" and len(tokens) == 2:
            origin = tokens[1].rstrip(".").lower()
            continue
        if tokens[0].upper() == "$TTL" and len(tokens) == 2 and tokens[1].isdigit():
            ttl = zone.default_ttl = int(tokens[1])
            continue
        if tokens[0].startswith("$"):
            zone.skipped.append(f"line {number}: {tokens[0]} is not supported")
            continue
        rest = tokens
        if owner_given:
            owner, rest = _relative(tokens[0], origin), tokens[1:]
        record_ttl = ttl
        while rest and (rest[0].isdigit() or rest[0].upper() in ("IN", "CH", "HS")):
            if rest[0].isdigit():
                record_ttl = int(rest[0])
            rest = rest[1:]
        if not rest:
            continue
        kind, data = rest[0].upper(), rest[1:]
        if kind == "SOA" and len(data) >= 3:
            zone.primary_server = data[0].rstrip(".")
            zone.admin_email = data[1].rstrip(".").replace(".", "@", 1)
            zone.serial = int(data[2]) if data[2].isdigit() else None
            continue
        if kind not in TYPES:
            zone.skipped.append(f"line {number}: type {kind[:10]} is not documented")
            continue
        if len(zone.records) >= MAX_RECORDS:
            raise ServiceError(_("The zone has more than 20 000 records."))
        priority = None
        if kind in ("MX", "SRV") and data and data[0].isdigit():
            priority, data = int(data[0]), data[1:]
        if kind in ("TXT", "CAA"):
            value = (
                "".join(t[1:-1] if t.startswith('"') else t for t in data)
                if kind == "TXT"
                else " ".join(data)
            )
        else:
            value = " ".join(data)
        zone.records.append(
            Record(owner, kind, value, record_ttl if record_ttl != ttl else None, priority)
        )
    return zone


# --- Providers --------------------------------------------------------------------


def _get(url: str, headers: dict[str, str]) -> Any:
    """GET JSON from a provider API (fixed host, verified TLS, bounded size)."""
    http = urllib3.PoolManager(timeout=TIMEOUT, retries=False, cert_reqs="CERT_REQUIRED")
    try:
        response = http.request("GET", url, headers=headers, redirect=False, preload_content=False)
        body = response.read(MAX_BYTES + 1)
        response.release_conn()
    except urllib3.exceptions.HTTPError as exc:
        raise ServiceError(_("The provider could not be reached.")) from exc
    if response.status in (401, 403):
        raise ServiceError(_("The provider refused the token."))
    if response.status == 404:
        raise ServiceError(_("The provider does not know this zone."))
    if response.status != 200 or len(body) > MAX_BYTES:
        raise ServiceError(
            _("The provider answered with an error (%(status)s).") % {"status": response.status}
        )
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ServiceError(_("The provider sent an answer that is not JSON.")) from exc


def _host_value(kind: str, value: str) -> str:
    """Host names from APIs are absolute: they get the trailing dot of zone files."""
    if kind in ("CNAME", "MX", "NS", "PTR") and value and not value.endswith("."):
        return value + "."
    return value


def _desec(name: str, token: str) -> Zone:
    data = _get(
        f"https://desec.io/api/v1/domains/{name}/rrsets/", {"Authorization": f"Token {token}"}
    )
    zone = Zone(name)
    for rrset in data if isinstance(data, list) else []:
        kind = str(rrset.get("type", "")).upper()
        if kind not in TYPES:
            continue
        owner = rrset.get("subname") or "@"
        for value in rrset.get("records", [])[:MAX_RECORDS]:
            priority = None
            text = str(value)
            if kind in ("MX", "SRV") and text.split(" ", 1)[0].isdigit():
                first, text = text.split(" ", 1)
                priority = int(first)
            if kind == "TXT":
                text = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', text)) or text
            zone.records.append(Record(owner, kind, text, rrset.get("ttl"), priority))
    return zone


def _cloudflare(name: str, token: str) -> Zone:
    headers = {"Authorization": f"Bearer {token}"}
    found = _get(f"https://api.cloudflare.com/client/v4/zones?name={name}", headers)
    results = found.get("result") or []
    if not results:
        raise ServiceError(_("The provider does not know this zone."))
    zone_id = re.sub(r"[^0-9a-f]", "", str(results[0].get("id", "")))
    zone = Zone(name)
    for page in range(1, MAX_PAGES + 1):
        data = _get(
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?per_page=1000&page={page}",
            headers,
        )
        for item in data.get("result") or []:
            kind = str(item.get("type", "")).upper()
            if kind in TYPES:
                owner = _relative(str(item.get("name", "")) + ".", name)
                ttl = item.get("ttl") if item.get("ttl") != 1 else None  # 1 means "automatic"
                zone.records.append(
                    Record(
                        owner,
                        kind,
                        _host_value(kind, str(item.get("content", ""))),
                        ttl,
                        item.get("priority"),
                    )
                )
        info = data.get("result_info") or {}
        if page >= int(info.get("total_pages") or 1):
            break
    return zone


def _hetzner(name: str, token: str) -> Zone:
    headers = {"Auth-API-Token": token}
    found = _get(f"https://dns.hetzner.com/api/v1/zones?name={name}", headers)
    zones = found.get("zones") or []
    if not zones:
        raise ServiceError(_("The provider does not know this zone."))
    zone_id = re.sub(r"[^0-9A-Za-z]", "", str(zones[0].get("id", "")))
    data = _get(f"https://dns.hetzner.com/api/v1/records?zone_id={zone_id}", headers)
    zone = Zone(name)
    for item in data.get("records") or []:
        kind = str(item.get("type", "")).upper()
        if kind not in TYPES:
            continue
        value = str(item.get("value", ""))
        priority = None
        if kind in ("MX", "SRV") and value.split(" ", 1)[0].isdigit():
            first, value = value.split(" ", 1)
            priority = int(first)
        if kind == "TXT":
            value = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', value)) or value
        zone.records.append(Record(item.get("name") or "@", kind, value, item.get("ttl"), priority))
    return zone


PROVIDERS = {
    "desec": ("deSEC", _desec),
    "cloudflare": ("Cloudflare", _cloudflare),
    "hetzner": ("Hetzner DNS", _hetzner),
}
_ZONE_NAME = re.compile(r"^(?=.{1,253}$)([a-z0-9_](-*[a-z0-9_])*\.)+[a-z0-9](-*[a-z0-9])*$")


def fetch(provider: str, name: str, token: str) -> Zone:
    """A zone from a provider's API."""
    name = name.strip().rstrip(".").lower()
    if not _ZONE_NAME.match(name):
        raise ServiceError(_("Enter a domain name like example.com."))
    if provider not in PROVIDERS or not token.strip() or any(c in token for c in "\r\n"):
        raise ServiceError(_("Choose a provider and enter its token."))
    return PROVIDERS[provider][1](name, token.strip())


# --- Saving ------------------------------------------------------------------------


@dataclass
class Imported:
    """What an import did."""

    zone: DNSZone
    created: bool
    records: int
    skipped: list[str]


def save(
    request: HttpRequest, actor: User, tenant: Any, data: Zone, *, replace: bool, source: str
) -> Imported:
    """Create or update the zone and its records (with ``replace``, existing ones go first)."""
    stored = DNSZone.objects.filter(tenant=tenant, name__iexact=data.name).first()
    action = "change" if stored else "add"
    allowed = (
        policy.has_perm(actor, "ipam.dnszone.change", stored)
        if stored
        else policy.has_perm(actor, "ipam.dnszone.add", scope_path=path_label(tenant.pk))
    )
    if not allowed:
        raise PermissionDeniedError(_("You do not have permission to do that."))
    skipped = list(data.skipped)
    with transaction.atomic():
        zone = stored or DNSZone(tenant=tenant, created_by=actor, name=data.name)
        for key in ("primary_server", "admin_email"):
            if getattr(data, key):
                setattr(zone, key, getattr(data, key)[:253])
        if data.serial is not None:
            zone.serial = data.serial
        if data.default_ttl:
            zone.default_ttl = data.default_ttl
        zone.full_clean(exclude=["tenant"])
        zone.save()
        if replace:
            zone.records.all().delete()
        existing = {(r.name, r.type, r.value) for r in zone.records.all()}
        count = 0
        for item in data.records:
            if (item.name, item.type, item.value) in existing:
                continue
            record = DNSRecord(
                tenant=tenant,
                zone=zone,
                name=item.name[:253],
                type=item.type,
                value=item.value[:1000],
                ttl=item.ttl,
                priority=item.priority,
            )
            try:
                record.full_clean(exclude=["tenant"])
            except Exception as exc:  # report the record and go on
                messages = getattr(exc, "messages", [str(exc)])
                skipped.append(f"{item.name} {item.type}: {'; '.join(messages)}")
                continue
            record.save()
            existing.add((item.name, item.type, item.value))
            count += 1
    audit.record(
        "ipam.dnszone.imported",
        request=request,
        obj=zone,
        data={"source": source, "records": count, "replace": replace, "action": action},
    )
    return Imported(zone, stored is None, count, skipped[:200])
