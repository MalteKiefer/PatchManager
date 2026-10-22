"""Store, read, compare and delete device configuration versions.

Configurations often hold secrets (community strings, password hashes, keys), so
the text is encrypted at rest and every read is audited.
"""

import difflib
import hashlib
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Max
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.configs.models import DeviceConfig
from patchmanager.core import crypto

MAX_BYTES = 2 * 1024 * 1024
DIFF_CONTEXT = 3


def _check(content: str) -> str:
    text = content.replace("\r\n", "\n")
    if not text.strip():
        raise ServiceError(_("The configuration is empty."))
    if "\x00" in text:
        raise ServiceError(_("The configuration must be text, not a binary file."))
    if len(text.encode()) > MAX_BYTES:
        raise ServiceError(_("The configuration is larger than 2 MB."))
    return text


def add(
    request: HttpRequest,
    actor: User,
    device: Any,
    content: str,
    *,
    source: str = DeviceConfig.Source.MANUAL,
    comment: str = "",
) -> tuple[DeviceConfig, bool]:
    """Store a new version unless it equals the latest. Returns the version and if it is new."""
    probe = DeviceConfig(device=device, tenant_id=device.tenant_id, path=device.path)
    require(actor, "configs.config.add", probe)
    text = _check(content)
    digest = hashlib.sha256(text.encode()).hexdigest()
    with transaction.atomic():
        # Serialise versions per device: lock the device row.
        type(device).objects.select_for_update().filter(pk=device.pk).first()
        latest = DeviceConfig.objects.filter(device=device).order_by("-version").first()
        if latest is not None and latest.sha256 == digest:
            return latest, False
        number = (
            DeviceConfig.objects.filter(device=device).aggregate(n=Max("version"))["n"] or 0
        ) + 1
        config = DeviceConfig.objects.create(
            tenant_id=device.tenant_id,
            created_by=actor,
            device=device,
            version=number,
            sha256=digest,
            size=len(text.encode()),
            lines=text.count("\n") + (0 if text.endswith("\n") else 1),
            source=source,
            comment=comment[:200],
            content=crypto.encrypt(text),
            path=device.path,
        )
    audit.record(
        "configs.config.added",
        request=request,
        obj=device,
        data={"version": number, "sha256": digest, "source": source},
    )
    return config, True


def read(request: HttpRequest, actor: User, config: DeviceConfig) -> str:
    """The text of a version; the read is audited."""
    require(actor, "configs.config.view", config)
    audit.record(
        "configs.config.read",
        request=request,
        obj=config.device,
        data={"version": config.version},
    )
    return crypto.decrypt(config.content)


def delete(request: HttpRequest, actor: User, config: DeviceConfig) -> None:
    """Delete one version."""
    require(actor, "configs.config.delete", config)
    config.delete()
    audit.record(
        "configs.config.deleted",
        request=request,
        obj=config.device,
        data={"version": config.version},
    )


@dataclass(frozen=True)
class DiffLine:
    """One line of a unified diff."""

    kind: str  # "add", "remove", "context" or "hunk"
    text: str
    old: int | None = None
    new: int | None = None


def diff(old: str, new: str) -> tuple[list[DiffLine], int, int]:
    """Unified diff with line numbers, and the counts of added and removed lines."""
    a, b = old.splitlines(), new.splitlines()
    lines: list[DiffLine] = []
    added = removed = 0
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for group in matcher.get_grouped_opcodes(DIFF_CONTEXT):
        first, last = group[0], group[-1]
        lines.append(
            DiffLine(
                "hunk",
                f"@@ -{first[1] + 1},{last[2] - first[1]} +{first[3] + 1},{last[4] - first[3]} @@",
            )
        )
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines += [
                    DiffLine("context", a[i], i + 1, j1 + k + 1)
                    for k, i in enumerate(range(i1, i2))
                ]
                continue
            if tag in ("replace", "delete"):
                lines += [DiffLine("remove", a[i], i + 1, None) for i in range(i1, i2)]
                removed += i2 - i1
            if tag in ("replace", "insert"):
                lines += [DiffLine("add", b[j], None, j + 1) for j in range(j1, j2)]
                added += j2 - j1
    return lines, added, removed
