import os
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# Note: You will need to install pydrive2: pip install pydrive2
# And you will need to place your 'client_secrets.json' in this folder.
# Read more about setting up PyDrive2 here: https://docs.iterative.ai/PyDrive2/

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'db.sqlite3')

def backup_db_to_drive():
    try:
        gauth = GoogleAuth()
        
        # Try to load saved client credentials
        gauth.LoadCredentialsFile("mycreds.txt")
        if gauth.credentials is None:
            # Authenticate if they're not there
            gauth.LocalWebserverAuth()
        elif gauth.access_token_expired:
            # Refresh them if expired
            gauth.Refresh()
        else:
            # Initialize the saved creds
            gauth.Authorize()
            
        # Save the current credentials to a file
        gauth.SaveCredentialsFile("mycreds.txt")

        drive = GoogleDrive(gauth)

        print(f"Uploading {DB_PATH} to Google Drive...")
        
        file_drive = drive.CreateFile({'title': 'pharmacy_ledger_backup_db.sqlite3'})  
        file_drive.SetContentFile(DB_PATH) 
        file_drive.Upload()
        
        print("Backup successful!")
    except Exception as e:
        print(f"Backup failed: {e}")

if __name__ == "__main__":
    backup_db_to_drive()
