"""Persistent gmail auth: URL to file, HTTP server, code to file, exchange, save."""
import os
import sys
import json
import time
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

os.environ["OAUTH_CODE_FILE"] = r"C:\Users\bmurt\AppData\Local\Temp\gmail_code.txt"
sys.path.insert(0, r"C:\Dev\justdumpit")

from src.gmail_sender import GMAIL_SCOPES, _client_config, _token_path
from google_auth_oauthlib.flow import InstalledAppFlow

code_file = Path(r"C:\Users\bmurt\AppData\Local\Temp\gmail_code.txt")
url_file = Path(r"C:\Users\bmurt\AppData\Local\Temp\gmail_url.txt")
port = 18099
if code_file.exists():
    code_file.unlink()

flow = InstalledAppFlow.from_client_config(_client_config(), scopes=GMAIL_SCOPES)
flow.redirect_uri = f"http://127.0.0.1:{port}"

captured = {"code": None, "error": None}
event = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if "code" in qs:
            captured["code"] = qs["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>OK</h2>")
        elif "error" in qs:
            captured["error"] = qs["error"][0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Error</h2>")
        else:
            self.send_response(404)
            self.end_headers()
        event.set()

    def log_message(self, *a, **k):
        pass


server = HTTPServer(("127.0.0.1", port), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()

auth_url, _ = flow.authorization_url(
    prompt="consent", access_type="offline", include_granted_scopes="true"
)
url_file.write_text(auth_url, encoding="utf-8")

with open(r"C:\Dev\justdumpit\gmail_url.txt", "w") as f:
    f.write(auth_url + "\n")

print(f"URL:{auth_url}", flush=True)

deadline = time.time() + 600
while time.time() < deadline:
    if event.is_set():
        break
    if code_file.exists():
        try:
            raw = code_file.read_bytes()
            if raw.startswith(b'\xef\xbb\xbf'):
                raw = raw[3:]
            content = raw.decode('utf-8').strip()
            if content:
                captured["code"] = content
                break
        except Exception:
            pass
    time.sleep(1)

server.shutdown()

if captured["error"]:
    print(f"ERROR:{captured['error']}", flush=True)
    sys.exit(1)

if not captured["code"]:
    print("TIMEOUT", flush=True)
    sys.exit(1)

try:
    flow.fetch_token(code=captured["code"])
    token_path = _token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(flow.credentials.to_json(), encoding="utf-8")
    print(f"SAVED:{token_path}", flush=True)
    print(f"SCOPES:{json.loads(token_path.read_text()).get('scopes')}", flush=True)
except Exception as e:
    print(f"FAIL:{e}", flush=True)
