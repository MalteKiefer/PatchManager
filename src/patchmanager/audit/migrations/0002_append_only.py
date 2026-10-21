from django.db import migrations

FORWARD = """
CREATE FUNCTION audit_reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit events are append only';
END;
$$;
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit_auditevent
    FOR EACH ROW EXECUTE FUNCTION audit_reject_change();
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_auditevent
    FOR EACH STATEMENT EXECUTE FUNCTION audit_reject_change();
"""

BACKWARD = """
DROP TRIGGER audit_no_truncate ON audit_auditevent;
DROP TRIGGER audit_append_only ON audit_auditevent;
DROP FUNCTION audit_reject_change();
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
