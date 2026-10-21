import pytest
from django.db import connection, transaction
from django.db.utils import ProgrammingError

from patchmanager.audit import services
from patchmanager.audit.models import AuditEvent

pytestmark = pytest.mark.django_db


def test_chain_links_and_verifies(make_user) -> None:
    user = make_user()
    first = services.record("test.one", actor=user, obj=user, data={"n": 1})
    second = services.record("test.two", data={"nested": {"a": [1, 2]}})
    assert first.prev_hash == services.GENESIS
    assert second.prev_hash == first.hash
    result = services.verify()
    assert result.ok and result.checked == 2


def test_update_and_delete_are_rejected() -> None:
    event = services.record("test.immutable")
    with pytest.raises(ProgrammingError, match="append only"), transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).update(action="changed")
    with pytest.raises(ProgrammingError, match="append only"), transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).delete()


def _tamper(sql: str, params: list[object]) -> None:
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE audit_auditevent DISABLE TRIGGER audit_append_only")
        cursor.execute(sql, params)
        cursor.execute("ALTER TABLE audit_auditevent ENABLE TRIGGER audit_append_only")


def test_verify_detects_altered_content() -> None:
    services.record("test.a")
    event = services.record("test.b", data={"amount": 1})
    services.record("test.c")
    _tamper("UPDATE audit_auditevent SET data = %s WHERE seq = %s", ['{"amount": 1000}', event.seq])
    result = services.verify()
    assert not result.ok
    assert result.broken_at == event.seq
    assert result.reason == "event content altered"


def test_verify_detects_deleted_event() -> None:
    services.record("test.a")
    middle = services.record("test.b")
    last = services.record("test.c")
    _tamper("DELETE FROM audit_auditevent WHERE seq = %s", [middle.seq])
    result = services.verify()
    assert not result.ok and result.broken_at == last.seq


def test_verify_command(capsys) -> None:
    from django.core.management import call_command

    services.record("test.cmd")
    call_command("verify_audit_chain")
    assert "ok: 1 events verified" in capsys.readouterr().out


def test_audit_views(admin_client) -> None:
    services.record("test.view", data={"formula": "=SUM(A1)"})
    assert admin_client.get("/manage/audit/?action=test", secure=True).status_code == 200
    export = admin_client.get("/manage/audit/export.csv", secure=True)
    body = b"".join(export.streaming_content).decode()
    assert "test.view" in body
    response = admin_client.post("/manage/audit/verify/", secure=True, follow=True)
    assert "Chain intact" in response.content.decode()


def test_csv_neutralises_formulas(admin_client, make_user) -> None:
    make_user("=cmd@example.com")
    services.record("=HYPERLINK(evil)")
    body = b"".join(
        admin_client.get("/manage/audit/export.csv", secure=True).streaming_content
    ).decode()
    assert "'=HYPERLINK(evil)" in body


def test_auditor_without_global_scope_sees_only_tenant_events(
    client, make_user, make_tenant, grant
) -> None:
    from tests.conftest import login

    mine, other = make_tenant(), make_tenant()
    services.record("test.mine", tenant_id=mine.pk)
    services.record("test.other", tenant_id=other.pk)
    auditor = make_user()
    grant(auditor, "auditor", mine)
    login(client, auditor)
    page = client.get("/manage/audit/", secure=True).content.decode()
    assert "test.mine" in page and "test.other" not in page
