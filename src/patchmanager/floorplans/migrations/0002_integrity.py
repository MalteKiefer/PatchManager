from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity

# A plan belongs to its floor's tenant, and so does its image.
PLAN = r"""
CREATE FUNCTION pm_check_floorplan() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM locations_floor WHERE id = NEW.floor_id)
        OR NEW.tenant_id IS DISTINCT FROM (SELECT tenant_id FROM metadata_attachment WHERE id = NEW.image_id) THEN
        RAISE EXCEPTION 'floor plan must belong to the tenant of its floor and image';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER floorplans_floorplan_tenant BEFORE INSERT OR UPDATE ON floorplans_floorplan
    FOR EACH ROW EXECUTE FUNCTION pm_check_floorplan();
"""


class Migration(migrations.Migration):
    dependencies = [("floorplans", "0001_initial")]

    operations = [
        migrations.RunSQL(
            PLAN,
            "DROP TRIGGER floorplans_floorplan_tenant ON floorplans_floorplan; DROP FUNCTION pm_check_floorplan();",
        ),
        EnableRowLevelSecurity("floorplan"),
    ]
