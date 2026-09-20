#!/usr/bin/env python3
"""
find_view_spikes.py

Find videos that spiked in popularity relative to the channel's *own*
audience size at the time — not relative to the channel's all-time view
counts. It does this by comparing each video's view count to a local
baseline built from the videos uploaded just before and after it
(chronologically), rather than to the channel average overall. That way a
video from the channel's early, small-audience days that clearly punched
above its weight will surface, even though its raw view count is nowhere
near the channel's more recent, big-audience videos.

Requires the SQLite database produced by youtube_channel_archiver.py.

Usage:
    python find_view_spikes.py channel.sqlite3
    python find_view_spikes.py channel.sqlite3 --window 8 --threshold 75
    python find_view_spikes.py channel.sqlite3 --type video          # long-form only
    python find_view_spikes.py channel.sqlite3 --type short
    python find_view_spikes.py channel.sqlite3 --csv spikes.csv
    python find_view_spikes.py channel.sqlite3 --top 50

How it works:
    1. Videos are sorted chronologically (by upload date).
    2. Because shorts / regular videos / livestream VODs tend to attract
       very different view counts even on the same channel, each of those
       is analyzed as its own separate timeline by default (see --type and
       --no-split-types).
    3. For each video, a local baseline is the median view count of the
       `--window` nearest videos before it and the `--window` nearest
       videos after it in that same timeline (median is used instead of
       mean so that a neighboring spike doesn't skew the baseline).
    4. spike_percent = (views - baseline) / baseline * 100
       A video is reported as a spike if spike_percent >= --threshold.
    5. Results are sorted by spike_percent, largest first.

Caveats:
    - Videos published very recently may not have accumulated their
      "true" view count yet, which can create false spikes at the very
      end of the timeline. Use --min-age-days to exclude recent uploads.
    - Videos at the very start/end of a timeline have fewer neighbors to
      compare against; --min-neighbors controls how many are required
      before a video is scored at all.
"""

import argparse
import csv as csv_module
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from typing import List, Optional


def parse_upload_date(row) -> Optional[datetime]:
    """Prefer the unix timestamp; fall back to parsing YYYYMMDD."""
    ts = row["upload_timestamp"]
    if ts:
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            pass
    d = row["upload_date"]
    if d and len(d) == 8 and d.isdigit():
        try:
            return datetime.strptime(d, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def load_rows(conn: sqlite3.Connection, video_type: Optional[str]) -> List[dict]:
    conn.row_factory = sqlite3.Row
    if video_type:
        cur = conn.execute(
            "SELECT * FROM videos WHERE video_type = ? AND view_count IS NOT NULL",
            (video_type,),
        )
    else:
        cur = conn.execute("SELECT * FROM videos WHERE view_count IS NOT NULL")

    rows = []
    for r in cur.fetchall():
        dt = parse_upload_date(r)
        if dt is None:
            continue  # can't place it on the timeline, skip
        rows.append(
            {
                "video_id": r["video_id"],
                "title": r["title"],
                "webpage_url": r["webpage_url"],
                "view_count": r["view_count"],
                "video_type": r["video_type"],
                "upload_dt": dt,
            }
        )
    rows.sort(key=lambda x: x["upload_dt"])
    return rows


def find_spikes(
    rows: List[dict],
    window: int,
    threshold: float,
    min_neighbors: int,
    min_views: int,
    min_age_days: Optional[int],
) -> List[dict]:
    now = datetime.now(timezone.utc)
    n = len(rows)
    results = []

    for i, row in enumerate(rows):
        if row["view_count"] is None or row["view_count"] < min_views:
            continue
        if min_age_days is not None:
            age_days = (now - row["upload_dt"]).days
            if age_days < min_age_days:
                continue

        before = rows[max(0, i - window) : i]
        after = rows[i + 1 : i + 1 + window]
        neighbors = before + after
        neighbor_views = [
            v["view_count"] for v in neighbors if v["view_count"] is not None
        ]

        if len(neighbor_views) < min_neighbors:
            continue

        baseline = statistics.median(neighbor_views)
        if baseline <= 0:
            continue

        spike_percent = (row["view_count"] - baseline) / baseline * 100.0
        if spike_percent >= threshold:
            results.append(
                {
                    **row,
                    "baseline": baseline,
                    "neighbors_used": len(neighbor_views),
                    "spike_percent": spike_percent,
                    "before": before,
                    "after": after,
                }
            )

    results.sort(key=lambda x: x["spike_percent"], reverse=True)
    return results


def print_results(results: List[dict], top: Optional[int], show_context: bool) -> None:
    if not results:
        print("No spikes found with the current settings.")
        return

    shown = results[:top] if top else results
    print(
        f"\nFound {len(results)} spike(s){f', showing top {len(shown)}' if top and top < len(results) else ''}:\n"
    )

    for r in shown:
        date_str = r["upload_dt"].strftime("%Y-%m-%d")
        print(
            f"+{r['spike_percent']:6.0f}%  [{date_str}] [{r['video_type'] or '?'}]  "
            f"{r['view_count']:>10,} views  (baseline ~{r['baseline']:,.0f}, "
            f"{r['neighbors_used']} neighbors)"
        )
        print(f"          {r['title']}")
        if r["webpage_url"]:
            print(f"          {r['webpage_url']}")

        if show_context:
            print()
            for n in r["before"]:
                views = n["view_count"] if n["view_count"] is not None else 0
                print(f"    {views:>12,} views  {n['title']}")
            print(f"  > {r['view_count']:>12,} views  {r['title']}")
            for n in r["after"]:
                views = n["view_count"] if n["view_count"] is not None else 0
                print(f"    {views:>12,} views  {n['title']}")

        print()


def write_csv(results: List[dict], path: str) -> None:
    fieldnames = [
        "video_id",
        "title",
        "video_type",
        "upload_date",
        "view_count",
        "baseline",
        "spike_percent",
        "neighbors_used",
        "webpage_url",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv_module.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(
                {
                    "video_id": r["video_id"],
                    "title": r["title"],
                    "video_type": r["video_type"],
                    "upload_date": r["upload_dt"].strftime("%Y-%m-%d"),
                    "view_count": r["view_count"],
                    "baseline": round(r["baseline"], 1),
                    "spike_percent": round(r["spike_percent"], 1),
                    "neighbors_used": r["neighbors_used"],
                    "webpage_url": r["webpage_url"],
                }
            )
    print(f"Wrote {len(results)} row(s) to {path}")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find videos that spiked in views relative to nearby uploads on the same channel timeline.",
    )
    parser.add_argument(
        "db", help="Path to the SQLite database created by youtube_channel_archiver.py"
    )
    parser.add_argument(
        "--window",
        type=int,
        default=6,
        help="How many videos before/after (chronologically) to use as the local baseline (default: 6)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=100.0,
        help="Minimum %% above the local baseline to count as a spike, e.g. 100 = double the "
        "baseline (default: 100)",
    )
    parser.add_argument(
        "--min-neighbors",
        type=int,
        default=3,
        help="Minimum number of neighboring videos required to compute a baseline (default: 3)",
    )
    parser.add_argument(
        "--min-views",
        type=int,
        default=0,
        help="Ignore videos with fewer than this many views outright, to filter out noise "
        "from tiny view counts (default: 0)",
    )
    parser.add_argument(
        "--min-age-days",
        type=int,
        default=None,
        help="Exclude videos uploaded more recently than this many days ago, since their view "
        "counts may still be climbing (default: no limit)",
    )
    parser.add_argument(
        "--type",
        dest="video_type",
        choices=["video", "short", "stream"],
        default=None,
        help="Only analyze one video type. By default all types present are analyzed as "
        "separate timelines (see --no-split-types).",
    )
    parser.add_argument(
        "--no-split-types",
        action="store_true",
        help="Treat all videos as a single combined timeline instead of analyzing "
        "video/short/stream separately. Not recommended if the channel has very "
        "different view patterns across types.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Only show the top N spikes (default: show all)",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="Optional path to also write results as a CSV file",
    )
    parser.add_argument(
        "--no-context",
        action="store_true",
        help="Don't print the surrounding neighbor videos under each spike, just the summary line",
    )
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    conn = sqlite3.connect(args.db)

    if args.video_type:
        types_to_run = [args.video_type]
    elif args.no_split_types:
        types_to_run = [None]
    else:
        cur = conn.execute(
            "SELECT DISTINCT video_type FROM videos WHERE video_type IS NOT NULL"
        )
        types_to_run = [row[0] for row in cur.fetchall()] or [None]

    all_results = []
    for vtype in types_to_run:
        rows = load_rows(conn, vtype)
        if not rows:
            continue
        label = vtype or "all"
        print(f"Analyzing {len(rows)} '{label}' video(s)...", file=sys.stderr)
        results = find_spikes(
            rows,
            window=args.window,
            threshold=args.threshold,
            min_neighbors=args.min_neighbors,
            min_views=args.min_views,
            min_age_days=args.min_age_days,
        )
        all_results.extend(results)

    conn.close()
    all_results.sort(key=lambda x: x["spike_percent"], reverse=True)

    print_results(all_results, args.top, show_context=not args.no_context)
    if args.csv:
        write_csv(all_results[: args.top] if args.top else all_results, args.csv)


if __name__ == "__main__":
    main()
