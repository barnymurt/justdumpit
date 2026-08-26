import re
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class Video:
    id: str
    title: str
    duration: str
    upload_date: str
    url: str


@dataclass
class Channel:
    name: str
    url: str
    videos: list[Video]


def parse_youtube_url(url: str) -> str:
    url = url.strip()
    
    patterns = [
        r'youtube\.com/@(\w+)',
        r'youtube\.com/channel/([\w-]+)',
        r'youtube\.com/c/(\w+)',
        r'youtube\.com/user/(\w+)',
        r'youtube\.com/(\w+)',
        r'youtu\.be/([\w-]+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return url
    
    if not url.startswith(('http://', 'https://')):
        return f"https://www.youtube.com/{url}"
    
    return url


def get_channel_videos(channel_url: str, verbose: bool = False) -> Channel:
    channel_url = parse_youtube_url(channel_url)
    
    if verbose:
        print(f"Fetching videos from: {channel_url}")
    
    cmd = [
        "python", "-m", "yt_dlp",
        "--flat-playlist",
        "--playlist-end", "50",
        "--print", "%(channel)s|%(title)s|%(duration)s|%(upload_date)s|%(id)s",
        channel_url
    ]
    
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True
    )
    
    channel_name = None
    videos = []
    
    for line in result.stdout.strip().split('\n'):
        if not line:
            continue
        
        parts = line.split('|')
        if len(parts) >= 5:
            if not channel_name and parts[0] and parts[0] != 'NA':
                channel_name = parts[0]
            
            if parts[4]:
                video = Video(
                    id=parts[4],
                    title=parts[1],
                    duration=parts[2] or "N/A",
                    upload_date=parts[3] or "N/A",
                    url=f"https://www.youtube.com/watch?v={parts[4]}"
                )
                videos.append(video)
    
    if not channel_name or channel_name == "NA":
        import re
        match = re.search(r'@(\w+)', channel_url)
        if match:
            channel_name = match.group(1)
        else:
            channel_name = "Unknown Channel"
    
    return Channel(name=channel_name, url=channel_url, videos=videos)


def print_video_list(channel: Channel) -> None:
    print(f"\n{'='*60}")
    print(f"Channel: {channel.name}")
    print(f"Total Videos: {len(channel.videos)}")
    print(f"{'='*60}\n")
    
    for i, video in enumerate(channel.videos, 1):
        print(f"{i:3}. {video.title[:50]}")
        print(f"    Duration: {video.duration} | Date: {video.upload_date}")
        print(f"    URL: {video.url}")
        print()
