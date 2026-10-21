import ipaddress
from typing import Any

from django.apps import AppConfig
from django.conf import settings
from django.core import checks


class IntegrationsConfig(AppConfig):
    name = "patchmanager.integrations"
    label = "integrations"
    verbose_name = "Integrations"

    def ready(self) -> None:
        from patchmanager.accounts import permissions

        permissions.register(
            "integrations.webhook", "Webhook", actions=("manage",)
        )  # URLs may hold secrets
        checks.register(check_networks)


def check_networks(**kwargs: Any) -> list[checks.CheckMessage]:
    """Fail at start if PATCHMANAGER_WEBHOOK_ALLOWED_NETWORKS has a typo."""
    errors: list[checks.CheckMessage] = []
    for net in settings.WEBHOOK_ALLOWED_NETWORKS:
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError:
            errors.append(
                checks.Error(
                    f"Invalid network in PATCHMANAGER_WEBHOOK_ALLOWED_NETWORKS: {net}",
                    id="patchmanager.E020",
                )
            )
    return errors
