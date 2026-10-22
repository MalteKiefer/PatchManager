from django.apps import AppConfig


class ConfigsConfig(AppConfig):
    name = "patchmanager.configs"
    label = "configs"

    def ready(self) -> None:
        from patchmanager.accounts import permissions

        permissions.register("configs.config", "Device configuration")
