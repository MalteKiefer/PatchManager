import pytest
from django.core.management import CommandError, call_command

from patchmanager.cabling.models import Cable
from patchmanager.dcim.models import Port


def test_seed_scale_builds_a_cabled_inventory(super_admin, system, capsys) -> None:
    call_command("seed_scale", actor=super_admin.email, racks=2, slug="tiny")
    assert "2 racks, 200 ports, 48 cables" in capsys.readouterr().out
    with system():
        assert Port.objects.filter(tenant__slug="tiny").count() == 200
        assert Cable.objects.filter(tenant__slug="tiny").count() == 48


def test_seed_scale_needs_an_existing_user(db) -> None:
    with pytest.raises(CommandError):
        call_command("seed_scale", actor="nobody@example.com", racks=1)
