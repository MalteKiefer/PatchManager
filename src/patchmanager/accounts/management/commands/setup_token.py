from typing import Any

from django.core.management.base import BaseCommand, CommandError

from patchmanager.accounts.services import ServiceError, users


class Command(BaseCommand):
    help = "Print a one hour token for the first run setup page."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            token = users.issue_setup_token()
        except ServiceError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(token)
