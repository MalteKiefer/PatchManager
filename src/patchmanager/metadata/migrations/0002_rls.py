from django.db import migrations

from patchmanager.tenancy.rls import EnableRowLevelSecurity


class Migration(migrations.Migration):
    dependencies = [("metadata", "0001_initial")]

    operations = [
        EnableRowLevelSecurity("attachment", shared=True),
        EnableRowLevelSecurity("note", shared=True),
    ]
