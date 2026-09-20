#!/usr/bin/env python3
"""
youtube_channel_archiver.py

Fetch every video from a YouTube channel — regular uploads, shorts, and
livestream VODs — and store rich metadata for each one in a local SQLite
database.

Requirements:
    pip install yt-dlp

Usage:
    python youtube_channel_archiver.py "https://www.youtube.com/@SomeChannel"
    python youtube_channel_archiver.py "SomeChannel" --db mychannel.sqlite3
    python youtube_channel_archiver.py "@SomeChannel" --types videos streams shorts
    python youtube_channel_archiver.py "@SomeChannel" --update      # skip videos already in db
    python youtube_channel_archiver.py "@SomeChannel" --cookies cookies.txt --sleep 1.5

Notes:
    - YouTube channels split content across tabs: "Videos", "Shorts", "Live"
      (which holds both current and past livestreams / VODs). This script
      queries all three tabs (configurable via --types) and de-duplicates
      the resulting video IDs before fetching full metadata for each one.
    - Fetching full metadata requires one request per video (yt-dlp has to
      load each watch page), so large channels will take a while. Use
      --sleep to be polite to YouTube and reduce the chance of throttling,
      and --update on subsequent runs to only fetch new videos.
    - Some fields (like_count, comment_count, chapters, subtitles) may be
      unavailable/None depending on the video and what YouTube exposes.
"""

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

try:
    import yt_dlp
except ImportError:
    print(
        "This script requires yt-dlp. Install it with:\n    pip install yt-dlp",
        file=sys.stderr,
    )
    sys.exit(1)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("archiver")


SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id            TEXT PRIMARY KEY,
    channel_id          TEXT,
    channel_name        TEXT,
    channel_url         TEXT,
    uploader_id         TEXT,
    title               TEXT,
    description         TEXT,
    webpage_url         TEXT,
    thumbnail_url       TEXT,
    thumbnails_json      TEXT,
    upload_date         TEXT,     -- YYYYMMDD as given by yt-dlp
    upload_timestamp    INTEGER,  -- unix timestamp if available
    release_timestamp   INTEGER,  -- for premieres/streams
    duration_seconds    INTEGER,
    duration_string     TEXT,
    view_count          INTEGER,
    like_count          INTEGER,
    comment_count       INTEGER,
    average_rating      REAL,
    age_limit           INTEGER,
    categories_json      TEXT,
    tags_json            TEXT,
    is_live             INTEGER,  -- 1/0
    was_live            INTEGER,  -- 1/0
    live_status         TEXT,     -- is_live / was_live / not_live / upcoming / post_live
    availability        TEXT,
    video_type          TEXT,     -- video / short / stream (our own classification)
    source_tab          TEXT,     -- which channel tab this was discovered on
    resolution           TEXT,
    width                INTEGER,
    height               INTEGER,
    fps                  REAL,
    vcodec               TEXT,
    acodec               TEXT,
    format_note          TEXT,
    filesize_approx       INTEGER,
    language             TEXT,
    subtitles_json        TEXT,   -- list of available subtitle languages
    automatic_captions_json TEXT, -- list of available auto-caption languages
    chapters_json         TEXT,
    heatmap_json          TEXT,
    playable_in_embed     INTEGER,
    comment_count_disabled INTEGER,
    extractor            TEXT,
    fetched_at           TEXT,    -- ISO timestamp of when we scraped this row
    raw_json             TEXT     -- full raw yt-dlp info dict, for anything not modeled above
);

CREATE TABLE IF NOT EXISTS fetch_errors (
    video_id     TEXT,
    url          TEXT,
    error        TEXT,
    occurred_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_videos_channel_id ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_upload_date ON videos(upload_date);
CREATE INDEX IF NOT EXISTS idx_videos_video_type ON videos(video_type);
"""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def existing_video_ids(conn: sqlite3.Connection) -> Set[str]:
    cur = conn.execute("SELECT video_id FROM videos")
    return {row[0] for row in cur.fetchall()}


def best_thumbnail(info: Dict[str, Any]) -> Optional[str]:
    thumbs = info.get("thumbnails") or []
    if thumbs:
        # yt-dlp usually orders thumbnails smallest -> largest; take the last (largest)
        return thumbs[-1].get("url")
    return info.get("thumbnail")


def classify_video_type(info: Dict[str, Any], source_tab: str) -> str:
    if source_tab == "shorts":
        return "short"
    if info.get("was_live") or info.get("is_live") or source_tab == "streams":
        return "stream"
    return "video"


def row_from_info(info: Dict[str, Any], source_tab: str) -> Dict[str, Any]:
    duration = info.get("duration")
    subtitles = list((info.get("subtitles") or {}).keys())
    auto_captions = list((info.get("automatic_captions") or {}).keys())

    return {
        "video_id": info.get("id"),
        "channel_id": info.get("channel_id"),
        "channel_name": info.get("channel") or info.get("uploader"),
        "channel_url": info.get("channel_url"),
        "uploader_id": info.get("uploader_id"),
        "title": info.get("title"),
        "description": info.get("description"),
        "webpage_url": info.get("webpage_url"),
        "thumbnail_url": best_thumbnail(info),
        "thumbnails_json": json.dumps(info.get("thumbnails") or []),
        "upload_date": info.get("upload_date"),
        "upload_timestamp": info.get("timestamp"),
        "release_timestamp": info.get("release_timestamp"),
        "duration_seconds": duration,
        "duration_string": info.get("duration_string"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "average_rating": info.get("average_rating"),
        "age_limit": info.get("age_limit"),
        "categories_json": json.dumps(info.get("categories") or []),
        "tags_json": json.dumps(info.get("tags") or []),
        "is_live": int(bool(info.get("is_live"))),
        "was_live": int(bool(info.get("was_live"))),
        "live_status": info.get("live_status"),
        "availability": info.get("availability"),
        "video_type": classify_video_type(info, source_tab),
        "source_tab": source_tab,
        "resolution": info.get("resolution"),
        "width": info.get("width"),
        "height": info.get("height"),
        "fps": info.get("fps"),
        "vcodec": info.get("vcodec"),
        "acodec": info.get("acodec"),
        "format_note": info.get("format_note"),
        "filesize_approx": info.get("filesize_approx"),
        "language": info.get("language"),
        "subtitles_json": json.dumps(subtitles),
        "automatic_captions_json": json.dumps(auto_captions),
        "chapters_json": json.dumps(info.get("chapters") or []),
        "heatmap_json": json.dumps(info.get("heatmap") or []),
        "playable_in_embed": int(bool(info.get("playable_in_embed"))),
        "comment_count_disabled": int(
            bool(info.get("comment_count") == 0 and info.get("comments") == [])
        ),
        "extractor": info.get("extractor"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "raw_json": json.dumps(info, default=str),
    }


def upsert_video(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    columns = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    updates = ", ".join(f"{k}=excluded.{k}" for k in row.keys() if k != "video_id")
    sql = (
        f"INSERT INTO videos ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(video_id) DO UPDATE SET {updates}"
    )
    conn.execute(sql, list(row.values()))
    conn.commit()


def log_error(conn: sqlite3.Connection, video_id: str, url: str, error: str) -> None:
    conn.execute(
        "INSERT INTO fetch_errors (video_id, url, error, occurred_at) VALUES (?, ?, ?, ?)",
        (video_id, url, error, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------


def normalize_channel_url(channel: str) -> str:
    """Accept a full URL, an @handle, or a bare channel name and turn it into
    a channel URL yt-dlp can work with."""
    channel = channel.strip()
    if channel.startswith("http://") or channel.startswith("https://"):
        return channel.rstrip("/")
    if channel.startswith("@"):
        return f"https://www.youtube.com/{channel}"
    return f"https://www.youtube.com/@{channel}"


def tab_url(base_channel_url: str, tab: str) -> str:
    base = base_channel_url.rstrip("/")
    # strip any existing tab suffix like /videos, /streams, /shorts, /featured
    for known in ("/videos", "/streams", "/shorts", "/featured", "/about"):
        if base.endswith(known):
            base = base[: -len(known)]
            break
    return f"{base}/{tab}"


def list_video_ids_for_tab(
    base_channel_url: str, tab: str, cookies: Optional[str], max_videos: Optional[int]
) -> List[Dict[str, str]]:
    """Flat-extract the given channel tab to get video ids/urls quickly
    without downloading full metadata for each one yet."""
    url = tab_url(base_channel_url, tab)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
    }
    if cookies:
        ydl_opts["cookiefile"] = cookies
    if max_videos:
        ydl_opts["playlistend"] = max_videos

    entries: List[Dict[str, str]] = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not list tab '%s' (%s): %s", tab, url, exc)
        return entries

    if not info:
        return entries

    # Channel pages nest playlists inside "entries"; sometimes yt-dlp returns
    # a single flat playlist directly.
    raw_entries = info.get("entries") or []
    for e in raw_entries:
        if not e:
            continue
        # Some channel extractions return nested playlists (e.g. "Shorts" as
        # its own sub-playlist) — flatten one level if needed.
        if e.get("_type") == "playlist" and e.get("entries"):
            for sub in e["entries"]:
                if sub and sub.get("id"):
                    entries.append(
                        {
                            "id": sub["id"],
                            "url": sub.get("url") or sub.get("webpage_url"),
                        }
                    )
        elif e.get("id"):
            entries.append({"id": e["id"], "url": e.get("url") or e.get("webpage_url")})

    log.info("Tab '%s': found %d video(s)", tab, len(entries))
    return entries


def fetch_full_info(video_id: str, cookies: Optional[str]) -> Dict[str, Any]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": False,
    }
    if cookies:
        ydl_opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def archive_channel(
    channel: str,
    db_path: str,
    tabs: Iterable[str],
    cookies: Optional[str],
    sleep_seconds: float,
    max_videos: Optional[int],
    update_only: bool,
) -> None:
    base_url = normalize_channel_url(channel)
    log.info("Channel base URL: %s", base_url)

    conn = open_db(db_path)
    already_have = existing_video_ids(conn) if update_only else set()
    if update_only:
        log.info(
            "Update mode: %d video(s) already in database, will skip those",
            len(already_have),
        )

    # Step 1: discover video ids across all requested tabs
    discovered: Dict[
        str, str
    ] = {}  # video_id -> source_tab (first tab it was found on)
    for tab in tabs:
        for entry in list_video_ids_for_tab(base_url, tab, cookies, max_videos):
            vid = entry["id"]
            if vid not in discovered:
                discovered[vid] = tab

    log.info(
        "Discovered %d unique video(s) across tabs: %s",
        len(discovered),
        ", ".join(tabs),
    )

    to_fetch = [vid for vid in discovered if vid not in already_have]
    skipped = len(discovered) - len(to_fetch)
    if skipped:
        log.info("Skipping %d already-archived video(s)", skipped)

    # Step 2: fetch full metadata per video and store it
    total = len(to_fetch)
    for i, vid in enumerate(to_fetch, start=1):
        source_tab = discovered[vid]
        log.info("[%d/%d] Fetching %s (tab: %s)", i, total, vid, source_tab)
        try:
            info = fetch_full_info(vid, cookies)
            if info is None:
                raise RuntimeError("yt-dlp returned no data")
            row = row_from_info(info, source_tab)
            upsert_video(conn, row)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to fetch %s: %s", vid, exc)
            log_error(conn, vid, f"https://www.youtube.com/watch?v={vid}", str(exc))

        if sleep_seconds and i < total:
            time.sleep(sleep_seconds)

    conn.close()
    log.info("Done. Database written to %s", db_path)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive every video (including livestream VODs) from a YouTube channel into SQLite.",
    )
    parser.add_argument(
        "channel",
        help="Channel URL, @handle, or bare channel name, e.g. '@SomeChannel' or "
        "'https://www.youtube.com/@SomeChannel'",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the SQLite database file (default: <channel>.sqlite3)",
    )
    parser.add_argument(
        "--types",
        nargs="+",
        default=["videos", "streams", "shorts"],
        choices=["videos", "streams", "shorts"],
        help="Which channel tabs to scan (default: all three)",
    )
    parser.add_argument(
        "--cookies",
        default=None,
        help="Path to a cookies.txt file (needed for age-restricted or members-only content)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Seconds to sleep between per-video metadata requests (default: 1.0)",
    )
    parser.add_argument(
        "--max-videos",
        type=int,
        default=None,
        help="Limit how many videos to list per tab (default: no limit)",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Skip videos already present in the database instead of re-fetching them",
    )
    return parser.parse_args(argv)


def default_db_name(channel: str) -> str:
    slug = channel.strip().lstrip("@").rstrip("/")
    slug = slug.replace("https://www.youtube.com/", "").replace(
        "http://www.youtube.com/", ""
    )
    slug = "".join(c if c.isalnum() or c in "-_." else "_" for c in slug)
    return f"{slug or 'channel'}.sqlite3"


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    db_path = args.db or default_db_name(args.channel)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    archive_channel(
        channel=args.channel,
        db_path=db_path,
        tabs=args.types,
        cookies=args.cookies,
        sleep_seconds=args.sleep,
        max_videos=args.max_videos,
        update_only=args.update,
    )


if __name__ == "__main__":
    main()
