"""Core browser flows: first run setup, MFA login, admin area. Run with ``pixi run e2e``."""

import re

import pyotp
import pytest
from playwright.sync_api import Page, expect

from patchmanager.accounts.models import TOTPDevice
from patchmanager.accounts.services import users
from patchmanager.core import crypto
from tests.conftest import PASSWORD

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=False)]


@pytest.fixture
def csp_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on(
        "console",
        lambda msg: errors.append(msg.text) if "Content-Security-Policy" in msg.text else None,
    )
    return errors


def test_setup_enrol_mfa_and_admin(page: Page, live_server, csp_errors) -> None:
    token = users.issue_setup_token()
    page.goto(live_server.url)
    expect(page).to_have_url(re.compile(r"/setup/$"))
    page.get_by_label("Setup token").fill(token)
    page.get_by_label("Email").fill("admin@example.test")
    page.get_by_label("Name").fill("Admin")
    page.get_by_label("Password:", exact=True).fill(PASSWORD)
    page.get_by_label("Repeat password").fill(PASSWORD)
    page.get_by_role("button", name="Create administrator").click()

    expect(page).to_have_url(re.compile(r"/account/security/$"))
    page.get_by_role("button", name="Add authenticator app").click()
    expect(page.get_by_role("img", name="QR code")).to_be_visible()
    device = TOTPDevice.objects.get(confirmed=False)
    page.get_by_role("textbox", name="Code").fill(
        pyotp.TOTP(crypto.decrypt(device.encrypted_secret)).now()
    )
    page.get_by_role("button", name="Confirm").click()
    expect(page.get_by_text("Your recovery codes")).to_be_visible()

    page.get_by_role("link", name="Administration").first.click()
    expect(page.get_by_role("heading", name="Administration")).to_be_visible()
    page.get_by_role("navigation", name="Administration").get_by_role("link", name="Roles").click()
    expect(page.get_by_role("link", name="Super Admin")).to_be_visible()

    page.get_by_role("button", name="Admin").click()  # user menu (display name)
    page.get_by_role("button", name="Sign out").click()
    expect(page).to_have_url(re.compile(r"/auth/login/$"))
    assert csp_errors == []


def test_login_requires_second_factor(page: Page, live_server, make_user, csp_errors) -> None:
    from tests.conftest import add_totp

    user = make_user("ops@example.test")
    totp = add_totp(user)
    page.goto(live_server.url + "/auth/login/")
    page.get_by_label("Email").fill(user.email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page).to_have_url(re.compile(r"/auth/mfa/$"))
    page.get_by_label("Code").fill("000000")
    page.get_by_role("button", name="Verify").click()
    expect(page.get_by_text("This code is not valid.")).to_be_visible()
    page.get_by_label("Code").fill(totp.now())
    page.get_by_role("button", name="Verify").click()
    expect(page.get_by_role("heading", name=re.compile("Welcome"))).to_be_visible()
    assert csp_errors == []


def test_theme_toggle_persists(page: Page, live_server, make_user) -> None:
    from tests.conftest import add_totp

    user = make_user()
    totp = add_totp(user)
    page.goto(live_server.url + "/auth/login/")
    page.get_by_label("Email").fill(user.email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.get_by_label("Code").fill(totp.now())
    page.get_by_role("button", name="Verify").click()
    page.get_by_role("button", name=re.compile("Theme")).click()  # system -> light
    page.get_by_role("button", name=re.compile("Theme")).click()  # light -> dark
    page.reload()
    expect(page.locator("html")).to_have_class(re.compile("dark"))
