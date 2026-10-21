from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity

# A device hangs below its rack (if mounted) or its room, in that tenant; a
# mounted device's rack must stand in the device's room. The device type must
# be shared or private to the same tenant.
DEVICE = r"""
CREATE FUNCTION pm_check_device() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    parent_tenant uuid;
    parent_path ltree;
    rack_room uuid;
    type_tenant uuid;
BEGIN
    IF NEW.rack_id IS NOT NULL THEN
        SELECT tenant_id, path, room_id INTO parent_tenant, parent_path, rack_room FROM dcim_rack WHERE id = NEW.rack_id;
        IF rack_room IS DISTINCT FROM NEW.room_id THEN
            RAISE EXCEPTION 'device rack must stand in the device room';
        END IF;
    ELSE
        SELECT tenant_id, path INTO parent_tenant, parent_path FROM locations_room WHERE id = NEW.room_id;
    END IF;
    IF parent_tenant IS NULL OR parent_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'device must belong to the tenant of its parent';
    END IF;
    IF NEW.path <> parent_path || text2ltree(replace(NEW.id::text, '-', '')) THEN
        RAISE EXCEPTION 'path of device does not match its parent';
    END IF;
    SELECT tenant_id INTO type_tenant FROM library_devicetype WHERE id = NEW.device_type_id;
    IF type_tenant IS NOT NULL AND type_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'device type is private to another tenant';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER dcim_device_hierarchy BEFORE INSERT OR UPDATE ON dcim_device
    FOR EACH ROW EXECUTE FUNCTION pm_check_device();
"""

# Ports share their device's tenant; a rear port must belong to the same device.
PORT = r"""
CREATE FUNCTION pm_check_port() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM dcim_device WHERE id = NEW.device_id) THEN
        RAISE EXCEPTION 'port must belong to the tenant of its device';
    END IF;
    IF NEW.rear_port_id IS NOT NULL AND NEW.device_id IS DISTINCT FROM
        (SELECT device_id FROM dcim_port WHERE id = NEW.rear_port_id) THEN
        RAISE EXCEPTION 'rear port must belong to the same device';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER dcim_port_check BEFORE INSERT OR UPDATE ON dcim_port
    FOR EACH ROW EXECUTE FUNCTION pm_check_port();
"""

# Space taken by a device lies in the device's rack.
SPACE_DEVICE = r"""
CREATE FUNCTION pm_check_rackspace_device() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.device_id IS NOT NULL AND NEW.rack_id IS DISTINCT FROM
        (SELECT rack_id FROM dcim_device WHERE id = NEW.device_id) THEN
        RAISE EXCEPTION 'rack space of a device must be in the device rack';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER dcim_rackspace_device BEFORE INSERT OR UPDATE ON dcim_rackspace
    FOR EACH ROW EXECUTE FUNCTION pm_check_rackspace_device();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("dcim", "0005_device_device_type_device_rack_device_room_and_more"),
        ("library", "0003_rls"),
    ]

    operations = [
        migrations.RunSQL(
            DEVICE,
            "DROP TRIGGER dcim_device_hierarchy ON dcim_device; DROP FUNCTION pm_check_device();",
        ),
        migrations.RunSQL(
            PORT, "DROP TRIGGER dcim_port_check ON dcim_port; DROP FUNCTION pm_check_port();"
        ),
        migrations.RunSQL(
            SPACE_DEVICE,
            "DROP TRIGGER dcim_rackspace_device ON dcim_rackspace; DROP FUNCTION pm_check_rackspace_device();",
        ),
        EnableRowLevelSecurity("device"),
        EnableRowLevelSecurity("port"),
    ]
