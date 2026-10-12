"""Compare cached scores (after sync_cache) against a PushPress CSV export.

Implements spec section 12: counts within ~2%, a 10-row spot check, and a full
list of mismatches (missing / extra / different score strings).

  uv run python scripts/validate_csv.py --csv ~/Downloads/cfd_score_export_pushpress_20260401.csv

Writes the report to state/validation_report.md (gitignored) and prints it.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from pushpress_mcp import DB_PATH, STATE_DIR
from pushpress_mcp.csv_format import ExportRow, read_pushpress_export
from pushpress_mcp.models import Score
from pushpress_mcp.store import Store

SIMILAR = 0.6  # description similarity needed to call two rows "the same workout"


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def title_of(row: ExportRow | Score) -> str:
    # The export's "Workout Title" is PushPress's workoutTitle, which we call `section`.
    return row.section if isinstance(row, Score) else row.title


def text_of(row: ExportRow | Score) -> str:
    return norm(f"{title_of(row)} {row.description}")


def similarity(a: ExportRow | Score, b: ExportRow | Score) -> float:
    return SequenceMatcher(None, text_of(a), text_of(b), autojunk=False).ratio()


@dataclass
class Result:
    matched: list[tuple[ExportRow, Score]]
    different: list[tuple[ExportRow, Score]]
    missing: list[ExportRow]  # in CSV, not in cache
    extra: list[Score]  # in cache, not in CSV


def match(csv_rows: list[ExportRow], cache_rows: list[Score]) -> Result:
    by_date_csv: dict[str, list[ExportRow]] = defaultdict(list)
    by_date_cache: dict[str, list[Score]] = defaultdict(list)
    for r in csv_rows:
        by_date_csv[r.date].append(r)
    for s in cache_rows:
        by_date_cache[s.date].append(s)

    res = Result([], [], [], [])
    for date in sorted(set(by_date_csv) | set(by_date_cache)):
        left, right = list(by_date_csv[date]), list(by_date_cache[date])
        # Pass 1: same score text, best-matching description first.
        for r in list(left):
            cands = [s for s in right if s.score.strip() == r.score.strip()]
            if cands:
                best = max(cands, key=lambda s: similarity(r, s))
                res.matched.append((r, best))
                left.remove(r)
                right.remove(best)
        # Pass 2: same workout (similar text), different score.
        for r in list(left):
            if not right:
                break
            best = max(right, key=lambda s: similarity(r, s))
            if similarity(r, best) >= SIMILAR:
                res.different.append((r, best))
                left.remove(r)
                right.remove(best)
        res.missing.extend(left)
        res.extra.extend(right)
    return res


def short(text: str, n: int = 70) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return (t[: n - 1] + "…" if len(t) > n else t).replace("|", "\\|")


def spot_check(csv_rows: list[ExportRow], cache_rows: list[Score], n: int, seed: int) -> list[str]:
    lines = ["| Date | CSV title | CSV score | Cache title | Cache score | date | title | score |", "|---|---|---|---|---|---|---|---|"]
    by_date: dict[str, list[Score]] = defaultdict(list)
    for s in cache_rows:
        by_date[s.date].append(s)
    for r in random.Random(seed).sample(csv_rows, min(n, len(csv_rows))):
        cands = by_date.get(r.date, [])
        if not cands:
            lines.append(f"| {r.date} | {short(r.title, 30)} | {short(r.score, 20)} | - | - | ✗ | ✗ | ✗ |")
            continue
        exact = [s for s in cands if s.score.strip() == r.score.strip()]
        best = max(exact or cands, key=lambda s: similarity(r, s))
        ok = lambda b: "✓" if b else "✗"  # noqa: E731
        lines.append(
            f"| {r.date} | {short(r.title, 30)} | {short(r.score, 20)} | {short(best.section, 30)} | {short(best.score, 20)} "
            f"| ✓ | {ok(norm(r.title) == norm(best.section))} | {ok(r.score.strip() == best.score.strip())} |"
        )
    return lines


def build_report(csv_path: Path, csv_rows: list[ExportRow], cache_all: list[Score], sample: int, seed: int) -> str:
    csv_start, end = min(r.date for r in csv_rows), max(r.date for r in csv_rows)
    cache_start = min((s.date for s in cache_all), default=end)
    # Only compare the period both sides cover; older CSV rows are reported, not matched.
    start = max(csv_start, cache_start)
    older = [r for r in csv_rows if r.date < start]
    csv_rows = [r for r in csv_rows if r.date >= start]
    cache = [s for s in cache_all if start <= s.date <= end]
    newer = [s for s in cache_all if s.date > end]
    res = match(csv_rows, cache)
    # The export repeats some rows verbatim (one logged score -> several rows).
    # A missing row that's an exact copy of a matched row is one of those.
    key = lambda r: (r.date, r.title, r.description, r.score, r.division)  # noqa: E731
    matched_keys = {key(r) for r, _ in res.matched} | {key(r) for r, _ in res.different}
    dup_missing = [r for r in res.missing if key(r) in matched_keys]
    real_missing = [r for r in res.missing if key(r) not in matched_keys]
    unique_csv = len({key(r) for r in csv_rows})
    dupes = len(csv_rows) - unique_csv
    diff_pct = abs(len(cache) - unique_csv) / max(unique_csv, 1) * 100

    out = [
        "# Score validation: cache vs PushPress CSV export",
        "",
        f"- CSV: `{csv_path.name}`, {len(csv_rows) + len(older)} rows, {csv_start} to {end}",
        f"- Cache: {len(cache_all)} scores total, earliest {cache_start}; {len(newer)} newer than the CSV",
        f"- Compared period: {start} to {end}: {len(csv_rows)} CSV rows vs {len(cache)} cached scores. "
        f"{len(older)} CSV rows before {start} are not compared (the cache has nothing that old).",
        f"- CSV rows that exactly repeat another row: {dupes}, leaving {unique_csv} distinct CSV scores",
        f"- Count difference (cache vs distinct CSV scores): {len(cache) - unique_csv:+d} ({diff_pct:.1f}%). "
        f"{'PASS' if diff_pct <= 2 else 'FAIL'} (target ≤ 2%). Raw difference incl. repeats: {len(cache) - len(csv_rows):+d}",
        f"- Matched exactly: {len(res.matched)}; different score: {len(res.different)}; "
        f"missing from cache: {len(real_missing)} (+{len(dup_missing)} repeated CSV rows); extra in cache: {len(res.extra)}",
        "",
        f"## Spot check ({sample} random CSV rows, seed {seed})",
        "",
        *spot_check(csv_rows, cache, sample, seed),
        "",
        "## Different score strings (same date, same workout)",
        "",
        "Fill in *Looks right* after checking the app. The export is known to be inaccurate.",
        "",
        "| Date | Workout | CSV score | Cache score | Looks right |",
        "|---|---|---|---|---|",
        *(f"| {r.date} | {short(r.title or r.description, 50)} | {short(r.score, 25)} | {short(s.score, 25)} | |" for r, s in res.different),
        "",
        "## Missing from cache (in CSV only)",
        "",
        "| Date | Workout | CSV score | Division |",
        "|---|---|---|---|",
        *(f"| {r.date} | {short(r.title or r.description, 60)} | {short(r.score, 25)} | {r.division} |" for r in real_missing),
        "",
        "## Repeated CSV rows (exact copies of a row that did match)",
        "",
        "| Date | Workout | CSV score | Extra copies |",
        "|---|---|---|---|",
        *(f"| {k[0]} | {short(k[1] or k[2], 60)} | {short(k[3], 25)} | {n} |"
          for k, n in sorted(Counter(key(r) for r in dup_missing).items())),
        "",
        "## Extra in cache (not in CSV)",
        "",
        "| Date | Workout | Cache score | Division | workout_id |",
        "|---|---|---|---|---|",
        *(f"| {s.date} | {short(s.section or s.title or s.description, 60)} | {short(s.score, 25)} | {s.division} | {s.workout_id} |" for s in res.extra),
        "",
    ]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--sample", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260401)
    parser.add_argument("--report", type=Path, default=STATE_DIR / "validation_report.md")
    args = parser.parse_args()

    csv_rows = read_pushpress_export(args.csv.expanduser())
    if not csv_rows:
        sys.exit(f"No rows parsed from {args.csv}")
    if not args.db.exists():
        sys.exit(f"No cache at {args.db}. Run sync first: uv run python -m pushpress_mcp.tools sync --days-back 3650")
    store = Store(args.db)
    try:
        cache = store.get_scores()
    finally:
        store.close()

    report = build_report(args.csv, csv_rows, cache, args.sample, args.seed)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report)
    os.chmod(args.report, 0o600)
    print(report)
    print(f"\nReport saved to {args.report}", file=sys.stderr)


if __name__ == "__main__":
    main()
