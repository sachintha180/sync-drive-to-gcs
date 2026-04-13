import argparse
from typing import List
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

parser = argparse.ArgumentParser(description="Generate a Google OAuth refresh token.")
parser.add_argument(
    "--scopes",
    required=True,
    help="Comma-separated list of OAuth scopes (e.g. https://www.googleapis.com/auth/drive.readonly)",
)
args = parser.parse_args()

GCP_SCOPES: List[str] = [s.strip() for s in args.scopes.split(",")]
SECRETS_DIR = Path(__name__).parent / "secrets"

flow = InstalledAppFlow.from_client_secrets_file(
    SECRETS_DIR / "drive-reader-client-secret.json", GCP_SCOPES
)
creds = flow.run_local_server(port=8080, open_browser=True)

oauth_fp = SECRETS_DIR / "drive-reader-oauth.json"
with open(oauth_fp, "w", encoding="utf-8") as f:
    f.write(creds.to_json())

print(f"OAuth details written to {oauth_fp}.")
