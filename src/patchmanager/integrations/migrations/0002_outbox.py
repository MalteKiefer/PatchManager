from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity

# Every history row becomes a pending delivery for each matching active webhook,
# in the same transaction: no event is lost and none is sent for rolled back work.
OUTBOX = r"""
CREATE FUNCTION pm_webhook_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    event text := NEW.object_type || '.' || NEW.action;
BEGIN
    INSERT INTO integrations_delivery (id, created_at, updated_at, tenant_id, webhook_id, change_id, event,
                                       status, attempts, next_attempt_at, last_error)
    SELECT uuidv7(), now(), now(), w.tenant_id, w.id, NEW.id, event, 'pending', 0, now(), ''
    FROM integrations_webhook w
    WHERE w.active AND w.tenant_id = NEW.tenant_id
      AND EXISTS (SELECT 1 FROM unnest(w.events) AS p WHERE event LIKE replace(p, '*', '%'));
    RETURN NULL;
END;
$$;
CREATE TRIGGER history_change_webhooks AFTER INSERT ON history_change
    FOR EACH ROW EXECUTE FUNCTION pm_webhook_outbox();
"""


class Migration(migrations.Migration):
    dependencies = [("integrations", "0001_initial"), ("history", "0002_triggers")]

    operations = [
        migrations.RunSQL(
            OUTBOX,
            "DROP TRIGGER history_change_webhooks ON history_change; DROP FUNCTION pm_webhook_outbox();",
        ),
        EnableRowLevelSecurity("webhook"),
        EnableRowLevelSecurity("delivery"),
    ]
