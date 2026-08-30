"""Run gmail OAuth completion programmatically using the code from file."""
import os
import sys
import json
from pathlib import Path

os.environ["OAUTH_CODE_FILE"] = r"C:\Users\bmurt\AppData\Local\Temp\gmail_code.txt"

from src.gmail_sender import GMAIL_SCOPES, _client_config, _token_path
from google_auth_oauthlib.flow import InstalledAppFlow

code_file = Path(r"C:\Users\bmurt\AppData\Local\Temp\gmail_code.txt")
raw = code_file.read_bytes()
# strip UTF-8 BOM if present
if raw.startswith(b'\xef\xbb\xbf'):
    raw = raw[3:]
code = raw.decode('utf-8').strip()
print(f"Read code: {code[:30]}...")

flow = InstalledAppFlow.from_client_config(
    _client_config(),
    scopes=GMAIL_SCOPES,
)
flow.redirect_uri = "http://127.0.0.1:18099"

try:
    flow.fetch_token(code=code)
    token_path = _token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(flow.credentials.to_json(), encoding="utf-8")
    print(f"Saved token to {token_path}")

    creds_dict = json.loads(token_path.read_text())
    print(f"Scopes in new token: {creds_dict.get('scopes')}")
except Exception as e:
    print(f"FAIL: {e}")
    import traceback
    traceback.print_exc()
