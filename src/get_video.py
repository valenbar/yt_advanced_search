#!/usr/bin/env python3
"""
get_video.py

Look up a video's full metadata from the SQLite database produced by
youtube_channel_archiver.py, and print it as JSON. Accepts, in this order
of priority:

    1. A YouTube URL (watch, youtu.be, /shorts/, /live/, with or without
       extra query params like &t=123 or ?si=...)
    2. A bare 11-character YouTube video ID
    3. Anything else is treated as a title search, and instead of one
       video you get back a ranked JSON list of candidate matches so you
       can pick the right video_id/URL and look it up directly.

Usage:
    python get_video.py channel.sqlite3 "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    python get_video.py channel.sqlite3 dQw4w9WgXcQ
    python get_video.py channel.sqlite3 Hasan Piker
    python get_video.py channel.sqlite3 "belle delphine mystery box" --limit 5
    python get_video.py channel.sqlite3 dQw4w9WgXcQ --no-raw   # omit the raw yt-dlp blob
"""

import argparse
import json
import re
import sqlite3
import sys
from typing import Optional

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

URL_ID_PATTERNS = [
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),  # watch?v=ID
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),  # youtu.be/ID
    re.compile(r"/shorts/([A-Za-z0-9_-]{11})"),  # /shorts/ID
    re.compile(r"/live/([A-Za-z0-9_-]{11})"),  # /live/ID
    re.compile(r"/embed/([A-Za-z0-9_-]{11})"),  # /embed/ID
]

JSON_COLUMNS = [
    "thumbnails",
    "categories",
    "tags",
    "subtitles",
    "automatic_captions",
    "chapters",
    "heatmap",
]


def extract_video_id(query: str) -> Optional[str]:
    """Return a video ID if `query` is a recognizable YouTube URL or a bare ID."""
    query = query.strip()

    if query.startswith("http://") or query.startswith("https://") or "youtu" in query:
        for pattern in URL_ID_PATTERNS:
            m = pattern.search(query)
            if m:
                return m.group(1)
        return None  # looked like a URL but no ID pattern matched

    if VIDEO_ID_RE.match(query):
        return query

    return None


def row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)

    raw_json = data.pop("raw_json", None)

    # Decode the *_json text columns into real JSON values, and drop the
    # "_json" suffix in the output for readability.
    for col in JSON_COLUMNS:
        key = f"{col}_json"
        if key in data:
            raw_val = data.pop(key)
            try:
                data[col] = json.loads(raw_val) if raw_val else None
            except (json.JSONDecodeError, TypeError):
                data[col] = raw_val

    if raw_json:
        try:
            data["raw_ytdlp_metadata"] = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            data["raw_ytdlp_metadata"] = None

    return data


def get_video_by_id(conn: sqlite3.Connection, video_id: str, include_raw: bool) -> dict:
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,))
    row = cur.fetchone()
    if row is None:
        return {"error": "not_found", "video_id": video_id}

    data = row_to_dict(row)
    if not include_raw:
        data.pop("raw_ytdlp_metadata", None)
    return data


def search_by_title(
    conn: sqlite3.Connection, query: str, limit: int, include_description: bool
) -> list:
    conn.row_factory = sqlite3.Row
    words = [w for w in query.strip().split() if w]
    if not words:
        return []

    if include_description:
        clause = " AND ".join("(title LIKE ? OR description LIKE ?)" for _ in words)
        params = []
        for w in words:
            like = f"%{w}%"
            params.extend([like, like])
    else:
        clause = " AND ".join("title LIKE ?" for _ in words)
        params = [f"%{w}%" for w in words]

    sql = f"""
        SELECT video_id, title, upload_date, view_count, video_type,
               webpage_url, thumbnail_url
        FROM videos
        WHERE {clause}
        ORDER BY view_count DESC
        LIMIT ?
    """
    params.append(limit)
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Look up a video's full metadata by URL/ID, or search by title, from a "
        "youtube_channel_archiver.py database. Prints JSON to stdout.",
    )
    parser.add_argument("db", help="Path to the SQLite database")
    parser.add_argument(
        "query",
        nargs="+",
        help="A video URL, a bare video ID, or search words for the title (quoting is optional)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Max number of candidates to return when falling back to title search (default: 10)",
    )
    parser.add_argument(
        "--search-description",
        action="store_true",
        help="Also match against the video description when doing a title search",
    )
    parser.add_argument(
        "--no-raw",
        action="store_true",
        help="Omit the full raw yt-dlp metadata blob from a direct video lookup (keeps output "
        "shorter; only applies when a single video is matched by URL/ID)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Print compact single-line JSON instead of pretty-printed JSON",
    )
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    query = " ".join(args.query).strip()

    conn = sqlite3.connect(args.db)

    video_id = extract_video_id(query)
    if video_id:
        result = get_video_by_id(conn, video_id, include_raw=not args.no_raw)
    else:
        result = search_by_title(conn, query, args.limit, args.search_description)
        if not result:
            result = {"error": "no_matches", "query": query}

    conn.close()

    indent = None if args.compact else 2
    print(json.dumps(result, indent=indent, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
