from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity


class Migration(migrations.Migration):
    dependencies = [("tenancy", "0001_initial")]

    operations = [EnableRowLevelSecurity("tenant", column="id")]
