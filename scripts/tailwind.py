"""Fetch the pinned Tailwind CSS standalone CLI (checksum verified) and run it.

Usage: python scripts/tailwind.py [tailwind args...]
"""

import hashlib
import os
import platform
import stat
import subprocess  # nosec B404: runs the verified, pinned Tailwind binary
import sys
import urllib.request
from pathlib import Path

VERSION = "4.3.3"
CHECKSUMS = {
    "linux-x64": "dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a",
    "linux-arm64": "55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195",
    "macos-arm64": "cdf646702987a743464dff4d9c60fd4480d1c1e73dd819a9a67f1078815dce9d",
}


def target() -> str:
    """Return the release asset suffix for this machine."""
    system = {"Linux": "linux", "Darwin": "macos"}[platform.system()]
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}[
        platform.machine().lower()
    ]
    return f"{system}-{arch}"


def binary() -> Path:
    """Download the CLI once into .pixi/bin and verify its SHA-256."""
    name = target()
    root = Path(os.environ.get("PIXI_PROJECT_ROOT", Path(__file__).resolve().parent.parent))
    path = root / ".pixi" / "bin" / f"tailwindcss-{VERSION}-{name}"
    if not path.exists():
        url = f"https://github.com/tailwindlabs/tailwindcss/releases/download/v{VERSION}/tailwindcss-{name}"
        data = urllib.request.urlopen(url, timeout=60).read()  # nosec B310: fixed https URL
        if hashlib.sha256(data).hexdigest() != CHECKSUMS[name]:
            raise SystemExit(f"checksum mismatch for {url}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


if __name__ == "__main__":
    sys.exit(subprocess.call([str(binary()), *sys.argv[1:]]))  # noqa: S603  # nosec B603
