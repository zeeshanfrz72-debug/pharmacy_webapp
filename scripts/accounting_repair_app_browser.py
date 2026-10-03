"""Seed, launch, and remove a local disposable Django app for Chromium tests."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import accounting_audit as audit


def main():
    with tempfile.TemporaryDirectory(prefix="ledger-app-browser-") as directory:
        temp = Path(directory)
        audit.setup(temp / "browser.sqlite3")
        firm, rep = audit.account()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        output = audit.ROOT / "docs/accounting-repair-app-browser.json"
        fixture = {"base": base, "firm": firm.pk, "rep": rep.pk, "session": audit.client.cookies['sessionid'].value,
                   "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe", "output": str(output)}
        config = temp / "browser-fixture.json"; config.write_text(json.dumps(fixture), encoding="utf-8")
        runtime = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
        env = os.environ.copy(); env["NODE_PATH"] = str(runtime / "node_modules")
        with (temp / "server.log").open("wb") as log:
            server = subprocess.Popen([sys.executable, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"], cwd=audit.ROOT,
                stdout=log, stderr=log, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                for _ in range(100):
                    try:
                        urllib.request.urlopen(base + "/accounts/login/", timeout=1).close(); break
                    except (urllib.error.URLError, ConnectionError):
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Disposable application failed to start")
                result = subprocess.run([str(runtime / "bin/node.exe"), "scripts/accounting_repair_app_browser.cjs", str(config)], cwd=audit.ROOT, env=env, timeout=180)
                assert result.returncode == 0
                from ledger.models import Bill
                bills = list(Bill.objects.filter(firm=firm).values_list("bill_number", "bill_amount"))
                assert len(bills) == 3 and audit.sql_balance(firm) == 270, bills
                evidence = json.loads(output.read_text(encoding="utf-8"))
                evidence["persisted_bills"] = [(number, str(amount)) for number, amount in bills]
                evidence["persisted_balance"] = "270.00"
                output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            finally:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    server.terminate()
                server.wait(timeout=20)
                from django.db import connections
                connections.close_all()
        from django.db import connections
        connections.close_all()
    print(f"Verified actual application: {output}", flush=True)


if __name__ == "__main__":
    main()
