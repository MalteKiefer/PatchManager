"""Fetch a dump from the off-site copy: ``manage.py offsite_fetch patchmanager-<stamp>.dump``."""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from patchmanager.accounts.services import ServiceError
from patchmanager.instance import config, offsite


class Command(BaseCommand):
    help = "Restore a dump and its uploads archive from the off-site copy into a folder."

    def add_arguments(self, parser: CommandParser) -> None:
        """The dump name and where to put it."""
        parser.add_argument("name", help="for example patchmanager-20261022T030000Z.dump")
        parser.add_argument("--target", default="/restore", help="folder (default /restore)")

    def handle(self, *args: Any, **options: Any) -> None:
        """Run restic restore with the settings from the admin area."""
        try:
            offsite.fetch(config.get(), options["name"], options["target"])
        except ServiceError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"restored into {options['target']}")
