"""One-time OAuth authorization for Google Drive/Sheets access.

Run this once locally (NOT in Docker — it needs to open a browser window
on this machine):

    pip install google-auth-oauthlib
    python scripts/google_oauth_setup.py

Logs you into your own Google account and saves a refresh token to
backend/google-oauth-token.json. The backend then acts as *you* for all
Drive/Sheets API calls — files it creates count against your own Drive
storage, sidestepping the "service accounts have no storage quota" wall.
"""

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRET_PATH = ROOT / "backend" / "google-oauth-client.json"
TOKEN_PATH = ROOT / "backend" / "google-oauth-token.json"


def main() -> None:
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    print(f"Saved token to {TOKEN_PATH}")


if __name__ == "__main__":
    main()
