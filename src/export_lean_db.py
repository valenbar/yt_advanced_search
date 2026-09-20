#!/usr/bin/env python3
"""
export_lean_db.py

Create a small, web-ready copy of a youtube_channel_archiver.py database,
keeping only the columns the GitHub Pages table actually displays. The
full database's `raw_json` column (yt-dlp's complete per-video metadata —
every format/quality variant, full subtitle tracks, etc.) is usually the
single biggest thing bloating the file, often by 10-50x, and the website
never reads it. Dropping it is normally enough to get well under GitHub's
100 MB per-file push limit; gzip the result on top for a further big cut,
since it's almost all repetitive text/JSON.

Usage:
    python export_lean_db.py full.sqlite3 videos.sqlite3
    python export_lean_db.py full.sqlite3 videos.sqlite3 --gzip
    python export_lean_db.py full.sqlite3 videos.sqlite3 --keep description --gzip

By default this keeps exactly the columns index.html's COLUMNS/selectCols
use: video_id, webpage_url, thumbnail_url, title, video_type, upload_date,
duration_string, duration_seconds, view_count, like_count, comment_count.
Use --keep to add any extra columns back in (e.g. description, tags_json)
if you customize the page to show them.
"""

import argparse
import gzip
import shutil
import sqlite3
import sys
from pathlib import Path

DEFAULT_COLUMNS = [
    "video_id", "webpage_url", "thumbnail_url", "title", "video_type",
    "upload_date", "duration_string", "duration_seconds",
    "view_count", "like_count", "comment_count",
]


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def export_lean(src_path: str, dst_path: str, columns: list) -> None:
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row

    # Verify requested columns actually exist in the source table
    existing = {row[1] for row in src.execute("PRAGMA table_info(videos)")}
    missing = [c for c in columns if c not in existing]
    if missing:
        print(f"Warning: these columns aren't in the source db and will be skipped: "
              f"{', '.join(missing)}", file=sys.stderr)
        columns = [c for c in columns if c in existing]

    Path(dst_path).unlink(missing_ok=True)
    dst = sqlite3.connect(dst_path)

    col_defs = ", ".join(f'"{c}"' for c in columns)
    dst.execute(f"CREATE TABLE videos ({col_defs})")

    rows = src.execute(f"SELECT {col_defs} FROM videos").fetchall()
    placeholders = ", ".join("?" for _ in columns)
    dst.executemany(f"INSERT INTO videos ({col_defs}) VALUES ({placeholders})",
                     [tuple(row) for row in rows])
    dst.execute("CREATE INDEX IF NOT EXISTS idx_lean_type ON videos(video_type)")
    dst.commit()

    # Reclaim space from any deleted/temp pages
    dst.execute("VACUUM")
    dst.close()
    src.close()

    print(f"Exported {len(rows)} video(s) with columns: {', '.join(columns)}")


def gzip_file(path: str) -> str:
    gz_path = path + ".gz"
    with open(path, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=9) as f_out:
        shutil.copyfileobj(f_in, f_out)
    return gz_path


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a lean, web-ready copy of a channel archive database for GitHub Pages.",
    )
    parser.add_argument("source_db", help="Path to the full database from youtube_channel_archiver.py")
    parser.add_argument("output_db", help="Path to write the lean copy to (e.g. videos.sqlite3)")
    parser.add_argument(
        "--keep", nargs="+", default=[],
        help="Extra column names to keep in addition to the defaults",
    )
    parser.add_argument(
        "--gzip", action="store_true",
        help="Also write a gzip-compressed copy (output_db + '.gz') for an even smaller push",
    )
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    columns = DEFAULT_COLUMNS + [c for c in args.keep if c not in DEFAULT_COLUMNS]

    before = Path(args.source_db).stat().st_size
    export_lean(args.source_db, args.output_db, columns)
    after = Path(args.output_db).stat().st_size

    print(f"{human_size(before)} -> {human_size(after)} "
          f"({100 * (1 - after / before):.0f}% smaller)")

    if args.gzip:
        gz_path = gzip_file(args.output_db)
        gz_size = Path(gz_path).stat().st_size
        print(f"Gzipped: {gz_path} ({human_size(gz_size)}, "
              f"{100 * (1 - gz_size / after):.0f}% smaller than uncompressed lean db)")
        print(f"\nCommit '{gz_path}' to your repo and set DB_FILENAME to its name in index.html.")
    else:
        print(f"\nCommit '{args.output_db}' to your repo (or rerun with --gzip for an even smaller file).")


if __name__ == "__main__":
    main()
