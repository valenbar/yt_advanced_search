#!/usr/bin/env python3

"""
Fetch timestamped YouTube transcripts using caption URLs already stored
inside the videos.raw_json column.

Sources:
    - Manual/uploaded subtitles: raw_json["subtitles"]
    - Automatic captions:        raw_json["automatic_captions"]

The script does NOT call yt-dlp to re-fetch video metadata.

It:
    1. Reads videos from the existing SQLite database.
    2. Parses raw_json.
    3. Finds manual and/or automatic caption tracks.
    4. Selects the requested language(s).
    5. Downloads the caption file directly from its stored URL.
    6. Parses timestamps.
    7. Stores timestamped transcript segments in SQLite.
    8. Adds a direct YouTube timestamp URL to every segment.

Supported caption formats:
    - WebVTT
    - SRT
    - JSON3
    - TTML
    - SRV3

Example:

    python fetch_transcripts.py archive.db --language en

Prefer manual subtitles, falling back to automatic captions:

    python fetch_transcripts.py archive.db --language en

Fetch every available language and both manual + automatic tracks:

    python fetch_transcripts.py archive.db --all-languages

Force replacement of transcripts already stored:

    python fetch_transcripts.py archive.db --language en --force
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 30

# Preferred caption formats.
#
# VTT is generally the easiest format to parse while preserving timestamps.
# JSON3 also contains explicit timing information.
FORMAT_PREFERENCE = {
    "vtt": 0,
    "srt": 1,
    "json3": 2,
    "ttml": 3,
    "srv3": 4,
    "srv2": 5,
    "srv1": 6,
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    start: float
    end: float | None
    text: str

    def to_dict(self, video_id: str) -> dict[str, Any]:
        start_seconds = max(0, int(self.start))

        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "youtube_url": (
                f"https://www.youtube.com/watch?v={video_id}&t={start_seconds}s"
            ),
        }


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

TRANSCRIPTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    video_id TEXT NOT NULL,

    language TEXT NOT NULL,

    source TEXT NOT NULL,

    transcript_json TEXT NOT NULL,

    plain_text TEXT NOT NULL,

    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(video_id, language, source)
);

CREATE INDEX IF NOT EXISTS idx_transcripts_video_id
    ON transcripts(video_id);

CREATE INDEX IF NOT EXISTS idx_transcripts_language
    ON transcripts(language);

CREATE INDEX IF NOT EXISTS idx_transcripts_source
    ON transcripts(source);
"""


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(TRANSCRIPTS_SCHEMA)

    return conn


def transcript_exists(
    conn: sqlite3.Connection,
    video_id: str,
    language: str,
    source: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM transcripts
        WHERE video_id = ?
          AND language = ?
          AND source = ?
        LIMIT 1
        """,
        (video_id, language, source),
    ).fetchone()

    return row is not None


def save_transcript(
    conn: sqlite3.Connection,
    video_id: str,
    language: str,
    source: str,
    segments: list[Segment],
) -> None:
    transcript = [segment.to_dict(video_id) for segment in segments]

    plain_text = "\n".join(segment.text for segment in segments if segment.text.strip())

    transcript_json = json.dumps(
        transcript,
        ensure_ascii=False,
    )

    conn.execute(
        """
        INSERT INTO transcripts (
            video_id,
            language,
            source,
            transcript_json,
            plain_text
        )
        VALUES (?, ?, ?, ?, ?)

        ON CONFLICT(video_id, language, source)
        DO UPDATE SET
            transcript_json = excluded.transcript_json,
            plain_text = excluded.plain_text,
            fetched_at = CURRENT_TIMESTAMP
        """,
        (
            video_id,
            language,
            source,
            transcript_json,
            plain_text,
        ),
    )

    conn.commit()


# ---------------------------------------------------------------------------
# JSON / raw metadata helpers
# ---------------------------------------------------------------------------


def load_raw_info(raw_json: str | None) -> dict[str, Any]:
    if not raw_json:
        return {}

    try:
        value = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return {}

    if not isinstance(value, dict):
        return {}

    return value


def get_caption_tracks(
    info: dict[str, Any],
    source: str,
) -> dict[str, list[dict[str, Any]]]:
    """
    Return:

        {
            "en": [
                {"url": "...", "ext": "vtt", ...},
                {"url": "...", "ext": "json3", ...},
            ],
            ...
        }

    source is either:
        "manual"
        "automatic"
    """

    if source == "manual":
        tracks = info.get("subtitles") or {}
    elif source == "automatic":
        tracks = info.get("automatic_captions") or {}
    else:
        raise ValueError(f"Unknown source: {source}")

    if not isinstance(tracks, dict):
        return {}

    result: dict[str, list[dict[str, Any]]] = {}

    for language, formats in tracks.items():
        if not isinstance(language, str):
            continue

        if not isinstance(formats, list):
            continue

        valid_formats = [
            fmt for fmt in formats if isinstance(fmt, dict) and fmt.get("url")
        ]

        if valid_formats:
            result[language] = valid_formats

    return result


# ---------------------------------------------------------------------------
# Language selection
# ---------------------------------------------------------------------------


def language_matches(
    language: str,
    requested: str,
) -> bool:
    """
    Match things such as:

        en
        en-US
        en-GB
        en-orig

    when the requested language is:

        en
    """

    language = language.lower()
    requested = requested.lower()

    if language == requested:
        return True

    return language.startswith(requested + "-") or language.startswith(requested + "_")


def choose_language(
    available: Iterable[str],
    requested: str | None,
    video_language: str | None,
) -> str | None:
    available = list(available)

    if not available:
        return None

    # Explicit language request.
    if requested:
        exact = [lang for lang in available if lang.lower() == requested.lower()]

        if exact:
            return exact[0]

        compatible = [lang for lang in available if language_matches(lang, requested)]

        if compatible:
            return compatible[0]

        return None

    # Otherwise prefer the video's declared language.
    if video_language:
        exact = [lang for lang in available if lang.lower() == video_language.lower()]

        if exact:
            return exact[0]

        compatible = [
            lang for lang in available if language_matches(lang, video_language)
        ]

        if compatible:
            return compatible[0]

    # Then prefer English if it exists.
    english = [lang for lang in available if language_matches(lang, "en")]

    if english:
        return english[0]

    # Finally use the first available language.
    return available[0]


# ---------------------------------------------------------------------------
# Caption format selection
# ---------------------------------------------------------------------------


def format_rank(fmt: dict[str, Any]) -> tuple[int, str]:
    ext = str(fmt.get("ext") or "").lower()

    return (
        FORMAT_PREFERENCE.get(ext, 100),
        ext,
    )


def choose_caption_format(
    formats: list[dict[str, Any]],
) -> dict[str, Any] | None:
    usable = [fmt for fmt in formats if isinstance(fmt, dict) and fmt.get("url")]

    if not usable:
        return None

    usable.sort(key=format_rank)

    return usable[0]


# ---------------------------------------------------------------------------
# HTTP download
# ---------------------------------------------------------------------------


def download_caption(
    caption_format: dict[str, Any],
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bytes, str]:
    url = caption_format.get("url")

    if not url:
        raise RuntimeError("Caption format has no URL")

    headers = caption_format.get("http_headers") or {}

    if not isinstance(headers, dict):
        headers = {}

    # A normal User-Agent makes direct HTTP requests more reliable.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/130.0 Safari/537.36"
        ),
        **headers,
    }

    request = urllib.request.Request(
        str(url),
        headers=headers,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            data = response.read()

            content_type = response.headers.get(
                "Content-Type",
                "",
            )

    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} downloading caption URL") from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error downloading caption URL: {exc}") from exc

    return data, content_type


# ---------------------------------------------------------------------------
# Text cleanup
# ---------------------------------------------------------------------------

HTML_TAG_RE = re.compile(r"<[^>]+>")


def clean_caption_text(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    # Remove common subtitle markup.
    text = HTML_TAG_RE.sub("", text)

    # Decode common entities through the standard library.
    import html

    text = html.unescape(text)

    # Remove zero-width characters.
    text = (
        text.replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
    )

    # Normalize whitespace while preserving intentional line breaks.
    lines = []

    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()

        if line:
            lines.append(line)

    return " ".join(lines).strip()


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


def parse_timestamp(value: str) -> float:
    """
    Parse subtitle timestamps.

    Supports:

        00:01:23.456
        01:23.456
        01:23,456
        83.456
    """

    value = value.strip().replace(",", ".")

    # Plain seconds.
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value)

    parts = value.split(":")

    if len(parts) == 3:
        hours, minutes, seconds = parts

        return float(hours) * 3600 + float(minutes) * 60 + float(seconds)

    if len(parts) == 2:
        minutes, seconds = parts

        return float(minutes) * 60 + float(seconds)

    raise ValueError(f"Unrecognized timestamp: {value!r}")


# ---------------------------------------------------------------------------
# WebVTT / SRT parser
# ---------------------------------------------------------------------------

TIMESTAMP_LINE_RE = re.compile(
    r"^\s*"
    r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})"
    r"\s*-->\s*"
    r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})"
)


def parse_vtt_or_srt(text: str) -> list[Segment]:
    segments: list[Segment] = []

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    i = 0

    while i < len(lines):
        line = lines[i].strip()

        match = TIMESTAMP_LINE_RE.match(line)

        # SRT sometimes has a numeric cue ID before the timestamp.
        if not match and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            match = TIMESTAMP_LINE_RE.match(next_line)

            if match:
                i += 1
                line = next_line

        if not match:
            i += 1
            continue

        try:
            start = parse_timestamp(match.group(1))
            end = parse_timestamp(match.group(2))
        except ValueError:
            i += 1
            continue

        i += 1

        text_lines = []

        while i < len(lines):
            current = lines[i]

            if not current.strip():
                break

            # Stop if the next cue starts immediately.
            if TIMESTAMP_LINE_RE.match(current.strip()):
                i -= 1
                break

            text_lines.append(current)
            i += 1

        text_value = clean_caption_text("\n".join(text_lines))

        if text_value:
            segments.append(
                Segment(
                    start=start,
                    end=end,
                    text=text_value,
                )
            )

        i += 1

    return segments


# ---------------------------------------------------------------------------
# JSON3 parser
# ---------------------------------------------------------------------------


def parse_json3(data: bytes) -> list[Segment]:
    payload = json.loads(data.decode("utf-8-sig", errors="replace"))

    events = payload.get("events") or []

    segments: list[Segment] = []

    for event in events:
        if not isinstance(event, dict):
            continue

        start_ms = event.get("t")

        if start_ms is None:
            continue

        try:
            start = float(start_ms) / 1000.0
        except (TypeError, ValueError):
            continue

        duration_ms = event.get("d")

        if duration_ms is not None:
            try:
                end = (float(start_ms) + float(duration_ms)) / 1000.0
            except (TypeError, ValueError):
                end = None
        else:
            end = None

        parts = []

        for seg in event.get("segs") or []:
            if not isinstance(seg, dict):
                continue

            value = seg.get("utf8")

            if value is not None:
                parts.append(str(value))

        text_value = clean_caption_text("".join(parts))

        if not text_value:
            continue

        segments.append(
            Segment(
                start=start,
                end=end,
                text=text_value,
            )
        )

    return segments


# ---------------------------------------------------------------------------
# XML subtitle parser
# ---------------------------------------------------------------------------


def local_name(tag: str) -> str:
    """
    Convert:

        {namespace}p

    into:

        p
    """

    if "}" in tag:
        return tag.rsplit("}", 1)[1]

    return tag


def element_text(element: ET.Element) -> str:
    return "".join(element.itertext())


def parse_xml_captions(
    data: bytes,
    source_format: str,
) -> list[Segment]:
    root = ET.fromstring(data)

    segments: list[Segment] = []

    for element in root.iter():
        if local_name(element.tag) != "p":
            continue

        # SRV3 typically uses:
        #
        #   t = start milliseconds
        #   d = duration milliseconds
        #
        if source_format in {"srv3", "srv2", "srv1"}:
            t = element.attrib.get("t")
            d = element.attrib.get("d")

            if t is None:
                continue

            try:
                start = float(t) / 1000.0
            except ValueError:
                continue

            if d is not None:
                try:
                    end = (float(t) + float(d)) / 1000.0
                except ValueError:
                    end = None
            else:
                end = None

        # TTML normally uses begin/end/dur.
        else:
            begin = element.attrib.get("begin")
            end_value = element.attrib.get("end")
            duration = element.attrib.get("dur")

            if begin is None:
                continue

            try:
                start = parse_ttml_time(begin)
            except ValueError:
                continue

            end = None

            if end_value:
                try:
                    end = parse_ttml_time(end_value)
                except ValueError:
                    end = None
            elif duration:
                try:
                    end = start + parse_ttml_time(duration)
                except ValueError:
                    end = None

        text_value = clean_caption_text(element_text(element))

        if not text_value:
            continue

        segments.append(
            Segment(
                start=start,
                end=end,
                text=text_value,
            )
        )

    return segments


def parse_ttml_time(value: str) -> float:
    """
    Parse common TTML time expressions.

    Examples:

        00:00:03.500
        3.5s
        3500ms
        75f
    """

    value = value.strip()

    if value.endswith("ms"):
        return float(value[:-2]) / 1000.0

    if value.endswith("s"):
        return float(value[:-1])

    # Frames. YouTube's subtitle feeds commonly use 30fps
    # when frame-based timing is encountered.
    if value.endswith("f"):
        return float(value[:-1]) / 30.0

    return parse_timestamp(value)


# ---------------------------------------------------------------------------
# Caption parsing dispatcher
# ---------------------------------------------------------------------------


def parse_caption(
    data: bytes,
    extension: str,
) -> list[Segment]:
    extension = extension.lower().lstrip(".")

    if extension in {"vtt", "webvtt"}:
        text = data.decode(
            "utf-8-sig",
            errors="replace",
        )
        return parse_vtt_or_srt(text)

    if extension == "srt":
        text = data.decode(
            "utf-8-sig",
            errors="replace",
        )
        return parse_vtt_or_srt(text)

    if extension == "json3":
        return parse_json3(data)

    if extension in {
        "ttml",
        "srv3",
        "srv2",
        "srv1",
        "xml",
    }:
        return parse_xml_captions(
            data,
            extension,
        )

    # Some stored formats may have an unexpected/missing extension.
    #
    # Try VTT first because that is the most common YouTube subtitle
    # representation.
    try:
        text = data.decode(
            "utf-8-sig",
            errors="replace",
        )

        segments = parse_vtt_or_srt(text)

        if segments:
            return segments
    except Exception:
        pass

    # Then try JSON3.
    try:
        return parse_json3(data)
    except Exception:
        pass

    raise RuntimeError(f"Unsupported or unrecognized caption format: {extension}")


# ---------------------------------------------------------------------------
# Transcript processing
# ---------------------------------------------------------------------------


def fetch_one_track(
    video_id: str,
    language: str,
    source: str,
    formats: list[dict[str, Any]],
    timeout: int,
) -> list[Segment]:
    caption_format = choose_caption_format(formats)

    if not caption_format:
        raise RuntimeError(f"No usable caption format for {language}")

    extension = str(caption_format.get("ext") or "vtt").lower()

    url = caption_format.get("url")

    print(f"      {source:9s} {language:12s} {extension:6s} {url[:100]}...")

    data, _content_type = download_caption(
        caption_format,
        timeout=timeout,
    )

    segments = parse_caption(
        data,
        extension,
    )

    if not segments:
        raise RuntimeError("Caption file contained no transcript segments")

    return segments


def process_video(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    language: str | None,
    all_languages: bool,
    force: bool,
    timeout: int,
) -> int:
    video_id = row["video_id"]
    title = row["title"] or video_id

    info = load_raw_info(row["raw_json"])

    if not info:
        print(f"[SKIP] {video_id}: invalid or empty raw_json")
        return 0

    video_language = info.get("language")

    manual = get_caption_tracks(
        info,
        "manual",
    )

    automatic = get_caption_tracks(
        info,
        "automatic",
    )

    if not manual and not automatic:
        print(f"[NONE] {video_id}: no caption URLs in raw_json")
        return 0

    # ------------------------------------------------------------------
    # Select tracks.
    #
    # Normal mode:
    #     Prefer manual subtitles.
    #     Fall back to automatic captions.
    #
    # --all-languages:
    #     Fetch every available manual and automatic language.
    # ------------------------------------------------------------------

    selected: list[tuple[str, str, list[dict[str, Any]]]] = []

    if all_languages:
        for lang, formats in manual.items():
            selected.append(("manual", lang, formats))

        for lang, formats in automatic.items():
            selected.append(("automatic", lang, formats))

    else:
        selected_language = choose_language(
            list(manual.keys()) + list(automatic.keys()),
            language,
            video_language,
        )

        if not selected_language:
            requested = language or "(automatic)"

            print(f"[NONE] {video_id}: language {requested!r} not available")
            return 0

        # Prefer manual for the selected language.
        manual_language = next(
            (
                lang
                for lang in manual
                if language_matches(
                    lang,
                    selected_language,
                )
            ),
            None,
        )

        if manual_language:
            selected.append(
                (
                    "manual",
                    manual_language,
                    manual[manual_language],
                )
            )

        else:
            automatic_language = next(
                (
                    lang
                    for lang in automatic
                    if language_matches(
                        lang,
                        selected_language,
                    )
                ),
                None,
            )

            if automatic_language:
                selected.append(
                    (
                        "automatic",
                        automatic_language,
                        automatic[automatic_language],
                    )
                )

    if not selected:
        print(f"[NONE] {video_id}: no matching caption track")
        return 0

    print(f"\n[{video_id}] {title}")

    saved = 0

    for source, lang, formats in selected:
        if not force and transcript_exists(
            conn,
            video_id,
            lang,
            source,
        ):
            print(f"      [EXISTS] {source}/{lang}")
            continue

        try:
            segments = fetch_one_track(
                video_id=video_id,
                language=lang,
                source=source,
                formats=formats,
                timeout=timeout,
            )

            save_transcript(
                conn=conn,
                video_id=video_id,
                language=lang,
                source=source,
                segments=segments,
            )

            print(f"      [SAVED] {len(segments):,} segments")

            saved += 1

        except Exception as exc:
            print(f"      [ERROR] {source}/{lang}: {exc}")

    return saved


# ---------------------------------------------------------------------------
# Main archive loop
# ---------------------------------------------------------------------------


def archive_transcripts(
    db_path: str,
    language: str | None,
    all_languages: bool,
    force: bool,
    timeout: int,
    delay: float,
) -> None:
    conn = open_db(db_path)

    try:
        rows = conn.execute(
            """
            SELECT
                video_id,
                title,
                raw_json
            FROM videos
            WHERE raw_json IS NOT NULL
              AND raw_json != ''
            ORDER BY rowid
            """
        ).fetchall()

        total = len(rows)

        print(f"Found {total:,} videos with raw_json.")

        total_saved = 0

        for index, row in enumerate(rows, start=1):
            video_id = row["video_id"]

            print(f"\n{'=' * 80}")
            print(f"[{index:,}/{total:,}] {video_id}")

            saved = process_video(
                conn=conn,
                row=row,
                language=language,
                all_languages=all_languages,
                force=force,
                timeout=timeout,
            )

            total_saved += saved

            if delay > 0 and index < total:
                time.sleep(delay)

        print(f"\nCompleted.")
        print(f"Transcript records saved: {total_saved:,}")

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch timestamped YouTube transcripts from "
            "caption URLs already stored in videos.raw_json."
        )
    )

    parser.add_argument(
        "database",
        help="Path to the SQLite database created by youtube_channel_archiver.py",
    )

    parser.add_argument(
        "--language",
        "-l",
        default=None,
        help=(
            "Preferred language, e.g. en, de, fr. "
            "en also matches en-US/en-GB/etc. "
            "Without this option, the video's declared language "
            "is preferred, then English, then the first available language."
        ),
    )

    parser.add_argument(
        "--all-languages",
        action="store_true",
        help=(
            "Fetch every available manual and automatic caption language "
            "instead of selecting one language."
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing transcript records.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Delay between videos in seconds.",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    if args.delay < 0:
        parser.error("--delay cannot be negative")

    try:
        archive_transcripts(
            db_path=args.database,
            language=args.language,
            all_languages=args.all_languages,
            force=args.force,
            timeout=args.timeout,
            delay=args.delay,
        )

    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130

    except sqlite3.Error as exc:
        print(
            f"Database error: {exc}",
            file=sys.stderr,
        )
        return 1

    except Exception as exc:
        print(
            f"Fatal error: {exc}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
