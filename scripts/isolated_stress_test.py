"""Exercise concurrent ledger writes against a disposable synthetic SQLite DB.

Run from the project root with the project virtualenv's Python interpreter:

    python scripts/isolated_stress_test.py

The script never opens the configured application database. It creates a fresh
database in the operating system temp directory and deletes it when finished.
"""

from __future__ import annotations

import concurrent.futures
import http.cookiejar
import json
import os
import pathlib
import re
import secrets
import socket
import subprocess  # nosec B404
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal


ROOT = pathlib.Path(__file__).resolve().parents[1]
PYTHON = pathlib.Path(sys.executable)
OWNER_USERNAME = "stress-owner"
OWNER_PASSWORD = secrets.token_urlsafe(24)
RAMPS = (5, 10, 20, 40)


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self.csrf = self._read_csrf("/accounts/login/?next=%2Fledger%2Fadd-transaction%2F")
        login_body = urllib.parse.urlencode(
            {
                "csrfmiddlewaretoken": self.csrf,
                "username": OWNER_USERNAME,
                "password": OWNER_PASSWORD,
                "next": "/ledger/add-transaction/",
            }
        ).encode()
        response = self.opener.open(
            urllib.request.Request(
                self.base_url + "/accounts/login/",
                data=login_body,
                headers={"Referer": self.base_url + "/accounts/login/"},
                method="POST",
            ),
            timeout=15,
        )
        if response.status != 200 or "/accounts/login" in response.geturl():
            raise RuntimeError("Synthetic owner login failed.")
        self.csrf = self._read_csrf("/ledger/add-transaction/")

    def _read_csrf(self, path: str) -> str:
        page = self.opener.open(self.base_url + path, timeout=15).read().decode()
        match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
        if not match:
            raise RuntimeError(f"Could not read CSRF token from {path}.")
        return match.group(1)

    def pay(self, firm_id: int, representative_id: int) -> tuple[int | None, float, bool, str]:
        body = urllib.parse.urlencode(
            {
                "csrfmiddlewaretoken": self.csrf,
                "request_id": str(uuid.uuid4()),
                "source_type": "distributor",
                "firm": str(firm_id),
                "representative": str(representative_id),
                "bill_choice": "",
                "new_bill_number": "",
                "new_bill_amount": "",
                "payment_choice": "1000",
                "custom_payment_amount": "",
            }
        ).encode()
        request = urllib.request.Request(
            self.base_url + "/ledger/add-transaction/",
            data=body,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.base_url + "/ledger/add-transaction/",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            response = self.opener.open(request, timeout=45)
            status = response.status
            content = response.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            content = exc.read().decode(errors="replace")
        except Exception as exc:  # Keep transport failures in the report.
            elapsed = (time.perf_counter() - started) * 1000
            return None, elapsed, False, f"{type(exc).__name__}: {exc}"
        elapsed = (time.perf_counter() - started) * 1000
        try:
            payload = json.loads(content)
            return status, elapsed, bool(payload.get("success")), str(payload.get("error", ""))
        except json.JSONDecodeError:
            return status, elapsed, False, content[:240]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pharmacy-isolated-stress-") as temp_dir:
        temp_path = pathlib.Path(temp_dir)
        database = temp_path / "synthetic.sqlite3"
        server_log_path = temp_path / "server.log"
        env = os.environ.copy()
        env.update(
            {
                "DJANGO_SETTINGS_MODULE": "pharmacy_app.settings",
                "DJANGO_DB_NAME": str(database),
                "DJANGO_SECRET_KEY": "isolated-stress-key-" + "x" * 64,
                "DJANGO_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
                "APP_OWNER_USERNAME": OWNER_USERNAME,
                "DJANGO_DEBUG": "True",
                "DJANGO_SECURE_SSL_REDIRECT": "False",
                "DJANGO_HSTS_SECONDS": "0",
                "DJANGO_SQLITE_TIMEOUT": "20",
            }
        )
        # Run the fixed migration command against this script's temporary DB.
        migration = subprocess.run(  # nosec B603
            [str(PYTHON), "manage.py", "migrate", "--noinput"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if migration.returncode:
            raise RuntimeError("Synthetic database migration failed: " + migration.stderr[-2000:])

        os.environ.update(env)
        sys.path.insert(0, str(ROOT))
        import django

        django.setup()
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        from ledger.models import Firm, LedgerEntry, Representative

        get_user_model().objects.create_user(
            username=OWNER_USERNAME,
            password=OWNER_PASSWORD,
        )

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"

        reports = []
        with server_log_path.open("w", encoding="utf-8") as server_log:
            # Start only the local Django server for this isolated fixture.
            server = subprocess.Popen(  # nosec B603
                [str(PYTHON), "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
                cwd=ROOT,
                env=env,
                stdout=server_log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.time() + 45
                while time.time() < deadline:
                    if server.poll() is not None:
                        raise RuntimeError("Synthetic Django server exited during startup.")
                    try:
                        # Probe the ephemeral loopback listener created above.
                        urllib.request.urlopen(  # nosec B310
                            base_url + "/accounts/login/", timeout=2
                        ).read()
                        break
                    except Exception:
                        time.sleep(0.25)
                else:
                    raise RuntimeError("Synthetic Django server did not become ready.")

                for client_count in RAMPS:
                    firm = Firm.objects.create(
                        name=f"Synthetic Stress Firm {client_count}",
                        source_type=Firm.SourceType.DISTRIBUTOR,
                    )
                    representative = Representative.objects.create(
                        firm=firm,
                        name=f"Synthetic Representative {client_count}",
                    )
                    LedgerEntry.objects.create(
                        firm=firm,
                        entry_type=LedgerEntry.EntryType.OPENING_BALANCE,
                        date=timezone.localdate(),
                        increase=Decimal("5000.00"),
                        decrease=Decimal("0.00"),
                        description="Synthetic isolated stress-test opening balance",
                    )
                    clients = [Client(base_url) for _ in range(client_count)]
                    wall_started = time.perf_counter()
                    with concurrent.futures.ThreadPoolExecutor(max_workers=client_count) as pool:
                        results = list(
                            pool.map(
                                lambda client: client.pay(firm.pk, representative.pk),
                                clients,
                            )
                        )
                    wall_ms = (time.perf_counter() - wall_started) * 1000
                    latencies = sorted(result[1] for result in results)
                    successes = sum(1 for result in results if result[2])
                    status_counts: dict[str, int] = {}
                    failures = []
                    for result in results:
                        status = str(result[0]) if result[0] is not None else "transport_error"
                        status_counts[status] = status_counts.get(status, 0) + 1
                        if not result[2]:
                            failures.append({"status": status, "detail": result[3]})
                    balance = Firm.objects.get(pk=firm.pk).current_debt()
                    expected_successes = min(client_count, 5)
                    p95_index = max(0, (len(latencies) * 95 + 99) // 100 - 1)
                    reports.append(
                        {
                            "clients": client_count,
                            "successes": successes,
                            "expected_successes": expected_successes,
                            "status_counts": status_counts,
                            "wall_ms": round(wall_ms, 1),
                            "median_ms": round(latencies[len(latencies) // 2], 1),
                            "p95_ms": round(latencies[p95_index], 1),
                            "balance_after": str(balance),
                            "accounting_ok": balance
                            == Decimal(max(0, 5000 - expected_successes * 1000)),
                            "sample_failures": failures[:2],
                        }
                    )
            finally:
                server.terminate()
                server.wait(timeout=15)

        from django.db import connections

        connections.close_all()
        log = server_log_path.read_text(encoding="utf-8", errors="replace").lower()
        print(
            json.dumps(
                {
                    "database": "temporary synthetic SQLite; discarded after run",
                    "client_ramps": reports,
                    "sqlite_locked_messages": log.count("database is locked"),
                    "http_500_messages": log.count('" 500 '),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
