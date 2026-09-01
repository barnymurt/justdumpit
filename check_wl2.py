"""Check what's in the YouTube WL via API."""
import sys
sys.path.insert(0, ".")
import httpx
import json

with open("/data/youtube_token.json") as f:
    t = json.load(f)
token = t["token"]
print(f"Token scope: {t.get('scopes')}")
print(f"Token expires: {t.get('expiry')}")

r = httpx.get(
    "https://www.googleapis.com/youtube/v3/channels",
    params={"part": "snippet,contentDetails", "mine": "true"},
    headers={"Authorization": f"Bearer {token}"},
)
data = r.json()
items = data.get("items", [])
print(f"\nMy channels ({len(items)}):")
for c in items:
    s = c.get("snippet", {})
    cd = c.get("contentDetails", {})
    print(f"  - {s.get('title')} ({c.get('id')})")
    print(f"    uploads playlist: {cd.get('relatedPlaylists', {}).get('uploads')}")

print("\nFetching WL playlist items...")
r2 = httpx.get(
    "https://www.googleapis.com/youtube/v3/playlistItems",
    params={"part": "snippet,contentDetails", "playlistId": "WL", "maxResults": 5},
    headers={"Authorization": f"Bearer {token}"},
)
print(f"Status: {r2.status_code}")
data2 = r2.json()
items2 = data2.get("items", [])
print(f"Items: {len(items2)}")
for it in items2[:5]:
    s = it.get("snippet", {})
    print(f"  - {s.get('title', '?')[:60]} ({s.get('resourceId', {}).get('videoId', '?')})")
print(f"\nResponse: {r2.text[:500]}")
