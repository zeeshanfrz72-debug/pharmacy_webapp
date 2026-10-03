from django.core.management.base import BaseCommand, CommandError
from ledger.backup import create_snapshot


class Command(BaseCommand):
    help = "Create and verify a consistent snapshot of the configured SQLite database."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        try:
            result = create_snapshot(options["output"])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Verified backup: {options['output']} ({result['ledger_entries']} entries)"))
