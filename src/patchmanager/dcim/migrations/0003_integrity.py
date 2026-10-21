from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity

RACK_TRIGGER = (
    "CREATE TRIGGER dcim_rack_hierarchy BEFORE INSERT OR UPDATE ON dcim_rack "
    "FOR EACH ROW EXECUTE FUNCTION pm_check_hierarchy('locations_room', 'room_id');"
)

# Rack space must stay inside the rack and belong to the rack's tenant.
SPACE_FUNCTION = r"""
CREATE FUNCTION pm_check_rackspace() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    rack_tenant uuid;
    rack_height integer;
BEGIN
    SELECT tenant_id, u_height INTO rack_tenant, rack_height FROM dcim_rack WHERE id = NEW.rack_id;
    IF rack_tenant IS NULL OR rack_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'rack space must belong to the tenant of its rack';
    END IF;
    IF NOT (NEW.units <@ int4range(1, rack_height + 1)) OR isempty(NEW.units) THEN
        RAISE EXCEPTION 'rack space % lies outside the rack (1 to %)', NEW.units, rack_height;
    END IF;
    IF NOT (NEW.faces <@ int4range(0, 2)) OR isempty(NEW.faces) THEN
        RAISE EXCEPTION 'invalid rack face range %', NEW.faces;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER dcim_rackspace_check BEFORE INSERT OR UPDATE ON dcim_rackspace
    FOR EACH ROW EXECUTE FUNCTION pm_check_rackspace();
"""

# Shrinking a rack must not cut off occupied units.
HEIGHT_FUNCTION = r"""
CREATE FUNCTION pm_check_rack_height() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.u_height < OLD.u_height AND EXISTS (
        SELECT 1 FROM dcim_rackspace WHERE rack_id = NEW.id AND upper(units) > NEW.u_height + 1
    ) THEN
        RAISE EXCEPTION 'rack height % would cut off occupied units', NEW.u_height;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER dcim_rack_height BEFORE UPDATE OF u_height ON dcim_rack
    FOR EACH ROW EXECUTE FUNCTION pm_check_rack_height();
"""


class Migration(migrations.Migration):
    dependencies = [("dcim", "0002_initial"), ("locations", "0002_integrity")]

    operations = [
        migrations.RunSQL(RACK_TRIGGER, "DROP TRIGGER dcim_rack_hierarchy ON dcim_rack;"),
        migrations.RunSQL(
            SPACE_FUNCTION,
            "DROP TRIGGER dcim_rackspace_check ON dcim_rackspace; DROP FUNCTION pm_check_rackspace();",
        ),
        migrations.RunSQL(
            HEIGHT_FUNCTION,
            "DROP TRIGGER dcim_rack_height ON dcim_rack; DROP FUNCTION pm_check_rack_height();",
        ),
        EnableRowLevelSecurity("rack"),
        EnableRowLevelSecurity("rackspace"),
    ]
