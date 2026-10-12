"""SQLite cache. Raw JSON is always kept so records can be re-parsed offline."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from typing import Iterable

from . import DB_PATH
from .models import LeaderboardEntry, Score, Workout

SCHEMA = """
CREATE TABLE IF NOT EXISTS workouts (
    id TEXT PRIMARY KEY,
    date TEXT,
    title TEXT,
    description TEXT,
    type TEXT,
    program TEXT,
    section TEXT,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS workouts_date ON workouts(date);

CREATE TABLE IF NOT EXISTS scores (
    id TEXT PRIMARY KEY,
    workout_id TEXT,
    date TEXT,
    score TEXT,
    score_type TEXT,
    division TEXT,
    rx INTEGER,
    title TEXT,
    description TEXT,
    section TEXT,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS scores_date ON scores(date);

-- Other members' data: local cache only, never exported.
CREATE TABLE IF NOT EXISTS leaderboard_entries (
    workout_id TEXT,
    rank INTEGER,
    name TEXT,
    score TEXT,
    division TEXT,
    is_me INTEGER,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS leaderboard_workout ON leaderboard_entries(workout_id);

-- Days whose programming has been fetched, so long syncs can resume.
CREATE TABLE IF NOT EXISTS synced_days (
    date TEXT PRIMARY KEY,
    fetched_at TEXT
);
"""

# Scores arrive from two sources (score history and the workout-of-day's
# attached score) with different fields filled in. Merge instead of replace:
# a non-empty incoming value wins, an empty one keeps what's there.
_SCORE_COLUMNS = ["workout_id", "date", "score", "score_type", "division", "title", "description", "section"]
_UPSERT_SCORE = f"""
INSERT INTO scores (id, {", ".join(_SCORE_COLUMNS)}, rx, raw_json)
VALUES ({", ".join("?" * (len(_SCORE_COLUMNS) + 3))})
ON CONFLICT(id) DO UPDATE SET
    {", ".join(f"{c} = COALESCE(NULLIF(excluded.{c}, ''), scores.{c})" for c in _SCORE_COLUMNS)},
    rx = COALESCE(excluded.rx, scores.rx),
    raw_json = json_patch(scores.raw_json, excluded.raw_json)
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str = DB_PATH) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        if str(path) != ":memory:":
            os.chmod(path, 0o600)

    def close(self) -> None:
        self.conn.close()

    # --- workouts -------------------------------------------------------

    def upsert_workouts(self, workouts: Iterable[Workout]) -> int:
        rows = [
            (w.id, w.date, w.title, w.description, w.type, w.program, w.section, json.dumps(w.raw))
            for w in workouts
        ]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO workouts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def get_workouts(self, start: str, end: str) -> list[Workout]:
        cur = self.conn.execute(
            "SELECT * FROM workouts WHERE date BETWEEN ? AND ? ORDER BY date, program, rowid", (start, end)
        )
        return [_workout(r) for r in cur]

    def get_workout(self, workout_id: str) -> Workout | None:
        row = self.conn.execute("SELECT * FROM workouts WHERE id = ?", (workout_id,)).fetchone()
        return _workout(row) if row else None

    def find_workouts(self, date: str, title: str | None = None) -> list[Workout]:
        rows = [_workout(r) for r in self.conn.execute("SELECT * FROM workouts WHERE date = ? ORDER BY rowid", (date,))]
        if title:
            needle = title.strip().lower()
            rows = [w for w in rows if needle in f"{w.program} {w.section} {w.title}".lower()]
        return rows

    def mark_day_synced(self, date: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO synced_days VALUES (?, ?)", (date, _now()))

    def synced_days(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT date FROM synced_days")}

    # --- scores ---------------------------------------------------------

    def upsert_scores(self, scores: Iterable[Score]) -> int:
        rows = [
            (s.id, s.workout_id, s.date, s.score, s.score_type, s.division, s.title, s.description, s.section,
             None if s.rx is None else int(s.rx), json.dumps(s.raw))
            for s in scores
        ]
        with self.conn:
            self.conn.executemany(_UPSERT_SCORE, rows)
        return len(rows)

    def get_scores(self, start: str | None = None, end: str | None = None, limit: int | None = None) -> list[Score]:
        # Fall back to the workout's text when the score row lacks it.
        sql = """
            SELECT s.id, s.workout_id, s.date, s.score, s.score_type, s.division, s.rx, s.raw_json,
                   COALESCE(NULLIF(s.title, ''), w.title, '') AS title,
                   COALESCE(NULLIF(s.description, ''), w.description, '') AS description,
                   COALESCE(NULLIF(s.section, ''), w.section, '') AS section
            FROM scores s LEFT JOIN workouts w ON w.id = s.workout_id
            WHERE (? IS NULL OR s.date >= ?) AND (? IS NULL OR s.date <= ?)
            ORDER BY s.date DESC, CAST(s.id AS INTEGER) DESC, s.id DESC
        """
        params: list[object] = [start, start, end, end]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [_score(r) for r in self.conn.execute(sql, params)]

    def score_date_for_workout(self, workout_id: str) -> str | None:
        row = self.conn.execute("SELECT date FROM scores WHERE workout_id = ? LIMIT 1", (workout_id,)).fetchone()
        return row[0] if row else None

    def count_scores(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]

    # --- leaderboards ---------------------------------------------------

    def replace_leaderboard(self, workout_id: str, entries: Iterable[LeaderboardEntry]) -> int:
        fetched_at = _now()
        rows = [(workout_id, e.rank, e.name, e.score, e.division, int(e.is_me), fetched_at) for e in entries]
        with self.conn:
            self.conn.execute("DELETE FROM leaderboard_entries WHERE workout_id = ?", (workout_id,))
            self.conn.executemany("INSERT INTO leaderboard_entries VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def get_leaderboard(self, workout_id: str) -> tuple[list[LeaderboardEntry], str | None]:
        cur = self.conn.execute(
            "SELECT * FROM leaderboard_entries WHERE workout_id = ? ORDER BY rowid", (workout_id,)
        )
        rows = cur.fetchall()
        entries = [
            LeaderboardEntry(r["workout_id"], r["rank"], r["name"], r["score"], r["division"], bool(r["is_me"]))
            for r in rows
        ]
        return entries, (rows[0]["fetched_at"] if rows else None)


def _workout(r: sqlite3.Row) -> Workout:
    return Workout(
        id=r["id"], date=r["date"], title=r["title"], description=r["description"], type=r["type"] or "",
        program=r["program"] or "", section=r["section"] or "", raw=json.loads(r["raw_json"] or "{}"),
    )


def _score(r: sqlite3.Row) -> Score:
    return Score(
        id=r["id"], workout_id=r["workout_id"], date=r["date"], score=r["score"],
        score_type=r["score_type"] or "", division=r["division"] or "",
        rx=None if r["rx"] is None else bool(r["rx"]),
        title=r["title"], description=r["description"], section=r["section"],
        raw=json.loads(r["raw_json"] or "{}"),
    )
