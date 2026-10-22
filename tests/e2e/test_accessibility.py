"""WCAG 2.2 A and AA checks with axe-core on the main pages, light and dark, desktop and phone."""

import hashlib
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.e2e.test_locations import _sign_in

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=False)]

AXE = Path(__file__).parent / "vendor" / "axe-4.12.1.min.js"
AXE_SHA256 = "66a8aaa95a8b044a7fd74a5435873bf04ff65a1ca75567c921b7509742085a14"  # cdnjs SRI checked
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]


VIOLATIONS = """async (tags) => {
  const result = await axe.run(document, {runOnly: {type: "tag", values: tags}});
  return result.violations.filter((v) => ["serious", "critical"].includes(v.impact)).map((v) => ({
    id: v.id,
    impact: v.impact,
    nodes: v.nodes.slice(0, 3).map((n) => {
      const data = n.any[0] && n.any[0].data;
      const colours = data ? JSON.stringify([data.fgColor, data.bgColor, data.contrastRatio]) : "";
      return `${n.target.join(" ")} ${colours}`;
    }),
  }));
}"""


def _axe(page: Page) -> list[dict[str, Any]]:
    source = AXE.read_bytes()
    assert hashlib.sha256(source).hexdigest() == AXE_SHA256
    page.evaluate(source.decode())  # evaluate is not bound by the page's CSP
    found: list[dict[str, Any]] = page.evaluate(VIOLATIONS, TAGS)
    return found


def _pages(live_server, tree) -> list[str]:
    return [
        "/",
        "/locations/",
        f"/locations/site/{tree['site'].pk}/",
        f"/locations/floor/{tree['floor'].pk}/",
        f"/racks/{tree['rack'].pk}/",
        "/library/",
        "/cabling/",
        "/networks/vlans/",
        "/history/",
        "/search/?q=A0",
        "/exchange/",
        "/account/",
        "/account/security/",
        "/account/tokens/",
        "/manage/",
        "/manage/users/",
        "/manage/roles/",
        "/manage/audit/",
        "/manage/system/",
        "/manage/edition/",
        "/manage/branding/",
        "/manage/sso/",
        f"/plans/{tree['floor'].pk}/",
        "/licenses/",
    ]


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_pages_meet_wcag_aa(page: Page, live_server, super_admin, make_tree, scheme) -> None:
    tree = make_tree()
    page.emulate_media(color_scheme=scheme)
    page.context.add_cookies([{"name": "pm_theme", "value": scheme, "url": live_server.url}])
    _sign_in(page, live_server, super_admin)
    problems = {}
    for path in _pages(live_server, tree):
        page.goto(live_server.url + path)
        found = _axe(page)
        if found:
            problems[path] = found
    assert problems == {}


def test_sign_in_and_phone_layout(page: Page, live_server, super_admin, make_tree) -> None:
    tree = make_tree()
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(live_server.url + "/auth/login/")
    assert _axe(page) == []
    _sign_in(page, live_server, super_admin)
    page.goto(f"{live_server.url}/racks/{tree['rack'].pk}/")
    assert _axe(page) == []
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 375, "no horizontal scrolling on a phone"
    nav = page.locator("#main-nav")
    assert not nav.is_visible()
    page.get_by_role("button", name="Open menu").click()
    assert nav.is_visible()
    page.get_by_role("link", name="Cables").click()
    assert page.url.endswith("/cabling/")
