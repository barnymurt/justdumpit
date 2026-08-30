"""Replace 3-scope list with 1-scope list."""
import sys
from pathlib import Path

p = Path(sys.argv[1])
content = p.read_text(encoding="utf-8")
old = (
    'YOUTUBE_SCOPES = [\n'
    '    "openid",\n'
    '    "email",\n'
    '    "https://www.googleapis.com/auth/youtube",\n'
    ']'
)
new = (
    'YOUTUBE_SCOPES = [\n'
    '    "https://www.googleapis.com/auth/youtube",\n'
    ']'
)
if old not in content:
    print("OLD NOT FOUND")
    sys.exit(1)
content = content.replace(old, new, 1)
p.write_text(content, encoding="utf-8")
print("DONE")
