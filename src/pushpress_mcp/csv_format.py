"""The PushPress score-export CSV format: reading (with repair) and writing."""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import Score

COLUMNS = ["Workout Title", "Workout Description", "Score", "Division", "Date Time"]
# PushPress writes e.g. "4/1/2026 12:0 AM" (unpadded minutes).
_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4}) \d{1,2}:\d{1,2} [AP]M$")


@dataclass
class ExportRow:
    title: str
    description: str
    score: str
    division: str
    date: str  # YYYY-MM-DD


def format_export_date(iso_date: str) -> str:
    d = dt.date.fromisoformat(iso_date[:10])
    return f"{d.month}/{d.day}/{d.year} 12:0 AM"


def parse_export_date(value: str) -> str | None:
    m = _DATE_RE.match(value.strip())
    if not m:
        return None
    month, day, year = (int(g) for g in m.groups())
    return dt.date(year, month, day).isoformat()


# One export record. PushPress quotes fields but doesn't escape quotes inside
# them (e.g. `30" box jump`), which breaks a standard CSV parser. Every record
# ends with `","<score>","<division>",<date>` at end of line, so anchor on that.
_RECORD_RE = re.compile(
    r'^"(?P<title>[^\n]*?)","(?P<description>.*?)","(?P<score>[^"\n]*)","(?P<division>[^"\n]*)",'
    r'"?(?P<date>\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{1,2} [AP]M)"?\r?$',
    re.MULTILINE | re.DOTALL,
)


def read_pushpress_export(path: Path) -> list[ExportRow]:
    text = path.read_text(encoding="utf-8-sig")
    first_line, _, rest = text.partition("\n")
    if first_line.replace('"', "").strip().startswith(",".join(COLUMNS[:2])):
        text = rest
    return [
        ExportRow(
            title=m["title"].replace('""', '"'),
            description=m["description"].replace('""', '"').replace("\r\n", "\n"),
            score=m["score"],
            division=m["division"],
            date=parse_export_date(m["date"]) or "",
        )
        for m in _RECORD_RE.finditer(text)
    ]


def division_label(score: Score) -> str:
    if score.division:
        return score.division
    if score.rx is None:
        return ""
    return "Rx" if score.rx else "Scaled"


def write_scores_csv(path: Path, scores: Iterable[Score]) -> int:
    n = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(COLUMNS)
        for s in scores:
            # PushPress puts the part's "workoutTitle" (our `section`) in the title column.
            writer.writerow([s.section, s.description, s.score, division_label(s), format_export_date(s.date)])
            n += 1
    return n
