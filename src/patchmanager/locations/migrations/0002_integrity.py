from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity

# Every node must belong to its parent's tenant and carry the path
# <parent path>.<own id>. Sites hang directly below their tenant. Rejecting
# anything else in the database means a bug or a crafted request can never
# attach an object to another tenant's tree or forge a permission scope.
FUNCTION = r"""
CREATE FUNCTION pm_check_hierarchy() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    parent_table text := TG_ARGV[0];
    parent_column text := TG_ARGV[1];
    parent_id uuid;
    parent_tenant uuid;
    parent_path ltree;
    own_label text := replace(NEW.id::text, '-', '');
BEGIN
    IF parent_table = 'tenant' THEN
        IF NEW.path <> text2ltree(replace(NEW.tenant_id::text, '-', '') || '.' || own_label) THEN
            RAISE EXCEPTION 'path of % does not match its tenant', TG_TABLE_NAME;
        END IF;
        RETURN NEW;
    END IF;
    parent_id := (to_jsonb(NEW) ->> parent_column)::uuid;
    EXECUTE format('SELECT tenant_id, path FROM %I WHERE id = $1', parent_table)
        INTO parent_tenant, parent_path USING parent_id;
    IF parent_tenant IS NULL OR parent_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION '% must belong to the tenant of its parent', TG_TABLE_NAME;
    END IF;
    IF NEW.path <> parent_path || text2ltree(own_label) THEN
        RAISE EXCEPTION 'path of % does not match its parent', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END;
$$;
"""


def trigger(table: str, parent_table: str, column: str) -> str:
    return (
        f"CREATE TRIGGER {table}_hierarchy BEFORE INSERT OR UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION pm_check_hierarchy('{parent_table}', '{column}');"
    )


class Migration(migrations.Migration):
    dependencies = [("locations", "0001_initial")]

    operations = [
        migrations.RunSQL(FUNCTION, "DROP FUNCTION pm_check_hierarchy();"),
        migrations.RunSQL(
            trigger("locations_site", "tenant", "tenant_id"),
            "DROP TRIGGER locations_site_hierarchy ON locations_site;",
        ),
        migrations.RunSQL(
            trigger("locations_building", "locations_site", "site_id"),
            "DROP TRIGGER locations_building_hierarchy ON locations_building;",
        ),
        migrations.RunSQL(
            trigger("locations_floor", "locations_building", "building_id"),
            "DROP TRIGGER locations_floor_hierarchy ON locations_floor;",
        ),
        migrations.RunSQL(
            trigger("locations_room", "locations_floor", "floor_id"),
            "DROP TRIGGER locations_room_hierarchy ON locations_room;",
        ),
        EnableRowLevelSecurity("site"),
        EnableRowLevelSecurity("building"),
        EnableRowLevelSecurity("floor"),
        EnableRowLevelSecurity("room"),
    ]
