-- YTTranscriptScraper knowledge base schema
-- All timestamps stored as ISO 8601 strings (UTC) for portability.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    channel_id TEXT,
    channel_name TEXT,
    duration INTEGER,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    transcript_tier TEXT,
    transcript_available INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_videos_channel
    ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_published
    ON videos(published_at);
CREATE INDEX IF NOT EXISTS idx_videos_channel_published
    ON videos(channel_id, published_at);

CREATE TABLE IF NOT EXISTS transcripts (
    video_id TEXT PRIMARY KEY,
    raw_text TEXT NOT NULL,
    segments_json TEXT,
    tier TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS analyses (
    video_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    output_json TEXT NOT NULL,
    markdown TEXT,
    tldr TEXT,
    stage2_output TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (video_id, prompt_version),
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_analyses_created
    ON analyses(created_at);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    start_ts REAL,
    end_ts REAL,
    text TEXT NOT NULL,
    char_start INTEGER,
    char_end INTEGER,
    embedding BLOB,
    embedding_model TEXT,
    UNIQUE (video_id, chunk_index),
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_video
    ON chunks(video_id);

CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    channel_url TEXT NOT NULL,
    channel_name TEXT,
    added_at TEXT NOT NULL,
    last_polled_at TEXT,
    last_seen_video_id TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    new_videos INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS transcript_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT,
    happened_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_failures_happened
    ON transcript_failures(happened_at DESC);
CREATE INDEX IF NOT EXISTS idx_failures_reason
    ON transcript_failures(reason);

CREATE TABLE IF NOT EXISTS watch_later_processed (
    video_id TEXT PRIMARY KEY,
    video_title TEXT,
    channel_name TEXT,
    video_url TEXT,
    added_to_watch_later_at TEXT,
    discovered_at TEXT NOT NULL,
    processed_at TEXT,
    emailed_at TEXT,
    email_message_id TEXT,
    last_error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_watch_later_processed_at
    ON watch_later_processed(processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_watch_later_emailed_at
    ON watch_later_processed(emailed_at DESC);