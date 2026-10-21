from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity

# A cable end lives in its cable's tenant, and so does its port: cables never
# connect two tenants.
CABLE_END = r"""
CREATE FUNCTION pm_check_cableend() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM cabling_cable WHERE id = NEW.cable_id)
        OR NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM dcim_port WHERE id = NEW.port_id) THEN
        RAISE EXCEPTION 'cable ends must belong to the tenant of the cable and of the port';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER cabling_cableend_tenant BEFORE INSERT OR UPDATE ON cabling_cableend
    FOR EACH ROW EXECUTE FUNCTION pm_check_cableend();
"""


class Migration(migrations.Migration):
    dependencies = [("cabling", "0001_initial")]

    operations = [
        migrations.RunSQL(
            CABLE_END,
            "DROP TRIGGER cabling_cableend_tenant ON cabling_cableend; DROP FUNCTION pm_check_cableend();",
        ),
        EnableRowLevelSecurity("cable"),
        EnableRowLevelSecurity("cableend"),
    ]
