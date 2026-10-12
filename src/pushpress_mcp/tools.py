"""Tool logic, independent of the MCP transport so it can be tested directly.

get_* tools read live, upsert into the cache, and fall back to the cache (with
a warning in the response) when the live call fails.

`uv run python -m pushpress_mcp.tools sync --days-back 3650` runs sync_cache
from the terminal, which is better for long full-history syncs than an MCP
call (it takes ~1s per request).
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any, Callable, Iterator

from . import endpoints
from .client import PushPressClient
from .csv_format import write_scores_csv
from .errors import PushPressError, SessionExpiredError
from .models import Workout
from .store import Store

log = logging.getLogger(__name__)

DEFAULT_SCORES_LIMIT = 50
# Past days are re-fetched on sync only if this recent (scores can still be logged).
RESYNC_RECENT_DAYS = 3
# Raised by parsers when the response doesn't have the shape we expect.
_SHAPE_ERRORS = (KeyError, TypeError, ValueError, IndexError)


def _parse_date(value: str, name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}") from None


def _failure_message(e: Exception) -> str:
    if isinstance(e, PushPressError):
        return str(e)
    return f"Unexpected response shape from PushPress ({type(e).__name__}: {e}). The API may have changed."


def _date_chunks(start: dt.date, end: dt.date, days: int) -> Iterator[tuple[dt.date, dt.date]]:
    while start <= end:
        chunk_end = min(start + dt.timedelta(days=days - 1), end)
        yield start, chunk_end
        start = chunk_end + dt.timedelta(days=1)


class Tools:
    def __init__(self, store: Store, client_factory: Callable[[], PushPressClient] = PushPressClient) -> None:
        self.store = store
        self._client_factory = client_factory
        self._client: PushPressClient | None = None

    def _get_client(self) -> PushPressClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _reset_client(self) -> None:
        # Drop the client after a session error so a fresh login is picked up next call.
        if self._client is not None:
            self._client.close()
            self._client = None

    def _live(self, fn: Callable[[], Any]) -> tuple[Any, Exception | None]:
        try:
            return fn(), None
        except (PushPressError, *_SHAPE_ERRORS) as e:
            if isinstance(e, SessionExpiredError):
                self._reset_client()
            log.warning("Live call failed: %s", _failure_message(e))
            return None, e

    def _fetch_workouts(self, start: dt.date, end: dt.date, resume: bool = False) -> tuple[int, int]:
        """Fetch, parse and cache workouts (and my scores attached to them). Returns (workouts, scores).

        With resume=True, days already synced are skipped unless they're recent
        enough that programming or scores may still change.
        """
        client = self._get_client()
        done = self.store.synced_days() if resume else set()
        settled = dt.date.today() - dt.timedelta(days=RESYNC_RECENT_DAYS)
        n_workouts = n_scores = days = 0
        for chunk_start, chunk_end in _date_chunks(start, end, endpoints.WORKOUTS_MAX_RANGE_DAYS):
            if chunk_end < settled and chunk_start.isoformat() in done and chunk_end.isoformat() in done:
                continue
            raws = list(endpoints.fetch_workouts(client, chunk_start, chunk_end))
            # Per chunk, so partial progress is kept if a later request fails.
            n_workouts += self.store.upsert_workouts(w for raw in raws for w in endpoints.parse_workouts(raw))
            n_scores += self.store.upsert_scores(s for raw in raws for s in endpoints.scores_from_workout(raw))
            for d in _date_chunks(chunk_start, chunk_end, 1):
                self.store.mark_day_synced(d[0].isoformat())
            days += 1
            if days % 30 == 0:
                log.info("Synced workouts through %s", chunk_end.isoformat())
        return n_workouts, n_scores

    # --- tools ----------------------------------------------------------

    def get_workouts(self, start_date: str, end_date: str) -> dict[str, Any]:
        start, end = _parse_date(start_date, "start_date"), _parse_date(end_date, "end_date")
        if end < start:
            raise ValueError("end_date is before start_date")
        _, error = self._live(lambda: self._fetch_workouts(start, end))
        workouts = self.store.get_workouts(start.isoformat(), end.isoformat())
        return self._response("workouts", [w.public() for w in workouts], error)

    def get_my_scores(
        self, start_date: str | None = None, end_date: str | None = None, limit: int | None = DEFAULT_SCORES_LIMIT
    ) -> dict[str, Any]:
        start = _parse_date(start_date, "start_date") if start_date else None
        end = _parse_date(end_date, "end_date") if end_date else None
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1")

        def fetch() -> int:
            raws = endpoints.fetch_scores(self._get_client(), start, end)
            return self.store.upsert_scores(endpoints.parse_score(raw) for raw in raws)

        _, error = self._live(fetch)
        # Read back from the cache: it merges in division/description from synced workouts.
        scores = self.store.get_scores(start and start.isoformat(), end and end.isoformat(), limit)
        return self._response("scores", [s.public() for s in scores], error)

    def get_leaderboard(
        self, workout_id: str | None = None, date: str | None = None, title: str | None = None
    ) -> dict[str, Any]:
        if workout_id:
            resolved = self._workout_by_id(workout_id)
        elif date:
            resolved = self._resolve_workout(_parse_date(date, "date"), title)
        else:
            raise ValueError("Pass workout_id, or date (YYYY-MM-DD) plus optional title.")
        if isinstance(resolved, dict):
            return resolved
        workout = resolved
        about = {"workout_id": workout.id, "date": workout.date, "program": workout.program,
                 "section": workout.section, "title": workout.title}

        def fetch() -> list[dict[str, Any]]:
            entries = endpoints.parse_leaderboard(endpoints.fetch_leaderboard(self._get_client(), workout), workout)
            self.store.replace_leaderboard(workout.id, entries)
            return [e.public() for e in entries]

        live, error = self._live(fetch)
        if live is not None:
            return {"source": "live", **about, "count": len(live), "leaderboard": live}
        entries, fetched_at = self.store.get_leaderboard(workout.id)
        resp = self._response("leaderboard", [e.public() for e in entries], error)
        return {**resp, **about, "cached_at": fetched_at}

    def _workout_by_id(self, workout_id: str) -> Workout | dict[str, Any]:
        workout = self.store.get_workout(workout_id)
        error = None
        if workout is None and (date := self.store.score_date_for_workout(workout_id)):
            # Known from score history but not fetched yet: pull that day's programming.
            _, error = self._live(lambda: self._fetch_workouts(dt.date.fromisoformat(date), dt.date.fromisoformat(date)))
            workout = self.store.get_workout(workout_id)
        if workout is None:
            msg = f"Unknown workout_id {workout_id!r}. Call get_workouts for its date first, or pass date and title."
            return {"error": msg + (f" Live lookup failed: {_failure_message(error)}" if error else "")}
        return workout

    def _resolve_workout(self, date: dt.date, title: str | None) -> Workout | dict[str, Any]:
        matches = self.store.find_workouts(date.isoformat(), title)
        error = None
        if not matches:
            _, error = self._live(lambda: self._fetch_workouts(date, date))
            matches = self.store.find_workouts(date.isoformat(), title)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            msg = f"No workout found on {date.isoformat()}" + (f" matching {title!r}" if title else "") + "."
            return {"error": msg + (f" Live lookup failed: {_failure_message(error)}" if error else "")}
        return {
            "error": "Several workouts match; call again with workout_id or a more specific title.",
            "candidates": [{"id": w.id, "program": w.program, "section": w.section, "title": w.title} for w in matches],
        }

    def sync_cache(self, days_back: int = 365, days_forward: int = 14) -> dict[str, Any]:
        """Score history (one request), then workouts day by day (1 + programs requests per day).

        Days already synced are skipped (except the last few), so an
        interrupted sync can be re-run and picks up where it left off.
        """
        if days_back < 0 or days_forward < 0:
            raise ValueError("days_back and days_forward must be >= 0")
        today = dt.date.today()
        start, end = today - dt.timedelta(days=days_back), today + dt.timedelta(days=days_forward)
        result: dict[str, Any] = {"range": [start.isoformat(), end.isoformat()], "errors": []}

        scores, error = self._live(
            lambda: self.store.upsert_scores(
                endpoints.parse_score(raw) for raw in endpoints.fetch_scores(self._get_client(), start, today)
            )
        )
        result["scores_from_history"] = scores or 0
        if error:
            result["errors"].append(f"scores: {_failure_message(error)}")

        # Don't keep going if the session is already known to be dead.
        if not isinstance(error, SessionExpiredError):
            counts, error = self._live(lambda: self._fetch_workouts(start, end, resume=True))
            result["workouts_fetched"], result["scores_from_workouts"] = counts or (0, 0)
            if error:
                result["errors"].append(f"workouts: {_failure_message(error)} (re-run to resume)")
        result["scores_cached_total"] = self.store.count_scores()
        if not result["errors"]:
            del result["errors"]
        return result

    def export_scores_csv(self, path: str) -> dict[str, Any]:
        out = Path(path).expanduser().resolve()
        if out.suffix.lower() != ".csv":
            raise ValueError("path must end in .csv")
        if not out.parent.is_dir():
            raise ValueError(f"Directory does not exist: {out.parent}")
        n = write_scores_csv(out, self.store.get_scores())
        return {"path": str(out), "rows": n, "source": "cache"}

    @staticmethod
    def _response(key: str, items: list[dict[str, Any]], error: Exception | None) -> dict[str, Any]:
        resp: dict[str, Any] = {"source": "live" if error is None else "cache"}
        if error:
            resp["warning"] = f"Live fetch failed, showing cached data. {_failure_message(error)}"
        resp["count"] = len(items)
        resp[key] = items
        return resp


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Sync the local PushPress cache.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sync = sub.add_parser("sync")
    sync.add_argument("--days-back", type=int, default=365)
    sync.add_argument("--days-forward", type=int, default=14)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = Store()
    try:
        print(json.dumps(Tools(store).sync_cache(args.days_back, args.days_forward), indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
