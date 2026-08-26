import sys
video_id = "EzQAgnjTq2k"
print("=== youtube-transcript-api: list ===")
try:
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    listing = api.list(video_id)
    print("Available transcripts:")
    for t in listing:
        print(f"  - {t.language_code} ({t.language}) gen={t.is_generated} translatable={t.is_translatable}")
except Exception as e:
    print(f"LIST FAILED: {type(e).__name__}: {e}")

print()
print("=== youtube-transcript-api: fetch attempts ===")
from youtube_transcript_api import YouTubeTranscriptApi
api = YouTubeTranscriptApi()
for label, kwargs in [
    ("en", {"languages": ["en"]}),
    ("en-US", {"languages": ["en-US"]}),
    ("any", {}),
]:
    try:
        t = api.fetch(video_id, **kwargs)
        snippets = list(t)
        print(f"  fetch({label}) OK: {len(snippets)} snippets, first: {snippets[0].text[:60]!r}")
        break
    except Exception as e:
        print(f"  fetch({label}) FAILED: {type(e).__name__}: {e}")

print()
print("=== yt-dlp subtitle listing ===")
try:
    from yt_dlp import YoutubeDL
    ydl_opts = {"quiet": True, "skip_download": True, "listsubtitles": True}
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_id, download=False)
        subs = info.get("subtitles", {})
        autosubs = info.get("automatic_captions", {})
        print(f"Title: {info.get('title')}")
        print(f"Subtitles ({len(subs)}):")
        for lang, formats in subs.items():
            print(f"  - {lang}: {[f.get('ext') for f in formats[:3]]}")
        print(f"Automatic captions ({len(autosubs)}):")
        for lang, formats in autosubs.items():
            print(f"  - {lang}: {[f.get('ext') for f in formats[:3]]}")
except Exception as e:
    print(f"YT-DLP FAILED: {type(e).__name__}: {e}")