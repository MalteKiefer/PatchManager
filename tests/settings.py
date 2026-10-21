from patchmanager.settings import *  # noqa: F403

# collectstatic and the CSS build do not run before unit tests.
STORAGES = {
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "attachments": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
}
