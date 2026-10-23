import hashlib
import hmac
import json
import socket
from typing import Any

import pytest
from django.test import Client

from patchmanager.accounts.models import Assignment, Role
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.integrations import http, services
from patchmanager.integrations.models import Delivery
from patchmanager.locations import services as tree
from patchmanager.tenancy import rls
from tests.conftest import login

pytestmark = pytest.mark.django_db


def _resolve(monkeypatch, address: str) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))],
    )


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_post(url, body, headers):
        calls.append({"url": url, "body": json.loads(body), "headers": headers, "raw": body})
        return calls[0].get("status", 200) if False else fake_post.status

    fake_post.status = 200
    monkeypatch.setattr(http, "post", fake_post)
    _resolve(monkeypatch, "93.184.216.34")
    calls.append  # noqa: B018
    return calls, fake_post


def test_changes_become_signed_deliveries(make_tree, super_admin, sent) -> None:
    calls, _fake = sent
    t = make_tree()
    with rls.system_context():
        hook, secret = services.create(
            t["request"],
            super_admin,
            t["tenant"],
            name="CMDB",
            url="https://hooks.example.com/pm",
            events=["dcim.rack.*"],
        )
        services.create(
            t["request"],
            super_admin,
            t["tenant"],
            name="Off",
            url="https://hooks.example.com/x",
            events=["*"],
            active=False,
        )
        tree.update(t["request"], super_admin, t["rack"], name="A02")
        tree.update(t["request"], super_admin, t["room"], name="R2")  # not subscribed
        assert Delivery.objects.filter(webhook=hook).count() == 1
        assert services.deliver_due() == 1
    body, headers = calls[0]["body"], calls[0]["headers"]
    assert (
        body["event"] == "dcim.rack.update"
        and body["after"]["name"] == "A02"
        and body["fields"] == ["name"]
    )
    stamp, signature = (p.split("=", 1)[1] for p in headers["PatchManager-Signature"].split(","))
    expected = hmac.new(
        secret.encode(), f"{stamp}.".encode() + calls[0]["raw"], hashlib.sha256
    ).hexdigest()
    assert signature == expected and headers["PatchManager-Event"] == "dcim.rack.update"
    with rls.system_context():
        delivery = Delivery.objects.get(webhook=hook)
        assert (delivery.status, delivery.attempts, delivery.last_status_code) == ("sent", 1, 200)
        assert services.deliver_due() == 0


def test_failures_retry_with_backoff_then_fail(make_tree, super_admin, sent) -> None:
    calls, fake = sent
    fake.status = 500
    t = make_tree()
    with rls.system_context():
        hook, _secret = services.create(
            t["request"],
            super_admin,
            t["tenant"],
            name="x",
            url="https://hooks.example.com/pm",
            events=["*"],
        )
        ping = services.ping(t["request"], super_admin, hook)
        services.deliver_due()
        ping.refresh_from_db()
        assert (ping.status, ping.attempts, ping.last_error) == ("pending", 1, "HTTP 500")
        assert ping.next_attempt_at > ping.updated_at.replace(microsecond=0)
        Delivery.objects.filter(pk=ping.pk).update(
            attempts=len(services.BACKOFF), next_attempt_at=ping.created_at
        )
        services.deliver_due()
        ping.refresh_from_db()
        assert ping.status == "failed"
        services.redeliver(t["request"], super_admin, ping)
        fake.status = 204
        services.deliver_due()
        ping.refresh_from_db()
        assert ping.status == "sent"
    assert calls[-1]["body"]["event"] == "ping"


def test_rolled_back_changes_send_nothing(make_tree, super_admin, sent) -> None:
    from django.db import transaction

    t = make_tree()
    with rls.system_context():
        hook, _s = services.create(
            t["request"],
            super_admin,
            t["tenant"],
            name="x",
            url="https://hooks.example.com/",
            events=["*"],
        )
        with transaction.atomic():
            tree.update(t["request"], super_admin, t["rack"], name="gone")
            transaction.set_rollback(True)
        assert not Delivery.objects.filter(webhook=hook).exists()


def test_url_guard(monkeypatch, settings) -> None:
    for address in (
        "127.0.0.1",
        "10.1.2.3",
        "169.254.169.254",
        "::1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "2002:7f00:1::1",
        "64:ff9b::a9fe:a9fe",
        "2001:0:4136:e378:8000:63bf:f5ff:fffe",
    ):
        _resolve(monkeypatch, address)
        with pytest.raises(ServiceError, match="not allowed"):
            http.check("https://internal.example.com/hook")
    _resolve(monkeypatch, "10.1.2.3")
    settings.WEBHOOK_ALLOWED_NETWORKS = ["10.0.0.0/8"]
    assert http.check("https://cmdb.corp:8443/hook?x=1").path == "/hook?x=1"
    for url, match in (
        ("http://cmdb.corp/", "https"),
        ("https://user:pw@cmdb.corp/", "credentials"),
        ("ftp://x/", "https"),
    ):
        with pytest.raises(ServiceError, match=match):
            http.check(url)
    settings.WEBHOOK_ALLOW_HTTP = True
    assert http.check("http://cmdb.corp/").port == 80

    def fail(*a, **k):
        raise socket.gaierror

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(ServiceError, match="resolved"):
        http.check("https://nowhere.invalid/")


def test_post_uses_checked_address(monkeypatch, settings) -> None:
    import urllib3

    seen: dict[str, Any] = {}

    class Pool:
        def __init__(self, host, port, **kw):
            seen.update(host=host, port=port, **kw)

        def urlopen(self, method, path, **kw):
            seen.update(method=method, path=path, headers=kw["headers"], redirect=kw["redirect"])

            class Response:
                status = 202

                def read(self, n):
                    return b""

                def release_conn(self):
                    pass

            return Response()

        def close(self):
            pass

    monkeypatch.setattr(urllib3, "HTTPSConnectionPool", Pool)
    _resolve(monkeypatch, "93.184.216.34")
    assert http.post("https://hooks.example.com/a", b"{}", {"X": "1"}) == 202
    assert (seen["host"], seen["server_hostname"], seen["assert_hostname"]) == (
        "93.184.216.34",
        "hooks.example.com",
        "hooks.example.com",
    )
    assert seen["headers"]["Host"] == "hooks.example.com" and seen["redirect"] is False


def test_patterns_and_permissions(make_tree, make_user, super_admin, sent) -> None:
    t = make_tree()
    editor = make_user()
    with rls.system_context():
        for bad in ([], ["dcim.%"], ["DCIM.*"], ["a.b.c.d"]):
            with pytest.raises(ServiceError, match="patterns"):
                services.create(
                    t["request"],
                    super_admin,
                    t["tenant"],
                    name="x",
                    url="https://h.example.com/",
                    events=bad,
                )
        Assignment.objects.create(
            role=Role.objects.get(key="editor"),
            user=editor,
            tenant=t["tenant"],
            scope_path=t["tenant"].policy_path,
        )
        with pytest.raises(PermissionDeniedError):
            services.create(
                t["request"],
                editor,
                t["tenant"],
                name="x",
                url="https://h.example.com/",
                events=["*"],
            )


def test_pages(make_tree, admin_client: Client, super_admin, make_user, sent) -> None:
    t = make_tree()
    assert admin_client.get("/integrations/webhooks/new/", secure=True).status_code == 200
    for url in ("/integrations/webhooks/", "/integrations/webhooks/new/"):
        page_html = admin_client.get(url, secure=True).content.decode()
        assert 'aria-label="Administration"' in page_html  # admin side menu
        assert 'href="/integrations/webhooks/" aria-current="page"' in page_html
    page = admin_client.post(
        "/integrations/webhooks/new/",
        {
            "name": "CMDB",
            "url": "https://hooks.example.com/pm",
            "events": "dcim.*, cabling.cable.insert",
            "active": "on",
        },
        secure=True,
        follow=True,
    )
    assert b"Signing secret" in page.content
    with rls.system_context():
        hook = (
            t["tenant"]
            and __import__(
                "patchmanager.integrations.models", fromlist=["Webhook"]
            ).Webhook.objects.get()
        )
        assert hook.events == ["cabling.cable.insert", "dcim.*"]
    url = f"/integrations/webhooks/{hook.pk}/"
    assert b"Signing secret" not in admin_client.get(url, secure=True).content
    admin_client.post(url + "ping/", secure=True)
    with rls.system_context():
        services.deliver_due()
        delivery = Delivery.objects.get(webhook=hook)
    assert b"ping" in admin_client.get(url, secure=True).content
    admin_client.post(f"/integrations/deliveries/{delivery.pk}/redeliver/", secure=True)
    assert b"Signing secret" in admin_client.post(url + "rotate/", secure=True, follow=True).content
    saved = admin_client.post(
        url,
        {"name": "CMDB", "url": "https://hooks.example.com/v2", "events": "*"},
        secure=True,
        follow=True,
    )
    assert b"Webhook saved" in saved.content
    bad = admin_client.post(
        url, {"name": "CMDB", "url": "https://hooks.example.com/v2", "events": "x y"}, secure=True
    )
    assert b"patterns" in bad.content
    assert admin_client.post(url + "nope/", secure=True).status_code == 404
    viewer = Client()
    user = make_user()
    with rls.system_context():
        Assignment.objects.create(
            role=Role.objects.get(key="editor"),
            user=user,
            tenant=t["tenant"],
            scope_path=t["tenant"].policy_path,
        )
    login(viewer, user)
    assert viewer.get("/integrations/webhooks/", secure=True).status_code == 403
    assert viewer.get(url, secure=True).status_code == 404
    admin_client.post(url + "delete/", secure=True)
    with rls.system_context():
        assert not Delivery.objects.exists()


def test_prune(make_tree, super_admin, sent) -> None:
    from datetime import timedelta

    from django.utils import timezone

    t = make_tree()
    with rls.system_context():
        hook, _s = services.create(
            t["request"],
            super_admin,
            t["tenant"],
            name="x",
            url="https://h.example.com/",
            events=["*"],
        )
        old = services.ping(t["request"], super_admin, hook)
        Delivery.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=31))
        services.ping(t["request"], super_admin, hook)
        assert services.prune() == 1


def test_tenant_wide_sight_required_and_checked_on_delivery(
    make_tree, make_user, super_admin, sent
) -> None:
    calls, _fake = sent
    t = make_tree()
    owner = make_user()
    with rls.system_context():
        Assignment.objects.create(
            role=Role.objects.get(key="admin"),
            user=owner,
            tenant=t["tenant"],
            scope_path=t["tenant"].policy_path,
        )
        hook, _s = services.create(
            t["request"],
            owner,
            t["tenant"],
            name="x",
            url="https://h.example.com/a?key=s3cret",
            events=["*"],
        )
        from patchmanager.audit.models import AuditEvent

        assert (
            AuditEvent.objects.filter(action="integrations.webhook.created")
            .latest("seq")
            .data["url"]
            == "https://h.example.com"
        )
        deny = Assignment.objects.create(
            role=Role.objects.get(key="viewer"),
            user=owner,
            tenant=t["tenant"],
            scope_path=t["room"].path,
            effect="deny",
        )
        with pytest.raises(PermissionDeniedError):
            services.create(
                t["request"],
                owner,
                t["tenant"],
                name="y",
                url="https://h.example.com/",
                events=["*"],
            )
        tree.update(t["request"], super_admin, t["rack"], name="A02")
        services.deliver_due()
        delivery = Delivery.objects.get(webhook=hook)
        assert delivery.status == "failed" and "lost access" in delivery.last_error
        assert calls == []
        deny.delete()


def test_send_window_and_redacted_default_name(make_tree, super_admin, sent, monkeypatch) -> None:
    calls, _fake = sent
    t = make_tree()
    with rls.system_context():
        hook, _s = services.create(
            t["request"],
            super_admin,
            t["tenant"],
            name=" ",
            url="https://h.example.com/a?k=s",
            events=["*"],
        )
        assert hook.name == "https://h.example.com"
        services.ping(t["request"], super_admin, hook)
        monkeypatch.setattr(services, "SEND_WINDOW", -1)
        services.deliver_due()
        assert calls == [] and Delivery.objects.get().status == "pending"
    assert services.LEASE.total_seconds() > services.SEND_WINDOW + 60


def test_inactive_hooks_bad_ports_and_broken_sends(
    make_tree, super_admin, sent, monkeypatch
) -> None:
    calls, _fake = sent
    t = make_tree()
    with pytest.raises(ServiceError, match="port"):
        http.check("https://h.example.com:99999/")
    with rls.system_context():
        hook, _s = services.create(
            t["request"],
            super_admin,
            t["tenant"],
            name="x",
            url="https://h.example.com/",
            events=["*"],
        )
        first = services.ping(t["request"], super_admin, hook)
        services.update(
            t["request"],
            super_admin,
            hook,
            name="x",
            url="https://h.example.com/",
            events=["*"],
            active=False,
        )
        services.deliver_due()
        first.refresh_from_db()
        assert first.status == "failed" and "inactive" in first.last_error and calls == []
        services.update(
            t["request"],
            super_admin,
            hook,
            name="x",
            url="https://h.example.com/",
            events=["*"],
            active=True,
        )
        second = services.ping(t["request"], super_admin, hook)

        def boom(delivery):
            raise RuntimeError("bad key")

        monkeypatch.setattr(services, "_send", boom)
        services.deliver_due()
        second.refresh_from_db()
        assert (
            second.status == "pending"
            and second.attempts == 1
            and "server log" in second.last_error
        )


def test_types_added_later_do_not_stop_older_webhooks(make_tree, make_user) -> None:
    """A role from before new object types existed still sees everything its webhook sends."""
    t = make_tree()
    owner = make_user()
    with rls.system_context():
        role = Role.objects.create(
            key="old-viewer",
            name="Old viewer",
            permissions=["dcim.device.view", "dcim.port.view", "dcim.rack.view"],
        )
        Assignment.objects.create(
            role=role, user=owner, tenant=t["tenant"], scope_path=t["tenant"].policy_path
        )
        assert services.sees_everything(owner, t["tenant"], ["dcim.device.*", "dcim.port.update"])
        assert not services.sees_everything(owner, t["tenant"], ["*"])
        assert not services.sees_everything(owner, t["tenant"])
