from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity

# Each table with the references that must stay in the row's tenant (column, table);
# the check function comes from directory 0002.
REFERENCES = {
    "compute_cluster": [("site_id", "locations_site")],
    "compute_clusterhost": [("cluster_id", "compute_cluster"), ("device_id", "dcim_device")],
    "compute_storagevolume": [
        ("device_id", "dcim_device"),
        ("cluster_id", "compute_cluster"),
        ("parent_id", "compute_storagevolume"),
    ],
    "compute_virtualmachine": [("cluster_id", "compute_cluster"), ("host_id", "dcim_device")],
    "compute_vminterface": [("vm_id", "compute_virtualmachine"), ("vlan_id", "networks_vlan")],
    "compute_connection": [
        ("cluster_id", "compute_cluster"),
        ("node_room_id", "locations_room"),
    ],
    "compute_virtualdisk": [
        ("vm_id", "compute_virtualmachine"),
        ("storage_id", "compute_storagevolume"),
    ],
}
HISTORY = {
    "compute_cluster": ("compute.cluster", "name"),
    "compute_virtualmachine": ("compute.virtualmachine", "name"),
    "compute_storagevolume": ("compute.storagevolume", "name"),
}


def _args(pairs: list[tuple[str, str]]) -> str:
    return ", ".join(f"'{part}'" for pair in pairs for part in pair)


CREATE = (
    "\n".join(
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
DROP = (
    "\n".join(f"DROP TRIGGER {t}_history ON {t};" for t in HISTORY)
    + "\n"
    + "\n".join(f"DROP TRIGGER {t}_refs ON {t};" for t in REFERENCES)
)


class Migration(migrations.Migration):
    dependencies = [
        ("compute", "0001_initial"),
        ("directory", "0002_integrity"),
        ("history", "0002_triggers"),
    ]

    operations = [
        migrations.RunSQL(CREATE, DROP),
        *(
            EnableRowLevelSecurity(name)
            for name in (
                "cluster",
                "clusterhost",
                "storagevolume",
                "virtualmachine",
                "vminterface",
                "virtualdisk",
                "connection",
            )
        ),
    ]
