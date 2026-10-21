from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity

# The VLAN a port uses must be tenant wide or belong to the port's site
# (second label of the device path). Shared by untagged and tagged use.
FUNCTIONS = r"""
CREATE FUNCTION pm_vlan_fits_port(vlan uuid, port uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT v.tenant_id = p.tenant_id AND (v.site_id IS NULL OR s.path = subpath(d.path, 0, 2))
    FROM networks_vlan v
    JOIN dcim_port p ON p.id = port
    JOIN dcim_device d ON d.id = p.device_id
    LEFT JOIN locations_site s ON s.id = v.site_id
    WHERE v.id = vlan
$$;

CREATE FUNCTION pm_check_vlan() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.site_id IS NOT NULL AND NEW.tenant_id IS DISTINCT FROM
        (SELECT tenant_id FROM locations_site WHERE id = NEW.site_id) THEN
        RAISE EXCEPTION 'VLAN site must belong to the VLAN tenant';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER networks_vlan_check BEFORE INSERT OR UPDATE ON networks_vlan
    FOR EACH ROW EXECUTE FUNCTION pm_check_vlan();

CREATE FUNCTION pm_check_wlan() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.vlan_id IS NOT NULL AND NEW.tenant_id IS DISTINCT FROM
        (SELECT tenant_id FROM networks_vlan WHERE id = NEW.vlan_id) THEN
        RAISE EXCEPTION 'WLAN VLAN must belong to the WLAN tenant';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER networks_wlan_check BEFORE INSERT OR UPDATE ON networks_wlan
    FOR EACH ROW EXECUTE FUNCTION pm_check_wlan();

CREATE FUNCTION pm_check_broadcast() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM networks_wlan WHERE id = NEW.wlan_id)
        OR NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM dcim_port WHERE id = NEW.port_id) THEN
        RAISE EXCEPTION 'WLAN broadcast must stay inside one tenant';
    END IF;
    IF (SELECT kind FROM dcim_port WHERE id = NEW.port_id) <> 'wireless' THEN
        RAISE EXCEPTION 'only wireless ports broadcast WLANs';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER networks_wlanbroadcast_check BEFORE INSERT OR UPDATE ON networks_wlanbroadcast
    FOR EACH ROW EXECUTE FUNCTION pm_check_broadcast();

CREATE FUNCTION pm_check_tagged() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM dcim_port WHERE id = NEW.port_id)
        OR NOT coalesce(pm_vlan_fits_port(NEW.vlan_id, NEW.port_id), false) THEN
        RAISE EXCEPTION 'tagged VLAN must belong to the port tenant and site';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER networks_taggedvlan_check BEFORE INSERT OR UPDATE ON networks_taggedvlan
    FOR EACH ROW EXECUTE FUNCTION pm_check_tagged();

CREATE FUNCTION pm_check_port_vlan() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.untagged_vlan_id IS NOT NULL AND NOT coalesce(pm_vlan_fits_port(NEW.untagged_vlan_id, NEW.id), false) THEN
        RAISE EXCEPTION 'untagged VLAN must belong to the port tenant and site';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER dcim_port_vlan AFTER INSERT OR UPDATE OF untagged_vlan_id ON dcim_port
    FOR EACH ROW EXECUTE FUNCTION pm_check_port_vlan();
"""

DROP = """
DROP TRIGGER dcim_port_vlan ON dcim_port; DROP FUNCTION pm_check_port_vlan();
DROP TRIGGER networks_taggedvlan_check ON networks_taggedvlan; DROP FUNCTION pm_check_tagged();
DROP TRIGGER networks_wlanbroadcast_check ON networks_wlanbroadcast; DROP FUNCTION pm_check_broadcast();
DROP TRIGGER networks_wlan_check ON networks_wlan; DROP FUNCTION pm_check_wlan();
DROP TRIGGER networks_vlan_check ON networks_vlan; DROP FUNCTION pm_check_vlan();
DROP FUNCTION pm_vlan_fits_port(uuid, uuid);
"""


class Migration(migrations.Migration):
    dependencies = [
        ("networks", "0001_initial"),
        ("dcim", "0007_port_untagged_vlan_port_vlan_mode"),
    ]

    operations = [
        migrations.RunSQL(FUNCTIONS, DROP),
        EnableRowLevelSecurity("vlan"),
        EnableRowLevelSecurity("wlan"),
        EnableRowLevelSecurity("wlanbroadcast"),
        EnableRowLevelSecurity("taggedvlan"),
    ]
