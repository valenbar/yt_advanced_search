#!/usr/bin/env python3

"""
fetch_transcripts.py

Fetch timestamped YouTube transcripts/captions for every video
already present in an existing SQLite database.

The database is expected to be created by:
    youtube_channel_archiver.py

Requirements:
    pip install yt-dlp

Usage:

    python fetch_transcripts.py mychannel.sqlite3

    # Prefer English
    python fetch_transcripts.py mychannel.sqlite3 --language en

    # Save every available language
    python fetch_transcripts.py mychannel.sqlite3 --all-languages

    # Use YouTube cookies
    python fetch_transcripts.py mychannel.sqlite3 --cookies cookies.txt

    # Re-fetch transcripts that already exist
    python fetch_transcripts.py mychannel.sqlite3 --force

The transcripts table stores:

    video_id
    language
    source
    transcript_json
    plain_text
    fetched_at

transcript_json contains timestamped segments:

[
    {
        "start": 83.4,
        "end": 87.8,
        "text": "Hello everyone..."
    },
    ...
]
"""

import argparse
import html
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any

try:
    import yt_dlp
except ImportError:
    print(
        "This script requires yt-dlp. Install it with:\n    pip install yt-dlp",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("transcripts")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

TRANSCRIPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    video_id         TEXT NOT NULL,
    language         TEXT NOT NULL,
    source           TEXT NOT NULL,

    -- JSON array containing timestamped transcript segments.
    transcript_json  TEXT NOT NULL,

    -- Same transcript without timestamps, useful for search/LLM processing.
    plain_text       TEXT NOT NULL,

    fetched_at       TEXT NOT NULL,

    PRIMARY KEY (video_id, language, source),

    FOREIGN KEY (video_id)
        REFERENCES videos(video_id)
);

CREATE INDEX IF NOT EXISTS idx_transcripts_video_id
ON transcripts(video_id);

CREATE INDEX IF NOT EXISTS idx_transcripts_language
ON transcripts(language);
"""


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)

    conn.execute("PRAGMA journal_mode=WAL;")

    conn.executescript(TRANSCRIPT_SCHEMA)
    conn.commit()

    return conn


def transcript_exists(
    conn: sqlite3.Connection,
    video_id: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM transcripts
        WHERE video_id = ?
        LIMIT 1
        """,
        (video_id,),
    ).fetchone()

    return row is not None


def save_transcript(
    conn: sqlite3.Connection,
    video_id: str,
    language: str,
    source: str,
    segments: list[dict[str, Any]],
) -> None:

    plain_text = "\n".join(
        segment["text"] for segment in segments if segment.get("text")
    )

    transcript_json = json.dumps(
        segments,
        ensure_ascii=False,
    )

    conn.execute(
        """
        INSERT INTO transcripts (
            video_id,
            language,
            source,
            transcript_json,
            plain_text,
            fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?)

        ON CONFLICT(video_id, language, source)
        DO UPDATE SET
            transcript_json = excluded.transcript_json,
            plain_text = excluded.plain_text,
            fetched_at = excluded.fetched_at
        """,
        (
            video_id,
            language,
            source,
            transcript_json,
            plain_text,
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    conn.commit()


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------


def parse_timestamp(value: str) -> float | None:
    """
    Convert:

        00:01:23.400
        01:23.400
        83.400

    into seconds.
    """

    value = value.strip()

    # Already numeric.
    try:
        return float(value)
    except ValueError:
        pass

    parts = value.split(":")

    try:
        if len(parts) == 3:
            hours = float(parts[0])
            minutes = float(parts[1])
            seconds = float(parts[2])

            return hours * 3600 + minutes * 60 + seconds

        if len(parts) == 2:
            minutes = float(parts[0])
            seconds = float(parts[1])

            return minutes * 60 + seconds

    except ValueError:
        return None

    return None


# ---------------------------------------------------------------------------
# WebVTT parser
# ---------------------------------------------------------------------------


def parse_vtt(text: str) -> list[dict[str, Any]]:
    """
    Parse WebVTT into:

        [
            {
                "start": 83.4,
                "end": 87.8,
                "text": "Hello everyone..."
            }
        ]
    """

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    blocks = re.split(r"\n\s*\n", text)

    segments = []

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]

        if not lines:
            continue

        # Ignore WEBVTT header.
        if lines[0].startswith("WEBVTT"):
            lines = lines[1:]

        # Ignore NOTE / STYLE / REGION blocks.
        if lines and lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue

        timestamp_index = None

        for i, line in enumerate(lines):
            if "-->" in line:
                timestamp_index = i
                break

        if timestamp_index is None:
            continue

        timestamp_line = lines[timestamp_index]

        match = re.match(
            r"(.+?)\s+-->\s+(.+?)(?:\s+.*)?$",
            timestamp_line,
        )

        if not match:
            continue

        start = parse_timestamp(match.group(1))
        end = parse_timestamp(match.group(2))

        if start is None:
            continue

        if end is None:
            end = start

        text_lines = lines[timestamp_index + 1 :]

        if not text_lines:
            continue

        caption = " ".join(text_lines)

        # Remove WebVTT formatting tags.
        caption = re.sub(
            r"<[^>]+>",
            "",
            caption,
        )

        caption = html.unescape(caption)

        caption = re.sub(
            r"\s+",
            " ",
            caption,
        ).strip()

        if not caption:
            continue

        # Avoid duplicate adjacent captions.
        if (
            segments
            and segments[-1]["start"] == start
            and segments[-1]["text"] == caption
        ):
            continue

        segments.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "text": caption,
            }
        )

    return segments


# ---------------------------------------------------------------------------
# JSON3 parser
# ---------------------------------------------------------------------------


def parse_json3(text: str) -> list[dict[str, Any]]:
    """
    Parse YouTube JSON3 subtitle format.
    """

    data = json.loads(text)

    segments = []

    for event in data.get("events", []):
        if "tStartMs" not in event:
            continue

        start = float(event["tStartMs"]) / 1000

        duration = float(event.get("dDurationMs", 0)) / 1000

        end = start + duration

        pieces = []

        for seg in event.get("segs", []):
            value = seg.get("utf8")

            if value:
                pieces.append(value)

        caption = "".join(pieces).strip()

        if not caption:
            continue

        caption = html.unescape(caption)

        segments.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "text": caption,
            }
        )

    return segments


# ---------------------------------------------------------------------------
# TTML parser
# ---------------------------------------------------------------------------


def parse_ttml(text: str) -> list[dict[str, Any]]:
    """
    Basic TTML parser.

    Handles common TTML subtitle timestamps.
    """

    segments = []

    # Match <p begin="..." end="...">text</p>
    pattern = re.compile(
        r"<p\b([^>]*)>(.*?)</p>",
        re.IGNORECASE | re.DOTALL,
    )

    for match in pattern.finditer(text):
        attributes = match.group(1)
        caption = match.group(2)

        begin_match = re.search(
            r'\bbegin=["\']([^"\']+)',
            attributes,
            re.IGNORECASE,
        )

        end_match = re.search(
            r'\bend=["\']([^"\']+)',
            attributes,
            re.IGNORECASE,
        )

        if not begin_match:
            continue

        start = parse_timestamp(begin_match.group(1))

        end = parse_timestamp(end_match.group(1)) if end_match else start

        if start is None:
            continue

        caption = re.sub(
            r"<[^>]+>",
            "",
            caption,
        )

        caption = html.unescape(caption)

        caption = re.sub(
            r"\s+",
            " ",
            caption,
        ).strip()

        if not caption:
            continue

        segments.append(
            {
                "start": round(start, 3),
                "end": round(end or start, 3),
                "text": caption,
            }
        )

    return segments


# ---------------------------------------------------------------------------
# Subtitle format handling
# ---------------------------------------------------------------------------


def choose_format(
    formats: list[dict[str, Any]],
) -> dict[str, Any] | None:

    # VTT is easiest to parse while retaining timestamps.
    preferred_extensions = [
        "vtt",
        "json3",
        "ttml",
        "srv3",
    ]

    for extension in preferred_extensions:
        for fmt in formats:
            if fmt.get("ext") == extension:
                return fmt

    if formats:
        return formats[0]

    return None


def download_and_parse_subtitle(
    fmt: dict[str, Any],
    ydl: yt_dlp.YoutubeDL,
) -> list[dict[str, Any]]:

    url = fmt.get("url")

    if not url:
        raise RuntimeError("Subtitle format has no URL")

    response = ydl.urlopen(url)

    raw = response.read()

    text = raw.decode(
        "utf-8",
        errors="replace",
    )

    extension = (fmt.get("ext") or "").lower()

    if extension == "vtt":
        return parse_vtt(text)

    if extension == "json3":
        return parse_json3(text)

    if extension == "ttml":
        return parse_ttml(text)

    # Some YouTube subtitle formats are XML-like.
    if extension == "srv3":
        return parse_ttml(text)

    # Try VTT as a fallback.
    return parse_vtt(text)


# ---------------------------------------------------------------------------
# Video metadata
# ---------------------------------------------------------------------------


def fetch_video_info(
    video_id: str,
    cookies: str | None,
) -> dict[str, Any]:

    url = "https://www.youtube.com/watch?v=" + video_id

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": False,
    }

    if cookies:
        ydl_opts["cookiefile"] = cookies

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(
            url,
            download=False,
        )


# ---------------------------------------------------------------------------
# Language selection
# ---------------------------------------------------------------------------


def language_matches(
    language: str,
    preferred: str | None,
) -> bool:

    if not preferred:
        return True

    return (
        language == preferred
        or language.startswith(preferred + "-")
        or language.startswith(preferred + "_")
    )


def find_subtitles(
    info: dict[str, Any],
    preferred_language: str | None,
    all_languages: bool,
) -> list[dict[str, Any]]:

    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}

    candidates = []

    # ---------------------------------------------------------------
    # Manual captions first.
    # ---------------------------------------------------------------

    for language, formats in manual.items():
        if not language_matches(
            language,
            preferred_language,
        ):
            continue

        candidates.append(
            {
                "language": language,
                "source": "manual",
                "formats": formats,
            }
        )

    # ---------------------------------------------------------------
    # Automatic captions second.
    # ---------------------------------------------------------------

    for language, formats in automatic.items():
        if not language_matches(
            language,
            preferred_language,
        ):
            continue

        candidates.append(
            {
                "language": language,
                "source": "automatic",
                "formats": formats,
            }
        )

    if all_languages:
        return candidates

    # Without --all-languages:
    #
    # 1. Prefer manual subtitles.
    # 2. Otherwise use automatic subtitles.
    #
    # If a language was specified, this will naturally select
    # that language.

    manual_candidates = [c for c in candidates if c["source"] == "manual"]

    if manual_candidates:
        return manual_candidates[:1]

    automatic_candidates = [c for c in candidates if c["source"] == "automatic"]

    return automatic_candidates[:1]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def fetch_transcripts(
    db_path: str,
    cookies: str | None,
    sleep_seconds: float,
    preferred_language: str | None,
    all_languages: bool,
    force: bool,
) -> None:

    conn = open_db(db_path)

    videos = conn.execute(
        """
        SELECT
            video_id,
            title,
            webpage_url
        FROM videos
        ORDER BY upload_date
        """
    ).fetchall()

    log.info(
        "Found %d video(s) in database",
        len(videos),
    )

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    if cookies:
        ydl_opts["cookiefile"] = cookies

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for index, (
            video_id,
            title,
            webpage_url,
        ) in enumerate(videos, 1):
            log.info(
                "[%d/%d] %s",
                index,
                len(videos),
                title or video_id,
            )

            if not force and transcript_exists(
                conn,
                video_id,
            ):
                log.info("  Transcript already exists; skipping")
                continue

            try:
                info = fetch_video_info(
                    video_id,
                    cookies,
                )

                if not info:
                    log.warning("  No video information returned")
                    continue

                candidates = find_subtitles(
                    info,
                    preferred_language,
                    all_languages,
                )

                if not candidates:
                    log.info("  No subtitles/captions available")
                    continue

                for candidate in candidates:
                    language = candidate["language"]

                    source = candidate["source"]

                    formats = candidate["formats"]

                    fmt = choose_format(formats)

                    if not fmt:
                        log.warning(
                            "  No usable subtitle format for %s (%s)",
                            language,
                            source,
                        )
                        continue

                    log.info(
                        "  Fetching %s %s transcript",
                        language,
                        source,
                    )

                    segments = download_and_parse_subtitle(
                        fmt,
                        ydl,
                    )

                    if not segments:
                        log.warning("  Transcript was empty")
                        continue

                    save_transcript(
                        conn,
                        video_id,
                        language,
                        source,
                        segments,
                    )

                    total_characters = sum(len(segment["text"]) for segment in segments)

                    log.info(
                        "  Saved %d segments / %d characters",
                        len(segments),
                        total_characters,
                    )

                    if not all_languages:
                        break

            except Exception as exc:
                log.error(
                    "  Failed: %s",
                    exc,
                )

            if sleep_seconds and index < len(videos):
                time.sleep(sleep_seconds)

    conn.close()

    log.info("Transcript fetching complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv=None):

    parser = argparse.ArgumentParser(
        description=(
            "Fetch timestamped YouTube transcripts "
            "for videos in an existing SQLite database."
        )
    )

    parser.add_argument(
        "database",
        help=("SQLite database created by youtube_channel_archiver.py"),
    )

    parser.add_argument(
        "--cookies",
        default=None,
        help="Path to cookies.txt",
    )

    parser.add_argument(
        "--language",
        default=None,
        help=(
            "Preferred language, e.g. en, de, fr. "
            "Without this option the first available "
            "language is used."
        ),
    )

    parser.add_argument(
        "--all-languages",
        action="store_true",
        help=("Save every available subtitle language."),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=("Re-fetch transcripts even if one already exists."),
    )

    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help=("Seconds between videos (default: 1.0)"),
    )

    return parser.parse_args(argv)


def main():

    args = parse_args()

    fetch_transcripts(
        db_path=args.database,
        cookies=args.cookies,
        sleep_seconds=args.sleep,
        preferred_language=args.language,
        all_languages=args.all_languages,
        force=args.force,
    )


if __name__ == "__main__":
    main()
