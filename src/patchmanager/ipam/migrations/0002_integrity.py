from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity

# References must stay inside the tenant of the row (also for writes that bypass the
# services). A prefix's site and VLAN, an address's port, and both VRFs.
FUNCTIONS = r"""
CREATE FUNCTION pm_check_prefix() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.vrf_id IS NOT NULL AND NEW.tenant_id IS DISTINCT FROM
            (SELECT tenant_id FROM ipam_vrf WHERE id = NEW.vrf_id))
        OR (NEW.site_id IS NOT NULL AND NEW.tenant_id IS DISTINCT FROM
            (SELECT tenant_id FROM locations_site WHERE id = NEW.site_id))
        OR (NEW.vlan_id IS NOT NULL AND NEW.tenant_id IS DISTINCT FROM
            (SELECT tenant_id FROM networks_vlan WHERE id = NEW.vlan_id)) THEN
        RAISE EXCEPTION 'prefix references must belong to the prefix tenant';
    END IF;
    IF NEW.gateway IS NOT NULL AND NOT (NEW.gateway <<= NEW.prefix) THEN
        RAISE EXCEPTION 'the gateway must lie inside the prefix';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER ipam_prefix_check BEFORE INSERT OR UPDATE ON ipam_prefix
    FOR EACH ROW EXECUTE FUNCTION pm_check_prefix();

CREATE FUNCTION pm_check_ipaddress() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.vrf_id IS NOT NULL AND NEW.tenant_id IS DISTINCT FROM
            (SELECT tenant_id FROM ipam_vrf WHERE id = NEW.vrf_id))
        OR (NEW.port_id IS NOT NULL AND NEW.tenant_id IS DISTINCT FROM
            (SELECT tenant_id FROM dcim_port WHERE id = NEW.port_id)) THEN
        RAISE EXCEPTION 'address references must belong to the address tenant';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER ipam_ipaddress_check BEFORE INSERT OR UPDATE ON ipam_ipaddress
    FOR EACH ROW EXECUTE FUNCTION pm_check_ipaddress();

CREATE INDEX ipam_prefix_prefix_gist ON ipam_prefix USING gist (prefix inet_ops);
CREATE INDEX ipam_ipaddress_address_gist ON ipam_ipaddress USING gist (address inet_ops);
CREATE INDEX ipam_prefix_text_trgm ON ipam_prefix USING gin (text gin_trgm_ops);
CREATE INDEX ipam_ipaddress_host_trgm ON ipam_ipaddress USING gin (host gin_trgm_ops);
CREATE INDEX ipam_ipaddress_dns_trgm ON ipam_ipaddress USING gin (dns_name gin_trgm_ops);

CREATE TRIGGER ipam_vrf_history AFTER INSERT OR UPDATE OR DELETE ON ipam_vrf
    FOR EACH ROW EXECUTE FUNCTION pm_history('ipam.vrf', 'name', 'tenant');
CREATE TRIGGER ipam_prefix_history AFTER INSERT OR UPDATE OR DELETE ON ipam_prefix
    FOR EACH ROW EXECUTE FUNCTION pm_history('ipam.prefix', 'text', 'self');
CREATE TRIGGER ipam_ipaddress_history AFTER INSERT OR UPDATE OR DELETE ON ipam_ipaddress
    FOR EACH ROW EXECUTE FUNCTION pm_history('ipam.ipaddress', 'host', 'self');
"""

DROP = """
DROP TRIGGER ipam_ipaddress_history ON ipam_ipaddress;
DROP TRIGGER ipam_prefix_history ON ipam_prefix;
DROP TRIGGER ipam_vrf_history ON ipam_vrf;
DROP INDEX ipam_ipaddress_dns_trgm;
DROP INDEX ipam_ipaddress_host_trgm;
DROP INDEX ipam_prefix_text_trgm;
DROP INDEX ipam_ipaddress_address_gist;
DROP INDEX ipam_prefix_prefix_gist;
DROP TRIGGER ipam_ipaddress_check ON ipam_ipaddress;
DROP FUNCTION pm_check_ipaddress();
DROP TRIGGER ipam_prefix_check ON ipam_prefix;
DROP FUNCTION pm_check_prefix();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("ipam", "0001_initial"),
        ("history", "0002_triggers"),
        ("networks", "0002_integrity"),
    ]

    operations = [
        migrations.RunSQL(FUNCTIONS, DROP),
        EnableRowLevelSecurity("vrf"),
        EnableRowLevelSecurity("prefix"),
        EnableRowLevelSecurity("ipaddress"),
    ]
