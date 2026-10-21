from django.apps import AppConfig


class ApiConfig(AppConfig):
    name = "patchmanager.api"
    label = "api"
    verbose_name = "API"

    def ready(self) -> None:
        from patchmanager.accounts import permissions
        from patchmanager.api import schema  # noqa: F401  # registers the OpenAPI extension

        permissions.register("accounts.apiclient", "API client", actions=("view", "manage"))
