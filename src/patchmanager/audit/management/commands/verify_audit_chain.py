from typing import Any

from django.core.management.base import BaseCommand, CommandError

from patchmanager.audit import services


class Command(BaseCommand):
    help = "Verify the audit log hash chain. Exits non-zero if it is broken."

    def handle(self, *args: Any, **options: Any) -> None:
        result = services.verify()
        if not result.ok:
            raise CommandError(f"chain broken at seq {result.broken_at}: {result.reason}")
        self.stdout.write(f"ok: {result.checked} events verified")
