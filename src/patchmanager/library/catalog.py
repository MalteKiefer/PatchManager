"""The bundled device type catalog (community device type library, CC0-1.0).

``catalog.json.gz`` is produced by ``scripts/catalog.py``. It is loaded into
``CatalogType`` after migrations whenever its source commit changes, so the
catalog works on installations without internet access.
"""

import gzip
import json
from collections import Counter
from pathlib import Path
from typing import Any

from django.db import transaction
from django.db.models import QuerySet, Value
from django.db.models.functions import Concat
from django.http import HttpRequest

from patchmanager.accounts.models import User
from patchmanager.library import interchange
from patchmanager.library.models import CatalogType, DeviceType
from patchmanager.tenancy.models import Tenant

FILE = Path(__file__).with_name("catalog.json.gz")
SECTIONS = {
    "interfaces": "network",
    "front-ports": "front",
    "rear-ports": "rear",
    "console-ports": "console",
    "power-ports": "power",
}


def _summary(spec: dict[str, Any]) -> tuple[int, str]:
    counts = Counter({label: len(spec.get(key) or []) for key, label in SECTIONS.items()})
    total = sum(counts.values())
    return total, ", ".join(f"{n} {label}" for label, n in counts.items() if n)


def load(*, force: bool = False) -> int:
    """Replace the catalog with the bundled file if it is newer. Returns the number loaded."""
    payload = json.loads(gzip.decompress(FILE.read_bytes()))
    commit = payload["commit"]
    if not force and CatalogType.objects.filter(commit=commit).exists():
        return 0
    rows = []
    seen: set[str] = set()
    for spec in payload["types"]:
        slug = str(spec.get("slug") or f"{spec['manufacturer']}-{spec['model']}")[:200]
        if slug in seen:
            continue
        seen.add(slug)
        ports, summary = _summary(spec)
        height = spec.get("u_height", 1)
        rows.append(
            CatalogType(
                slug=slug,
                manufacturer=str(spec["manufacturer"])[:100],
                model=str(spec["model"])[:150],
                part_number=str(spec.get("part_number") or "")[:100],
                u_height=float(height) if isinstance(height, int | float) else 0,
                full_depth=bool(spec.get("is_full_depth", True)),
                ports=ports,
                summary=summary[:300],
                spec=spec,
                commit=commit,
            )
        )
    with transaction.atomic():
        CatalogType.objects.all().delete()
        CatalogType.objects.bulk_create(rows, batch_size=1000)
    return len(rows)


def search(query: str, *, limit: int = 50) -> QuerySet[CatalogType]:
    """Types whose manufacturer, model or part number contain every word of ``query``."""
    rows = CatalogType.objects.annotate(
        text=Concat("manufacturer", Value(" "), "model", Value(" "), "part_number")
    )
    for word in query.split()[:6]:
        rows = rows.filter(text__icontains=word[:50])
    return rows.order_by("manufacturer", "model")[:limit]


def adopt(
    request: HttpRequest, actor: User, entry: CatalogType, *, tenant: Tenant | None
) -> DeviceType:
    """Copy a catalog type with all its ports into the library (shared or for one tenant)."""
    return interchange.import_type(request, actor, json.dumps(entry.spec), tenant=tenant)
