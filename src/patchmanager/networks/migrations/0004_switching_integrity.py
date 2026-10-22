from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity

# Table and the column that labels a row in the history.
DEVICE_TABLES = (
    ("networks_switchsettings", "hostname"),
    ("networks_mstinstance", "instance"),
    ("networks_staticroute", "prefix"),
    ("networks_bgpneighbor", "address"),
    ("networks_portprofile", "name"),
)

# Every row stays in the tenant of what it refers to (also for writes past the services).
FUNCTIONS = r"""
CREATE FUNCTION pm_check_device_row() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM dcim_device WHERE id = NEW.device_id)
        OR (to_jsonb(NEW) ? 'vrf_id' AND (to_jsonb(NEW) ->> 'vrf_id') IS NOT NULL
            AND NEW.tenant_id IS DISTINCT FROM
            (SELECT tenant_id FROM ipam_vrf WHERE id = (to_jsonb(NEW) ->> 'vrf_id')::uuid)) THEN
        RAISE EXCEPTION 'device records must belong to the device tenant';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION pm_check_stack() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM locations_site WHERE id = NEW.site_id) THEN
        RAISE EXCEPTION 'a stack must belong to the tenant of its site';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER networks_stack_check BEFORE INSERT OR UPDATE ON networks_stack
    FOR EACH ROW EXECUTE FUNCTION pm_check_stack();

CREATE FUNCTION pm_check_stackmember() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM networks_stack WHERE id = NEW.stack_id)
        OR NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM dcim_device WHERE id = NEW.device_id) THEN
        RAISE EXCEPTION 'stack members must belong to the stack tenant';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER networks_stackmember_check BEFORE INSERT OR UPDATE ON networks_stackmember
    FOR EACH ROW EXECUTE FUNCTION pm_check_stackmember();

-- A port joins only a link aggregation of its own device.
CREATE FUNCTION pm_check_port_lag() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.lag_id IS NOT NULL AND (NEW.lag_id = NEW.id OR NEW.device_id IS DISTINCT FROM
            (SELECT device_id FROM dcim_port WHERE id = NEW.lag_id AND kind = 'lag')) THEN
        RAISE EXCEPTION 'a port joins only a link aggregation of its own device';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER dcim_port_lag_check BEFORE INSERT OR UPDATE OF lag_id ON dcim_port
    FOR EACH ROW EXECUTE FUNCTION pm_check_port_lag();

CREATE TRIGGER networks_stack_history AFTER INSERT OR UPDATE OR DELETE ON networks_stack
    FOR EACH ROW EXECUTE FUNCTION pm_history('networks.stack', 'name', 'site');
"""

DEVICE_SQL = "\n".join(
    f"CREATE TRIGGER {t}_check BEFORE INSERT OR UPDATE ON {t}"
    " FOR EACH ROW EXECUTE FUNCTION pm_check_device_row();\n"
    f"CREATE TRIGGER {t}_history AFTER INSERT OR UPDATE OR DELETE ON {t}"
    f" FOR EACH ROW EXECUTE FUNCTION pm_history('{t.replace('_', '.', 1)}', '{label}', 'device');"
    for t, label in DEVICE_TABLES
)

DROP = (
    "\n".join(
        f"DROP TRIGGER {t}_history ON {t}; DROP TRIGGER {t}_check ON {t};"
        for t, _label in DEVICE_TABLES
    )
    + """
DROP TRIGGER networks_stack_history ON networks_stack;
DROP TRIGGER dcim_port_lag_check ON dcim_port;
DROP FUNCTION pm_check_port_lag();
DROP TRIGGER networks_stackmember_check ON networks_stackmember;
DROP FUNCTION pm_check_stackmember();
DROP TRIGGER networks_stack_check ON networks_stack;
DROP FUNCTION pm_check_stack();
DROP FUNCTION pm_check_device_row();
"""
)


class Migration(migrations.Migration):
    dependencies = [
        ("networks", "0003_switching"),
        ("dcim", "0010_switching"),
        ("ipam", "0002_integrity"),
        ("history", "0002_triggers"),
    ]

    operations = [
        migrations.RunSQL(FUNCTIONS + DEVICE_SQL, DROP),
        *(
            EnableRowLevelSecurity(name)
            for name in (
                "switchsettings",
                "mstinstance",
                "staticroute",
                "bgpneighbor",
                "portprofile",
                "stack",
                "stackmember",
            )
        ),
    ]
