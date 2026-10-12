"""Normalized records produced by the endpoint parsers and stored in SQLite.

PushPress terms: a *workout* (WOD) is published per program ("class type") per
day and has one or more *parts* (e.g. a strength piece, then a metcon). Scores
attach to parts, so a Workout here is one part.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Workout:
    id: str  # workoutPartUid
    date: str  # YYYY-MM-DD
    title: str  # part title, e.g. "Topsy", "Skill Work"
    description: str
    type: str = ""  # normalized score type, e.g. FOR_TIME
    program: str = ""  # class type name, e.g. "HYROX"
    section: str = ""  # PushPress "workoutTitle", e.g. "Post-workout accessory"
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "date": self.date, "program": self.program, "section": self.section, "title": self.title,
            "description": self.description, "type": self.type, "id": self.id,
        }


@dataclass
class Score:
    id: str
    workout_id: str  # workoutPartUid
    date: str  # YYYY-MM-DD
    score: str  # display text, e.g. "13:01", "1 @ 110", "6 + 21"
    score_type: str = ""
    division: str = ""
    rx: bool | None = None
    title: str = ""
    description: str = ""
    section: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw")
        d.pop("id")
        return d


@dataclass
class LeaderboardEntry:
    workout_id: str
    rank: int  # position within the division, in the order the app shows
    name: str
    score: str
    division: str = ""
    is_me: bool = False

    def public(self) -> dict[str, Any]:
        d = {"rank": self.rank, "name": self.name, "score": self.score, "division": self.division}
        if self.is_me:
            d["me"] = True
        return d
