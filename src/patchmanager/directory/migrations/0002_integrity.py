from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity, policy_sql

# Tables of other apps created in the same release (the operation below is per app).
OTHER_TABLES = (
    "ipam_dhcpscope",
    "ipam_dhcpreservation",
    "ipam_dnszone",
    "ipam_dnsrecord",
    "circuits_circuit",
)

# Each table with the references that must stay in the row's tenant (column, table).
REFERENCES = {
    "directory_contact": [("organization_id", "directory_organization")],
    "directory_system": [
        ("customer_id", "directory_organization"),
        ("site_id", "locations_site"),
        ("contact_id", "directory_contact"),
    ],
    "directory_link": [
        ("organization_id", "directory_organization"),
        ("system_id", "directory_system"),
    ],
    "ipam_dhcpscope": [("prefix_id", "ipam_prefix"), ("server_id", "dcim_device")],
    "ipam_dhcpreservation": [("scope_id", "ipam_dhcpscope")],
    "ipam_dnszone": [("server_id", "dcim_device")],
    "ipam_dnsrecord": [("zone_id", "ipam_dnszone"), ("address_id", "ipam_ipaddress")],
    "circuits_circuit": [
        ("provider_id", "directory_organization"),
        ("site_id", "locations_site"),
        ("port_id", "dcim_port"),
    ],
    "dcim_device": [("supplier_id", "directory_organization")],
}

# Tables in the history: model label and the column that names a row.
HISTORY = {
    "directory_organization": ("directory.organization", "name"),
    "directory_system": ("directory.system", "name"),
    "ipam_dhcpscope": ("ipam.dhcpscope", "name"),
    "ipam_dnszone": ("ipam.dnszone", "name"),
    "ipam_dnsrecord": ("ipam.dnsrecord", "name"),
    "circuits_circuit": ("circuits.circuit", "circuit_id"),
}

FUNCTION = r"""
CREATE FUNCTION pm_check_references() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    row_data jsonb := to_jsonb(NEW);
    i int := 0;
    other uuid;
BEGIN
    WHILE i < TG_NARGS LOOP
        IF row_data ->> TG_ARGV[i] IS NOT NULL THEN
            EXECUTE format('SELECT tenant_id FROM %I WHERE id = $1', TG_ARGV[i + 1])
                INTO other USING (row_data ->> TG_ARGV[i])::uuid;
            IF other IS DISTINCT FROM NEW.tenant_id THEN
                RAISE EXCEPTION '% must belong to the tenant of the row', TG_ARGV[i];
            END IF;
        END IF;
        i := i + 2;
    END LOOP;
    RETURN NEW;
END;
$$;
"""


def _args(pairs: list[tuple[str, str]]) -> str:
    return ", ".join(f"'{part}'" for pair in pairs for part in pair)


CREATE = (
    FUNCTION
    + "\n".join(
        f"CREATE TRIGGER {table}_refs BEFORE INSERT OR UPDATE ON {table}"
        f" FOR EACH ROW EXECUTE FUNCTION pm_check_references({_args(pairs)});"
        for table, pairs in REFERENCES.items()
    )
    + "\n"
    + "\n".join(
        f"CREATE TRIGGER {table}_history AFTER INSERT OR UPDATE OR DELETE ON {table}"
        f" FOR EACH ROW EXECUTE FUNCTION pm_history('{label}', '{column}', 'tenant');"
        for table, (label, column) in HISTORY.items()
    )
)
CREATE += "\n" + "\n".join(sql + ";" for table in OTHER_TABLES for sql in policy_sql(table))
DROP = (
    "\n".join(
        f'DROP POLICY tenant_isolation ON "{t}"; ALTER TABLE "{t}" DISABLE ROW LEVEL SECURITY;'
        for t in OTHER_TABLES
    )
    + "\n"
    + "\n".join(f"DROP TRIGGER {table}_history ON {table};" for table in HISTORY)
    + "\n"
    + "\n".join(f"DROP TRIGGER {table}_refs ON {table};" for table in REFERENCES)
    + "\nDROP FUNCTION pm_check_references();"
)


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0001_services"),
        ("ipam", "0003_services"),
        ("circuits", "0002_services"),
        ("dcim", "0013_services"),
        ("history", "0002_triggers"),
    ]

    operations = [
        migrations.RunSQL(CREATE, DROP),
        EnableRowLevelSecurity("organization"),
        EnableRowLevelSecurity("contact"),
        EnableRowLevelSecurity("system"),
        EnableRowLevelSecurity("link"),
    ]
