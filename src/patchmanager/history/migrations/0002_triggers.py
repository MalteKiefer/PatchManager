from django.db import migrations

from patchmanager.history.registry import TRACKED
from patchmanager.tenancy.rls import EnableRowLevelSecurity

# One row per insert, update or delete, including bulk QuerySet.update() calls.
# Who did it comes from session settings the request middleware sets.
FUNCTION = r"""
CREATE FUNCTION pm_history() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    noise text[] := ARRAY['created_at', 'updated_at', 'created_by_id', 'path'];
    old_row jsonb := CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END;
    new_row jsonb := CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END;
    cur jsonb := coalesce(new_row, old_row);
    changed text[] := '{}';
    scope ltree;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        SELECT coalesce(array_agg(n.key ORDER BY n.key), '{}') INTO changed
        FROM jsonb_each(new_row) AS n
        WHERE NOT n.key = ANY (noise) AND n.value IS DISTINCT FROM old_row -> n.key;
        IF cardinality(changed) = 0 THEN
            RETURN NULL;
        END IF;
    END IF;
    scope := CASE TG_ARGV[2]
        WHEN 'self' THEN (cur ->> 'path')::ltree
        WHEN 'device' THEN (SELECT path FROM dcim_device WHERE id = (cur ->> 'device_id')::uuid)
        WHEN 'site' THEN (SELECT path FROM locations_site WHERE id = (cur ->> 'site_id')::uuid)
    END;
    scope := coalesce(scope, replace(cur ->> 'tenant_id', '-', '')::ltree);
    INSERT INTO history_change (id, occurred_at, tenant_id, object_type, object_id, label, path,
                                action, before, after, fields, actor_id, actor_label, request_id)
    VALUES (uuidv7(), now(), (cur ->> 'tenant_id')::uuid, TG_ARGV[0], (cur ->> 'id')::uuid,
            left(coalesce(cur ->> TG_ARGV[1], ''), 300), scope, lower(TG_OP),
            old_row - noise, new_row - noise, changed,
            nullif(current_setting('patchmanager.actor_id', true), '')::uuid,
            coalesce(current_setting('patchmanager.actor_label', true), ''),
            coalesce(current_setting('patchmanager.request_id', true), ''));
    RETURN NULL;
END;
$$;
"""


# The tables tracked when this migration was written; later apps add their own
# triggers in their migrations (their tables do not exist yet at this point).
FROZEN = [
    t
    for t in TRACKED.values()
    if t.label.split(".")[0] in {"locations", "dcim", "cabling", "networks"}
]


def _table(label: str) -> str:
    return label.replace(".", "_")


TRIGGERS = "\n".join(
    f"CREATE TRIGGER {_table(t.label)}_history AFTER INSERT OR UPDATE OR DELETE ON {_table(t.label)}"
    f" FOR EACH ROW EXECUTE FUNCTION pm_history('{t.label}', '{t.name_column}', '{t.scope}');"
    for t in FROZEN
)
DROP = "\n".join(f"DROP TRIGGER {_table(t.label)}_history ON {_table(t.label)};" for t in FROZEN)


class Migration(migrations.Migration):
    dependencies = [
        ("history", "0001_initial"),
        ("locations", "0003_room_outline"),
        ("dcim", "0008_device_plan_x_device_plan_y_rack_plan_rotation_and_more"),
        ("cabling", "0002_integrity"),
        ("networks", "0002_integrity"),
    ]

    operations = [
        migrations.RunSQL(FUNCTION + TRIGGERS, DROP + "\nDROP FUNCTION pm_history();"),
        EnableRowLevelSecurity("change"),
    ]
