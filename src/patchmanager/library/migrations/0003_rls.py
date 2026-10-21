from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity

# A port template always carries its device type's tenant.
PORT_TEMPLATE_TENANT = r"""
CREATE FUNCTION pm_check_porttemplate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM library_devicetype WHERE id = NEW.device_type_id) THEN
        RAISE EXCEPTION 'port template must share the tenant of its device type';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER library_porttemplate_tenant BEFORE INSERT OR UPDATE ON library_porttemplate
    FOR EACH ROW EXECUTE FUNCTION pm_check_porttemplate();
"""


class Migration(migrations.Migration):
    dependencies = [("library", "0002_initial")]

    operations = [
        migrations.RunSQL(
            PORT_TEMPLATE_TENANT,
            "DROP TRIGGER library_porttemplate_tenant ON library_porttemplate; DROP FUNCTION pm_check_porttemplate();",
        ),
        EnableRowLevelSecurity("devicetype", shared=True),
        EnableRowLevelSecurity("porttemplate", shared=True),
    ]
