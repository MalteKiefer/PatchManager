from typing import Any

from django.apps import AppConfig
from django.core import checks


class ExchangeConfig(AppConfig):
    name = "patchmanager.exchange"
    label = "exchange"
    verbose_name = "Import and export"

    def ready(self) -> None:
        checks.register(check_logo)


def check_logo(**kwargs: Any) -> list[checks.CheckMessage]:
    """A configured logo must be usable, so a broken mount shows at start, not in a report."""
    from patchmanager.exchange.pdf import logo_problem

    problem = logo_problem()
    return [checks.Error(problem, id="patchmanager.E010")] if problem else []
