import importlib.util
from collections.abc import Callable, Iterator
from typing import Any

import pyotp
import pytest
from django.core.cache import cache
from django.test import Client

from patchmanager.accounts.middleware import SetupRequiredMiddleware
from patchmanager.accounts.models import Assignment, Group, Role, User
from patchmanager.core import crypto
from patchmanager.tenancy import rls
from patchmanager.tenancy.models import Tenant

PASSWORD = "correct horse battery staple 42"

# Browser tests need the e2e environment (Playwright).
collect_ignore = [] if importlib.util.find_spec("playwright") else ["e2e"]


@pytest.fixture(autouse=True)
def _isolation() -> Iterator[None]:
    from django.utils import translation

    translation.activate("en")
    cache.clear()
    SetupRequiredMiddleware._done = False
    yield
    cache.clear()


@pytest.fixture
def system() -> Callable[[], Any]:
    """Context manager with visibility of every tenant, for test setup and assertions."""
    return rls.system_context


@pytest.fixture
def make_tenant(db: None) -> Callable[..., Tenant]:
    counter = iter(range(1, 10_000))

    def make(**kwargs: Any) -> Tenant:
        n = next(counter)
        kwargs.setdefault("slug", f"tenant-{n}")
        kwargs.setdefault("name", f"Tenant {n}")
        with rls.system_context():
            return Tenant.objects.create(**kwargs)

    return make


@pytest.fixture
def make_user(db: None) -> Callable[..., User]:
    counter = iter(range(1, 10_000))

    def make(email: str | None = None, *, password: str = PASSWORD, **kwargs: Any) -> User:
        n = next(counter)
        kwargs.setdefault("display_name", f"User {n}")
        return User.objects.create_user(
            email=email or f"user{n}@example.com", password=password, **kwargs
        )

    return make


@pytest.fixture
def grant() -> Callable[..., Assignment]:
    def assign(
        principal: User | Group,
        role_key: str,
        tenant: Tenant | None = None,
        *,
        effect: str = "allow",
        conditions: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Assignment:
        field = "user" if isinstance(principal, User) else "group"
        return Assignment.objects.create(
            role=Role.objects.get(key=role_key),
            effect=effect,
            conditions=conditions or [],
            tenant=tenant,
            scope_path=tenant.policy_path if tenant else None,
            scope_label=str(tenant) if tenant else "global",
            **{field: principal},
            **kwargs,
        )

    return assign


@pytest.fixture
def super_admin(make_user: Callable[..., User], grant: Callable[..., Assignment]) -> User:
    user = make_user("root@example.com")
    grant(user, "super_admin")
    return user


def add_totp(user: User) -> pyotp.TOTP:
    """Give ``user`` a confirmed TOTP device; returns a generator for codes."""
    secret = pyotp.random_base32(length=32)
    user.totp_devices.create(name="app", encrypted_secret=crypto.encrypt(secret), confirmed=True)
    return pyotp.TOTP(secret)


def login(client: Client, user: User, *, totp: pyotp.TOTP | None = None) -> None:
    """Log in through the real views (password, and TOTP if given)."""
    response = client.post("/auth/login/", {"email": user.email, "password": PASSWORD}, secure=True)
    assert response.status_code == 302, response.content
    if totp is not None:
        response = client.post("/auth/mfa/", {"code": totp.now()}, secure=True)
        assert response.status_code == 302


@pytest.fixture
def admin_client(client: Client, super_admin: User) -> Client:
    totp = add_totp(super_admin)
    login(client, super_admin, totp=totp)
    return client


@pytest.fixture
def make_tree(make_tenant, super_admin):
    """Create tenant/site/building/floor/room/rack through the services."""
    from django.test import RequestFactory

    from patchmanager.dcim.models import Rack
    from patchmanager.locations import services
    from patchmanager.locations.models import Building, Floor, Room, Site

    def make(
        tenant: Tenant | None = None, *, name: str = "HQ", u_height: int = 42
    ) -> dict[str, Any]:
        tenant = tenant or make_tenant()
        request = RequestFactory().post("/")
        request.user = super_admin
        with rls.system_context():
            site = services.create(request, super_admin, Site, parent=tenant, name=name)
            building = services.create(request, super_admin, Building, parent=site, name="B1")
            floor = services.create(
                request, super_admin, Floor, parent=building, name="F1", level=1
            )
            room = services.create(request, super_admin, Room, parent=floor, name="R1")
            rack = services.create(
                request, super_admin, Rack, parent=room, name="A01", u_height=u_height
            )
        return {
            "tenant": tenant,
            "site": site,
            "building": building,
            "floor": floor,
            "room": room,
            "rack": rack,
            "request": request,
        }

    return make


@pytest.fixture
def make_type(super_admin):
    """Create a device type with port templates through the services."""
    from django.test import RequestFactory

    from patchmanager.library import services as library

    def make(
        model: str = "Switch 48",
        *,
        tenant: Tenant | None = None,
        ports: list[dict[str, Any]] | None = None,
        **fields: Any,
    ) -> Any:
        request = RequestFactory().post("/")
        request.user = super_admin
        with rls.system_context():
            vendor = library.get_or_create_vendor(
                request, super_admin, fields.pop("vendor", "Acme")
            )
            device_type = library.create_type(
                request, super_admin, tenant=tenant, vendor=vendor, model=model, **fields
            )
            for spec in ports or [{"name_pattern": "Gi1/0/{n}", "count": 4, "speed_mbps": 1000}]:
                library.add_port_template(request, super_admin, device_type, **spec)
        return device_type

    return make
