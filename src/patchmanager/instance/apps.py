from django.apps import AppConfig


class InstanceConfig(AppConfig):
    name = "patchmanager.instance"
    label = "instance"
    verbose_name = "Instance settings"

    def ready(self) -> None:
        from patchmanager.instance import jobs  # noqa: F401  # connects the job monitor signals
