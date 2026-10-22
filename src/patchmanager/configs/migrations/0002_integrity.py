from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity

SQL = r"""
CREATE FUNCTION pm_check_deviceconfig() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM dcim_device WHERE id = NEW.device_id) THEN
        RAISE EXCEPTION 'a configuration belongs to the tenant of its device';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER configs_deviceconfig_check BEFORE INSERT OR UPDATE ON configs_deviceconfig
    FOR EACH ROW EXECUTE FUNCTION pm_check_deviceconfig();
"""
DROP = """
DROP TRIGGER configs_deviceconfig_check ON configs_deviceconfig;
DROP FUNCTION pm_check_deviceconfig();
"""


class Migration(migrations.Migration):
    dependencies = [("configs", "0001_initial")]

    operations = [
        migrations.RunSQL(SQL, DROP),
        EnableRowLevelSecurity("deviceconfig"),
    ]
