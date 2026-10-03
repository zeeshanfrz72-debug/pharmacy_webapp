"""Run real Chromium forms and queue tests against a disposable application."""
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
    with tempfile.TemporaryDirectory(prefix='invoice-browser-') as directory:
        temp = Path(directory); audit.setup(temp / 'browser.sqlite3')
        firm, rep = audit.account()
        first, second = audit.create(firm, rep, '1000')['bill'], audit.create(firm, rep, '2000')['bill']
        market, _ = audit.account(local=True)
        from ledger.models import Representative, Bill, Payment
        local_rep = Representative.objects.create(firm=market, name='Optional local representative')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        output = audit.ROOT / 'docs/accounting-bill-browser.json'
        fixture = dict(base=base, firm=firm.pk, rep=rep.pk, first=first.pk, second=second.pk,
            market=market.pk, local_rep=local_rep.pk, session=audit.client.cookies['sessionid'].value,
            chrome=r'C:\Program Files\Google\Chrome\Application\chrome.exe', output=str(output))
        config = temp / 'fixture.json'; config.write_text(json.dumps(fixture), encoding='utf-8')
        runtime = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
        env = os.environ.copy(); env['NODE_PATH'] = str(runtime / 'node_modules')
        with (temp / 'server.log').open('wb') as log:
            server = subprocess.Popen([sys.executable, 'manage.py', 'runserver', f'127.0.0.1:{port}', '--noreload'], cwd=audit.ROOT,
                stdout=log, stderr=log, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                for _ in range(150):
                    try:
                        urllib.request.urlopen(base + '/accounts/login/', timeout=1).close(); break
                    except (urllib.error.URLError, ConnectionError):
                        time.sleep(0.2)
                else:
                    raise RuntimeError('Disposable application failed to start')
                subprocess.run([str(runtime / 'bin/node.exe'), 'scripts/accounting_bill_browser.cjs', str(config)], cwd=audit.ROOT, env=env, timeout=240, check=True)
                first.refresh_from_db()
                assert not first.is_disabled and first.remaining_balance == 600
                assert audit.sql_balance(firm) == 3400
                assert Bill.objects.filter(firm=market, bill_number='').count() == 2
                assert audit.sql_balance(market) == 141 and Payment.objects.filter(firm=market).count() == 0
                result = json.loads(output.read_text(encoding='utf-8'))
                result['persisted_verification'] = {'firm_balance': '3400.00', 'older_bill_balance': '600.00', 'market_balance': '141.00', 'market_payments': 0}
                output.write_text(json.dumps(result, indent=2), encoding='utf-8')
            finally:
                server.terminate(); server.wait(timeout=20)
                from django.db import connections
                connections.close_all()
    print(f'Passed real browser invoice checks: {output}', flush=True)


if __name__ == '__main__':
    main()
