"""One-time OAuth consent for Google Calendar + Gmail.

1. Google Cloud Console -> create project -> enable "Google Calendar API" and "Gmail API".
2. OAuth consent screen: External, add your Gmail as Test user.
3. Credentials -> Create OAuth client ID -> Desktop app -> download JSON to secrets/google_client_secret.json
4. Run:  python scripts/google_auth.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.providers.calendar import SCOPES  # noqa: E402

s = get_settings()
flow = InstalledAppFlow.from_client_secrets_file(s.google_credentials_file, SCOPES)
creds = flow.run_local_server(port=0, prompt="consent")
Path(s.google_token_file).write_text(creds.to_json())
print(f"OK: token guardado en {s.google_token_file}")
