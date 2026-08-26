from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import feedparser


CHANNEL_URL_PATTERNS = [
    re.compile(r'youtube\.com/channel/([\w-]+)'),
    re.compile(r'youtube\.com/@([\w.-]+)'),
    re.compile(r'youtube\.com/c/([\w.-]+)'),
    re.compile(r'youtube\.com/user/([\w.-]+)'),
]


@dataclass
class FeedEntry:
    video_id: str
    title: str
    url: str
    published_at: str
    channel_id: str
    channel_name: Optional[str] = None


@dataclass
class FeedResult:
    channel_id: str
    channel_name: Optional[str]
    entries: list[FeedEntry]
    feed_url: str
    error: Optional[str] = None


def extract_channel_id_from_url(url: str) -> Optional[str]:
    for pattern in CHANNEL_URL_PATTERNS:
        match = pattern.search(url)
        if match:
            matched = match.group(1)
            if matched.startswith('UC') and len(matched) >= 20:
                return matched
            return None
    match = re.search(r'(UC[\w-]{20,})', url)
    if match:
        return match.group(1)
    return None


def _fetch_channel_name(channel_id: str) -> Optional[str]:
    try:
        from yt_dlp import YoutubeDL
        with YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
            info = ydl.extract_info(channel_id, download=False)
        return info.get('channel') or info.get('uploader')
    except Exception:
        return None


def resolve_channel_id(url_or_id: str, verbose: bool = False) -> tuple[str, str, str]:
    """Returns (channel_id, canonical_url, channel_name).

    Raises ValueError if the input doesn't look like a channel (e.g. a video URL).
    """
    text = url_or_id.strip()

    if 'youtube.com/watch' in text or 'youtu.be/' in text:
        raise ValueError(
            f"That looks like a video URL, not a channel. "
            f"Use: https://www.youtube.com/@handle or https://www.youtube.com/channel/UC..."
        )

    if text.startswith('UC') and len(text) >= 20 and re.match(r'^UC[\w-]+$', text):
        name = _fetch_channel_name(text) or 'Unknown'
        return text, f"https://www.youtube.com/channel/{text}", name

    canonical = text if text.startswith('http') else f"https://www.youtube.com/{text}"

    direct = extract_channel_id_from_url(canonical)
    if direct:
        name = _fetch_channel_name(direct) or 'Unknown'
        return direct, canonical, name

    try:
        from yt_dlp import YoutubeDL
        with YoutubeDL({'quiet': True, 'skip_download': True, 'extract_flat': True}) as ydl:
            info = ydl.extract_info(canonical, download=False)
        cid = info.get('channel_id') or info.get('uploader_id')
        cname = info.get('channel') or info.get('uploader') or 'Unknown'
        if cid and cid.startswith('UC'):
            return cid, canonical, cname
    except Exception as e:
        if verbose:
            print(f"yt-dlp resolve failed: {e}")

    raise ValueError(
        f"Could not resolve a YouTube channel from: {url_or_id}. "
        f"Try: https://www.youtube.com/@handle"
    )


def _iso_from_published(published: str) -> str:
    if not published:
        return ""
    try:
        dt = datetime.fromisoformat(published.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        return published


def fetch_channel_feed(channel_id: str, verbose: bool = False) -> FeedResult:
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

    if verbose:
        print(f"Fetching RSS: {feed_url}")

    try:
        parsed = feedparser.parse(feed_url)
    except Exception as e:
        return FeedResult(
            channel_id=channel_id,
            channel_name=None,
            entries=[],
            feed_url=feed_url,
            error=f"feedparser error: {e}",
        )

    if parsed.bozo and not parsed.entries:
        return FeedResult(
            channel_id=channel_id,
            channel_name=None,
            entries=[],
            feed_url=feed_url,
            error=f"feed error: {getattr(parsed, 'bozo_exception', 'unknown')}",
        )

    channel_name = None
    if hasattr(parsed.feed, 'title'):
        channel_name = parsed.feed.title

    entries: list[FeedEntry] = []
    for entry in parsed.entries:
        video_id = entry.get('yt_videoid') or entry.get('id', '').split(':')[-1]
        if not video_id or len(video_id) != 11:
            continue
        title = entry.get('title', '')
        link = entry.get('link', '')
        if not link:
            link = f"https://www.youtube.com/watch?v={video_id}"
        published = _iso_from_published(entry.get('published', ''))
        entry_channel_id = entry.get('yt_channelid', channel_id)

        entries.append(FeedEntry(
            video_id=video_id,
            title=title,
            url=link,
            published_at=published,
            channel_id=entry_channel_id,
            channel_name=channel_name,
        ))

    return FeedResult(
        channel_id=channel_id,
        channel_name=channel_name,
        entries=entries,
        feed_url=feed_url,
    )


def new_videos_since(channel_id: str, last_seen_video_id: Optional[str], verbose: bool = False) -> tuple[list[FeedEntry], Optional[str]]:
    """Returns (new_entries_newest_first, latest_video_id). Stops at last_seen_video_id."""
    result = fetch_channel_feed(channel_id, verbose=verbose)
    if result.error:
        return [], None

    if not result.entries:
        return [], None

    latest_id = result.entries[0].video_id

    if not last_seen_video_id:
        return result.entries[:15], latest_id

    new_entries: list[FeedEntry] = []
    for entry in result.entries:
        if entry.video_id == last_seen_video_id:
            break
        new_entries.append(entry)

    return new_entries, latest_id