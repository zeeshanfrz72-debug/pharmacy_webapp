"""Optional Drive uploader. It uploads a verified configured-database snapshot.

PyDrive2 and Drive credentials are optional operational dependencies. Failures
propagate so a scheduler cannot mistake a failed backup for success.
"""
from pathlib import Path
import tempfile


def backup_db_to_drive():
    from pydrive2.auth import GoogleAuth
    from pydrive2.drive import GoogleDrive
    from ledger.backup import create_snapshot
    from django.conf import settings
    with tempfile.TemporaryDirectory(prefix="ledger-drive-backup-") as folder:
        snapshot = Path(folder) / "ledger.sqlite3"
        create_snapshot(snapshot)
        gauth = GoogleAuth()
        credentials = str(Path(settings.BASE_DIR) / "mycreds.txt")
        gauth.LoadCredentialsFile(credentials)
        if gauth.credentials is None:
            gauth.LocalWebserverAuth()
        elif gauth.access_token_expired:
            gauth.Refresh()
        else:
            gauth.Authorize()
        gauth.SaveCredentialsFile(credentials)
        remote = GoogleDrive(gauth).CreateFile({'title': 'pharmacy_ledger_backup_db.sqlite3'})
        remote.SetContentFile(str(snapshot))
        remote.Upload()
        return remote['id']


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pharmacy_app.settings")
    import django
    django.setup()
    print(backup_db_to_drive())
