from django.db import migrations

# Trigram indexes speed up icontains and similarity matching on searched columns.
COLUMNS = {
    "locations_site": ("name", "code"),
    "locations_building": ("name",),
    "locations_floor": ("name",),
    "locations_room": ("name",),
    "dcim_rack": ("name", "serial", "asset_tag"),
    "dcim_device": ("name", "serial", "asset_tag"),
    "dcim_port": ("name", "label"),
    "cabling_cable": ("label",),
    "networks_vlan": ("name",),
    "networks_wlan": ("ssid",),
}
CREATE = [
    f"CREATE INDEX {table}_{column}_trgm ON {table} USING gin ({column} gin_trgm_ops);"
    for table, columns in COLUMNS.items()
    for column in columns
] + ["CREATE INDEX metadata_note_body_fts ON metadata_note USING gin (to_tsvector('simple', body));"]
DROP = [
    f"DROP INDEX {table}_{column}_trgm;" for table, columns in COLUMNS.items() for column in columns
] + ["DROP INDEX metadata_note_body_fts;"]


class Migration(migrations.Migration):
    dependencies = [
        ("search", "0001_initial"),
        ("locations", "0003_room_outline"),
        ("dcim", "0008_device_plan_x_device_plan_y_rack_plan_rotation_and_more"),
        ("cabling", "0002_integrity"),
        ("networks", "0002_integrity"),
        ("metadata", "0001_initial"),
    ]

    operations = [migrations.RunSQL(CREATE, DROP)]
