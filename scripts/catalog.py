"""Refresh the bundled device type catalog from the community device type library.

The library (https://github.com/netbox-community/devicetype-library) is CC0-1.0.
Only the YAML definitions are fetched (a sparse checkout without the images) and
written as one compressed JSON file that the application loads on migrate.

    pixi run -e dev python scripts/catalog.py [git ref]
"""

import gzip
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = "https://github.com/netbox-community/devicetype-library.git"
OUT = Path(__file__).resolve().parent.parent / "src/patchmanager/library/catalog.json.gz"
KEEP = (
    "manufacturer",
    "model",
    "slug",
    "part_number",
    "u_height",
    "is_full_depth",
    "airflow",
    "weight",
    "weight_unit",
    "subdevice_role",
    "interfaces",
    "console-ports",
    "console-server-ports",
    "power-ports",
    "power-outlets",
    "front-ports",
    "rear-ports",
)
PORT_KEYS = (
    "name",
    "type",
    "rear_port",
    "rear_port_position",
    "positions",
    "mgmt_only",
    "maximum_draw",
)


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)  # noqa: S603, S607
    return result.stdout.strip()


def _entry(data: dict) -> dict:
    entry = {k: data[k] for k in KEEP if k in data}
    for key, value in entry.items():
        if isinstance(value, list):
            entry[key] = [{k: p[k] for k in PORT_KEYS if k in p} for p in value]
    return entry


def main(ref: str = "master") -> None:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        _git(
            "clone",
            "--depth",
            "1",
            "--filter=blob:none",
            "--sparse",
            "--branch",
            ref,
            REPO,
            "lib",
            cwd=work,
        )
        repo = work / "lib"
        _git("sparse-checkout", "set", "device-types", cwd=repo)
        commit = _git("rev-parse", "HEAD", cwd=repo)
        entries = []
        for path in sorted((repo / "device-types").rglob("*.y*ml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("manufacturer") and data.get("model"):
                entries.append(_entry(data))
    payload = {"source": REPO, "commit": commit, "license": "CC0-1.0", "types": entries}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    OUT.write_bytes(gzip.compress(raw.encode(), mtime=0))
    print(f"{len(entries)} device types from {commit[:12]} -> {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main(*sys.argv[1:])
