"""Everything specific to PushPress's (unofficial) member-app API lives here.

Discovered in Phase 0; see docs/endpoints.md. The member app (a Flutter web
app at members.pushpress.com) reads workouts and scores from a GraphQL API at
api.pushpress.com/v2/graph/graphql, authenticated with a bearer JWT.

Queries below are trimmed copies of the app's own queries (only fields we
use; no other members' photos, comments or likes). Only queries are ever
sent, never mutations.

Contract used by tools.py:
  fetch_*  -> yield raw JSON dicts; one client call per request so the
              client's rate limiting applies.
  parse_*  -> raw dict(s) -> models. Raw JSON is stored too, so parsers can be
              fixed later without re-fetching.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
from typing import TYPE_CHECKING, Any, Iterable, Iterator

from .errors import APIError, SessionExpiredError
from .models import LeaderboardEntry, Score, Workout

if TYPE_CHECKING:
    from .client import PushPressClient

LOGIN_URL = "https://members.pushpress.com"
API_BASE_URL = "https://api.pushpress.com"
GRAPHQL_PATH = "/v2/graph/graphql"

# The workouts API is one day at a time: 1 call for the day's programs
# ("class types"), then 1 call per program.
WORKOUTS_MAX_RANGE_DAYS = 1

CLASS_TYPES_QUERY = """
query GetWorkoutTypes($classDate: String!) {
  workoutTypes: getClassTypes(getClassTypesInput: {date: $classDate}) { name uid }
}"""

WORKOUT_OF_DAY_QUERY = """
query GetWorkoutOfDay($classDate: String!, $classTypeId: String!) {
  workoutOfDay: getWorkoutOfDay(getWorkoutOfDayInput: {date: $classDate, classTypeUid: $classTypeId}) {
    uid id title workoutUid classTypeId workoutState publishingDate
    parts {
      workoutPartUid title workoutTitle description scoreType athletesNotes scoreCount sets divisions unit
      score { id division date unit primaryScore secondaryScore athleteComment sets { primaryScore secondaryScore } }
    }
  }
}"""

SCORE_HISTORY_QUERY = """
query UserScoreHistory($keyword: String!) {
  scoreHistory: userScoreHistory(userScoreHistoryInput: {keyword: $keyword}) {
    workoutUid workoutScoreId primaryScore secondaryScore workoutDate workoutTitle workoutPartUid title
    scoreType measurementUnit
  }
}"""

LEADERBOARD_QUERY = """
query GetWorkoutScores($workoutUid: String, $classTypeId: Float, $workoutPartUid: String!, $date: String!) {
  workoutGetScores(workoutGetScoresInput: {classTypeId: $classTypeId, date: $date, workoutPartUid: $workoutPartUid, workoutUid: $workoutUid}) {
    scores { id division date athleteDisplayName primaryScore secondaryScore athleteComment mine }
  }
}"""


# --- auth -----------------------------------------------------------------


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def token_expiry(session: dict[str, Any]) -> dt.datetime | None:
    exp = _jwt_claims(session.get("bearer_token", "")).get("exp")
    return dt.datetime.fromtimestamp(exp, dt.timezone.utc) if isinstance(exp, (int, float)) else None


def auth_headers(session: dict[str, Any]) -> dict[str, str]:
    """Headers for API calls. Raises SessionExpiredError if there's no usable token."""
    token = session.get("bearer_token")
    if not token:
        raise SessionExpiredError()
    expiry = token_expiry(session)
    if expiry and expiry <= dt.datetime.now(dt.timezone.utc):
        raise SessionExpiredError()
    return {"Authorization": f"Bearer {token}", "Origin": LOGIN_URL, "Referer": f"{LOGIN_URL}/"}


def graphql(client: PushPressClient, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    if re.match(r"\s*mutation\b", query):
        raise ValueError("Refusing to send a GraphQL mutation: this server is read-only.")
    op = m[1] if (m := re.search(r"query\s+(\w+)", query)) else None
    body = client.post_json(GRAPHQL_PATH, {"operationName": op, "query": query, "variables": variables})
    if errors := body.get("errors"):
        text = "; ".join(str(e.get("message", e)) for e in errors)[:300]
        codes = {str((e.get("extensions") or {}).get("code", "")).upper() for e in errors}
        if codes & {"UNAUTHENTICATED", "FORBIDDEN"} or re.search(r"unauthori[sz]ed|jwt|token", text, re.I):
            raise SessionExpiredError()
        raise APIError(f"GraphQL {op} failed: {text}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise APIError(f"GraphQL {op} returned no data.")
    return data


# --- score formatting -------------------------------------------------------

_TYPE_ALIASES = {"FOR_LOAD": "FOR_WEIGHT", "FOR_CALS": "FOR_CALORIES", "FOR_ROUNDS": "FOR_ROUNDS_REPS"}


def normalize_score_type(raw: str | None) -> str:
    """History says FOR_REPS, workout parts say "Reps": map both to FOR_*."""
    if not raw:
        return ""
    s = re.sub(r"[^A-Z]+", "_", raw.upper()).strip("_").replace("_AND_", "_")
    if not s.startswith("FOR_"):
        s = f"FOR_{s}"
    return _TYPE_ALIASES.get(s, s)


def _num(x: Any) -> str:
    if x is None:
        return "0"
    f = float(x)
    return str(int(f)) if f == int(f) else f"{f:g}"


def format_score(score_type: str, primary: Any, secondary: Any, comment: str | None = None) -> str:
    """Render the way the app and the CSV export do, e.g. 13:01, 1 @ 110, 6 + 21."""
    t = normalize_score_type(score_type)
    if t == "FOR_TIME":
        sec = float(secondary or 0)
        return f"{_num(primary)}:" + (f"{int(sec):02d}" if sec == int(sec) else f"{sec:04.1f}")
    if t == "FOR_WEIGHT":
        return f"{_num(primary)} @ {_num(secondary)}"
    if t == "FOR_ROUNDS_REPS":
        return f"{_num(primary)} + {_num(secondary)}"
    if t == "FOR_COMPLETION":
        return "Complete" if primary else "Incomplete"
    if t == "FOR_COMMENT":
        return (comment or "").strip()
    if t in ("FOR_REPS", "FOR_CALORIES", "FOR_DISTANCE", "FOR_POINTS"):
        return _num(primary)
    return _num(primary) + (f" + {_num(secondary)}" if secondary else "")


def _rx(division: str | None) -> bool | None:
    d = (division or "").strip().lower()
    return True if d in ("rx", "rx+") else False if d == "scaled" else None


# --- workouts ---------------------------------------------------------------


def fetch_workouts(client: PushPressClient, start: dt.date, end: dt.date) -> Iterator[dict[str, Any]]:
    """Every program's workout for each day in [start, end]. Adds `_date` and `_program`."""
    day = start
    while day <= end:
        date = day.isoformat()
        for program in graphql(client, CLASS_TYPES_QUERY, {"classDate": date}).get("workoutTypes") or []:
            data = graphql(client, WORKOUT_OF_DAY_QUERY, {"classDate": date, "classTypeId": program["uid"]})
            for wod in data.get("workoutOfDay") or []:
                yield {**wod, "_date": date, "_program": {"name": program.get("name"), "uid": program.get("uid")}}
        day += dt.timedelta(days=1)


def parse_workouts(raw: dict[str, Any]) -> list[Workout]:
    """One Workout per part. Each part's raw keeps the workout fields needed for leaderboards."""
    wod = {k: v for k, v in raw.items() if k != "parts"}
    out = []
    for part in raw.get("parts") or []:
        part_raw = {k: v for k, v in part.items() if k != "score"}
        out.append(Workout(
            id=str(part["workoutPartUid"]),
            date=raw["_date"],
            title=(part.get("title") or raw.get("title") or "").strip(),
            description=(part.get("description") or "").strip(),
            type=normalize_score_type(part.get("scoreType")),
            program=(raw.get("_program") or {}).get("name") or "",
            section=(part.get("workoutTitle") or "").strip(),
            raw={"workout": wod, "part": part_raw},
        ))
    return out


def scores_from_workout(raw: dict[str, Any]) -> list[Score]:
    """My score attached to each part of a workout-of-day (carries division and comment)."""
    out = []
    for part in raw.get("parts") or []:
        s = part.get("score")
        if not s or s.get("id") is None:
            continue
        score_type = normalize_score_type(part.get("scoreType"))
        out.append(Score(
            id=str(s["id"]),
            workout_id=str(part["workoutPartUid"]),
            date=raw["_date"],
            score=format_score(score_type, s.get("primaryScore"), s.get("secondaryScore"), s.get("athleteComment")),
            score_type=score_type,
            division=s.get("division") or "",
            rx=_rx(s.get("division")),
            title=(part.get("title") or raw.get("title") or "").strip(),
            description=(part.get("description") or "").strip(),
            section=(part.get("workoutTitle") or "").strip(),
            raw={"workout_score": s},
        ))
    return out


# --- scores -----------------------------------------------------------------


def fetch_scores(client: PushPressClient, start: dt.date | None, end: dt.date | None) -> Iterator[dict[str, Any]]:
    """My logged results, newest first. The API returns full history in one call.

    History starts around 2025-05-01 for this account; older scores only come
    through the workout-of-day sync (scores_from_workout).
    """
    data = graphql(client, SCORE_HISTORY_QUERY, {"keyword": ""})
    rows = data.get("scoreHistory") or []
    rows.sort(key=lambda r: (r.get("workoutDate") or "", r.get("workoutScoreId") or 0), reverse=True)
    for r in rows:
        date = (r.get("workoutDate") or "")[:10]
        if (start and date < start.isoformat()) or (end and date > end.isoformat()):
            continue
        yield r


def parse_score(raw: dict[str, Any]) -> Score:
    # History has no division or description; the workout sync fills those in.
    score_type = normalize_score_type(raw.get("scoreType"))
    return Score(
        id=str(raw["workoutScoreId"]),
        workout_id=str(raw.get("workoutPartUid") or ""),
        date=raw["workoutDate"][:10],
        score=format_score(score_type, raw.get("primaryScore"), raw.get("secondaryScore")),
        score_type=score_type,
        title=(raw.get("title") or "").strip(),
        section=(raw.get("workoutTitle") or "").strip(),
        raw={"history": raw},
    )


# --- leaderboards -----------------------------------------------------------


def fetch_leaderboard(client: PushPressClient, workout: Workout) -> Iterator[dict[str, Any]]:
    """Leaderboard rows for one workout part, in the order the app shows them."""
    wod = workout.raw.get("workout") or {}
    variables = {
        "workoutUid": wod.get("workoutUid") or wod.get("uid"),
        "classTypeId": wod.get("classTypeId"),
        "workoutPartUid": workout.id,
        "date": workout.date,
    }
    data = graphql(client, LEADERBOARD_QUERY, variables)
    yield from (data.get("workoutGetScores") or {}).get("scores") or []


def parse_leaderboard(raws: Iterable[dict[str, Any]], workout: Workout) -> list[LeaderboardEntry]:
    """Rank is the position within each division (Rx and Scaled aren't comparable)."""
    seen: dict[str, int] = {}
    out = []
    for r in raws:
        division = r.get("division") or ""
        seen[division] = seen.get(division, 0) + 1
        out.append(LeaderboardEntry(
            workout_id=workout.id,
            rank=seen[division],
            name=(r.get("athleteDisplayName") or "").strip(),
            score=format_score(workout.type, r.get("primaryScore"), r.get("secondaryScore"), r.get("athleteComment")),
            division=division,
            is_me=bool(r.get("mine")),
        ))
    return out
