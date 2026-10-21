from django.apps import AppConfig


class CablingConfig(AppConfig):
    name = "patchmanager.cabling"
    label = "cabling"

    def ready(self) -> None:
        from patchmanager.accounts import permissions
        from patchmanager.cabling.models import Cable

        permissions.register(
            "cabling.cable",
            "Cable",
            extra_actions=("trace",),
            condition_attrs=Cable.condition_attrs,
        )
