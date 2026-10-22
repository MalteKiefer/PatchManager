"""Configuration readers per platform, and recognising which one a text is."""

from collections.abc import Callable
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from patchmanager.networks.platforms import (
    arubacx,
    cisco,
    comware,
    fortios,
    junos,
    lancom,
    routeros,
)
from patchmanager.networks.platforms.common import Config, ParseError

__all__ = ["PLATFORMS", "VENDOR_NAMES", "Config", "ParseError", "Platform", "detect", "parse"]


@dataclass(frozen=True)
class Platform:
    """A reader: its key, its name, how sure it is about a text, and the reader itself."""

    key: str
    name: object
    detect: Callable[[str], int]
    parse: Callable[[str], Config]


def _aruba_detect(text: str) -> int:
    import re

    score = 0
    if re.search(r"^; \S+ Configuration Editor; Created on release", text, re.M):
        score += 90
    if re.search(r"^   (untagged|tagged) [\w/,-]+$", text, re.M):
        score += 40
    if "%_sys_" in text or re.search(r"^%if ", text, re.M):
        score += 50
    return min(score, 100)


def _aruba_parse(text: str) -> Config:
    from patchmanager.networks import aruba

    return aruba.parse(text)


PLATFORMS = {
    p.key: p
    for p in (
        Platform("aruba-aos-s", "ArubaOS-Switch (ProCurve)", _aruba_detect, _aruba_parse),
        Platform("aruba-aos-cx", "Aruba AOS-CX", arubacx.detect, arubacx.parse),
        Platform("cisco", "Cisco IOS, IOS-XE, NX-OS", cisco.detect, cisco.parse),
        Platform("fortios", "Fortinet FortiOS", fortios.detect, fortios.parse),
        Platform("junos", "Juniper Junos", junos.detect, junos.parse),
        Platform("routeros", "MikroTik RouterOS", routeros.detect, routeros.parse),
        Platform("comware", _("HPE Comware, H3C, Huawei VRP"), comware.detect, comware.parse),
        Platform("lancom", "LANCOM LCOS", lancom.detect, lancom.parse),
    )
}


VENDOR_NAMES = {
    "aruba-aos-s": "ArubaOS-Switch",
    "arubacx": "Aruba AOS-CX",
    "cisco": "Cisco",
    "fortios": "Fortinet FortiOS",
    "junos": "Juniper Junos",
    "routeros": "MikroTik RouterOS",
    "comware": "HPE Comware / H3C",
    "huawei": "Huawei VRP",
    "lancom": "LANCOM LCOS",
}


def detect(text: str) -> Platform | None:
    """The platform the text most likely comes from (None when none is sure enough)."""
    sample = text[:200_000]
    scores = sorted(((p.detect(sample), p.key) for p in PLATFORMS.values()), reverse=True)
    best, key = scores[0]
    return PLATFORMS[key] if best >= 50 else None


def parse(text: str, platform: str = "", variables: dict[str, object] | None = None) -> Config:
    """Read ``text`` with the given platform, or the recognised one."""
    if platform == "aruba-aos-s" or (not platform and variables):
        from patchmanager.networks import aruba

        return aruba.parse(text, variables or {})
    chosen = PLATFORMS.get(platform) if platform else detect(text)
    if chosen is None:
        raise ParseError("the platform of this configuration is not recognised; choose it")
    return chosen.parse(text)
