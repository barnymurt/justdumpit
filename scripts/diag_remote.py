from youtube_transcript_api import YouTubeTranscriptApi
import sys

video_id = "EzQAgnjTq2k"
print(f"Listing for {video_id}:")
try:
    api = YouTubeTranscriptApi()
    for t in api.list(video_id):
        print(f"  - {t.language_code} ({t.language}) gen={t.is_generated}")
except Exception as e:
    print(f"LIST FAILED: {type(e).__name__}: {e}")

print(f"Fetch en:")
try:
    t = api.fetch(video_id, languages=["en"])
    snippets = list(t)
    print(f"  OK: {len(snippets)} snippets, first: {snippets[0].text[:60]!r}")
except Exception as e:
    print(f"  FAILED: {type(e).__name__}: {e}")