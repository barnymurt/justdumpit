"""Single-shot gmail auth exchange. Generates URL with PKCE,
user pastes the redirect URL back, exchange uses the matching verifier."""
import secrets
import hashlib
import base64
import urllib.parse
import sys

# Generate PKCE pair
code_verifier = secrets.token_urlsafe(64)
# Trim to 43-128 chars (between 43 and 128 per RFC 7636)
code_verifier = code_verifier[:128]
raw = hashlib.sha256(code_verifier.encode("ascii")).digest()
code_challenge = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

client_id = "258147166506-6pp39dbdkmcgho90dltu092e22tq4s5m.apps.googleusercontent.com"
scope = "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly"
redirect_uri = "http://127.0.0.1:18099"

state_token = secrets.token_urlsafe(16)

params = {
    "response_type": "code",
    "client_id": client_id,
    "redirect_uri": redirect_uri,
    "scope": scope,
    "state": state_token,
    "code_challenge": code_challenge,
    "code_challenge_method": "S256",
    "prompt": "consent",
    "access_type": "offline",
    "include_granted_scopes": "true",
}
url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)

# Print URL + verifier (both needed for exchange)
print("=" * 80)
print("URL:")
print(url)
print("=" * 80)
print("CODE_VERIFIER (need this for exchange):")
print(code_verifier)
print("=" * 80)
