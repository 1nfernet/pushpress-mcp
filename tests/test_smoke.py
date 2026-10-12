"""Offline smoke tests. No network: a fake GraphQL server stands in for PushPress.

The fake responses mirror the shapes captured in Phase 0 (docs/endpoints.md),
with made-up data.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from pushpress_mcp import endpoints
from pushpress_mcp.client import PushPressClient
from pushpress_mcp.csv_format import ExportRow, read_pushpress_export
from pushpress_mcp.errors import APIError, SessionExpiredError
from pushpress_mcp.models import LeaderboardEntry, Score, Workout
from pushpress_mcp.store import Store
from pushpress_mcp.tools import Tools


def make_jwt(exp: dt.datetime) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'HS256'})}.{enc({'sub': 'usr_test', 'exp': int(exp.timestamp())})}.c2lnbmF0dXJl"


TOKEN = make_jwt(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30))
SESSION = {"bearer_token": TOKEN}


class FakePushPress:
    """Answers the four GraphQL operations the server uses."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.status = 200

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status)
        assert req.url.path == "/v2/graph/graphql"
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        body = json.loads(req.content)
        op, v = body["operationName"], body["variables"]
        self.calls.append(op)
        if op == "GetWorkoutTypes":
            data = {"workoutTypes": [{"name": "CrossFit", "uid": "ct-cf"}]}
        elif op == "GetWorkoutOfDay":
            data = {"workoutOfDay": [self.wod(v["classDate"])]}
        elif op == "UserScoreHistory":
            data = {"scoreHistory": [
                {"workoutUid": "w-2026-04-01", "workoutScoreId": 1001, "primaryScore": 1, "secondaryScore": 110,
                 "workoutDate": "2026-04-01T00:00:00.000Z", "workoutTitle": "Pre-workout strength",
                 "workoutPartUid": "p-2026-04-01-a", "title": "260401", "scoreType": "FOR_WEIGHT",
                 "measurementUnit": "IMPERIAL"},
                {"workoutUid": "w-2026-03-31", "workoutScoreId": 1000, "primaryScore": 13, "secondaryScore": 1,
                 "workoutDate": "2026-03-31T00:00:00.000Z", "workoutTitle": "", "workoutPartUid": "p-2026-03-31-b",
                 "title": "260331", "scoreType": "FOR_TIME", "measurementUnit": "IMPERIAL"},
            ]}
        elif op == "GetWorkoutScores":
            assert v["workoutPartUid"] and v["date"] and v["workoutUid"] and v["classTypeId"] == 143832
            data = {"workoutGetScores": {"scores": [
                {"id": 1, "division": "Rx", "athleteDisplayName": "Alex A", "primaryScore": 9, "secondaryScore": 0, "mine": False},
                {"id": 2, "division": "Rx", "athleteDisplayName": "Me Myself", "primaryScore": 9, "secondaryScore": 30, "mine": True},
                {"id": 3, "division": "Scaled", "athleteDisplayName": "Sam S", "primaryScore": 8, "secondaryScore": 5, "mine": False},
            ]}}
        else:
            raise AssertionError(f"unexpected operation {op}")
        return httpx.Response(200, json={"data": data})

    @staticmethod
    def wod(date: str) -> dict:
        return {
            "uid": f"w-{date}", "id": 143832, "title": "CrossFit", "workoutUid": f"w-{date}", "classTypeId": 143832,
            "workoutState": "PUBLISHED", "publishingDate": f"{date} 00:00:00.0",
            "parts": [
                {"workoutPartUid": f"p-{date}-a", "title": date.replace("-", "")[2:], "workoutTitle": "Pre-workout strength",
                 "description": "For load:\nPush press\n3-2-2-1-1-1", "scoreType": "Weight", "divisions": ["Rx", "Scaled"],
                 "score": {"id": int(date.replace("-", "")[4:]) + 1000 - 400, "division": "Rx", "date": "x",
                           "primaryScore": 1, "secondaryScore": 110, "athleteComment": ""}},
                {"workoutPartUid": f"p-{date}-b", "title": "Metcon", "workoutTitle": "", "description": "21-15-9\nThrusters",
                 "scoreType": "Time", "divisions": ["Rx", "Scaled"], "score": None},
            ],
        }


@pytest.fixture
def api():
    return FakePushPress()


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def client_for(handler) -> PushPressClient:
    return PushPressClient(SESSION, transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None)


@pytest.fixture
def tools(store, api):
    return Tools(store, client_factory=lambda: client_for(api))


# --- score formatting (rules derived from the CSV vs API comparison) ----------


@pytest.mark.parametrize("score_type, p, s, comment, expected", [
    ("FOR_TIME", 13, 1, None, "13:01"),
    ("FOR_TIME", 8, 9.1, None, "8:09.1"),
    ("FOR_TIME", 0, 45.8, None, "0:45.8"),
    ("FOR_WEIGHT", 1, 112.5, None, "1 @ 112.5"),
    ("Weight", 42, 550, None, "42 @ 550"),
    ("FOR_ROUNDS_REPS", 6, 21, None, "6 + 21"),
    ("Rounds + Reps", 0, 155, None, "0 + 155"),
    ("FOR_REPS", 374, 0, None, "374"),
    ("FOR_DISTANCE", 4801, 0, None, "4801"),
    ("FOR_CALORIES", 19, 0, None, "19"),
    ("FOR_COMPLETION", 1, 0, None, "Complete"),
    ("FOR_COMMENT", 0, 0, " 35#, 55#, 75# ", "35#, 55#, 75#"),
])
def test_format_score(score_type, p, s, comment, expected):
    assert endpoints.format_score(score_type, p, s, comment) == expected


def test_normalize_score_type():
    assert endpoints.normalize_score_type("Reps") == "FOR_REPS"
    assert endpoints.normalize_score_type("FOR_ROUNDS_REPS") == "FOR_ROUNDS_REPS"
    assert endpoints.normalize_score_type("Rounds and Reps") == "FOR_ROUNDS_REPS"
    assert endpoints.normalize_score_type(None) == ""


# --- auth / client -----------------------------------------------------------

RELOGIN = "Session expired. Run `uv run python -m pushpress_mcp.auth`."


def test_expired_or_missing_token_gives_relogin_message():
    expired = make_jwt(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
    for session in ({"bearer_token": expired}, {}):
        with pytest.raises(SessionExpiredError) as e:
            PushPressClient(session)
        assert str(e.value) == RELOGIN


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_failure_gives_relogin_message(status):
    with pytest.raises(SessionExpiredError) as e:
        client_for(lambda req: httpx.Response(status)).get_json("/api/x")
    assert str(e.value) == RELOGIN


def test_graphql_auth_error_gives_relogin_message():
    body = {"errors": [{"message": "Unauthorized", "extensions": {"code": "UNAUTHENTICATED"}}]}
    client = client_for(lambda req: httpx.Response(200, json=body))
    with pytest.raises(SessionExpiredError):
        endpoints.graphql(client, endpoints.CLASS_TYPES_QUERY, {"classDate": "2026-04-01"})


def test_graphql_other_error_and_mutation_guard():
    client = client_for(lambda req: httpx.Response(200, json={"errors": [{"message": "Bad date"}]}))
    with pytest.raises(APIError, match="Bad date"):
        endpoints.graphql(client, endpoints.CLASS_TYPES_QUERY, {"classDate": "x"})
    with pytest.raises(ValueError, match="read-only"):
        endpoints.graphql(client, "mutation LogScore { logScore { id } }", {})


def test_login_page_counts_as_expired():
    with pytest.raises(SessionExpiredError):
        client_for(lambda req: httpx.Response(200, html="<html>Log in</html>")).get_json("/api/x")


def test_token_never_in_errors():
    with pytest.raises(APIError) as e:
        client_for(lambda req: httpx.Response(500)).get_json("/api/x", {"t": TOKEN})
    assert TOKEN not in str(e.value) and "/api/x" in str(e.value)


def test_retries_then_succeeds():
    attempts = []

    def handler(req):
        attempts.append(1)
        return httpx.Response(503) if len(attempts) < 2 else httpx.Response(200, json={"ok": True})

    assert client_for(handler).get_json("/api/x") == {"ok": True}
    assert len(attempts) == 2


# --- store -------------------------------------------------------------------


def test_store_merges_scores_from_both_sources(store):
    store.upsert_workouts([Workout("p1", "2026-04-01", "Fran", "21-15-9", "FOR_TIME", "CrossFit", "", raw={"a": 1})])
    # Workout sync knows the division; history doesn't. Order shouldn't matter.
    store.upsert_scores([Score("s1", "p1", "2026-04-01", "4:12", "FOR_TIME", "Rx", True, raw={"workout_score": {}})])
    store.upsert_scores([Score("s1", "p1", "2026-04-01", "4:12", "FOR_TIME", title="Fran", raw={"history": {}})])
    [s] = store.get_scores()
    assert (s.division, s.rx, s.title, s.description) == ("Rx", True, "Fran", "21-15-9")
    assert set(s.raw) == {"workout_score", "history"}

    store.replace_leaderboard("p1", [LeaderboardEntry("p1", 1, "A", "3:00")])
    store.replace_leaderboard("p1", [LeaderboardEntry("p1", 1, "B", "2:59", is_me=True)])
    entries, fetched_at = store.get_leaderboard("p1")
    assert [(e.name, e.is_me) for e in entries] == [("B", True)] and fetched_at


# --- tools against the fake API ---------------------------------------------


def test_get_workouts_live(tools, api, store):
    resp = tools.get_workouts("2026-04-01", "2026-04-03")
    assert resp["source"] == "live" and resp["count"] == 6  # 3 days x 2 parts
    assert api.calls.count("GetWorkoutTypes") == 3 and api.calls.count("GetWorkoutOfDay") == 3
    first = resp["workouts"][0]
    assert first == {"date": "2026-04-01", "program": "CrossFit", "section": "Pre-workout strength", "title": "260401",
                     "description": "For load:\nPush press\n3-2-2-1-1-1", "type": "FOR_WEIGHT", "id": "p-2026-04-01-a"}
    # My scores attached to the workout parts were harvested too, with division.
    assert {(s.score, s.division, s.rx) for s in store.get_scores()} == {("1 @ 110", "Rx", True)}


def test_get_my_scores_merges_division_from_workouts(tools, api):
    tools.get_workouts("2026-04-01", "2026-04-01")
    resp = tools.get_my_scores(limit=10)
    assert resp["source"] == "live"
    by_date = {s["date"]: s for s in resp["scores"]}
    assert by_date["2026-03-31"]["score"] == "13:01"
    assert set(by_date["2026-03-31"]) == {"date", "workout_id", "title", "description", "score", "score_type",
                                          "division", "rx", "section"}


def test_get_leaderboard_by_date_and_title(tools, api):
    resp = tools.get_leaderboard(date="2026-04-02", title="metcon")
    assert resp["source"] == "live" and resp["title"] == "Metcon"
    assert resp["leaderboard"] == [
        {"rank": 1, "name": "Alex A", "score": "9:00", "division": "Rx"},
        {"rank": 2, "name": "Me Myself", "score": "9:30", "division": "Rx", "me": True},
        {"rank": 1, "name": "Sam S", "score": "8:05", "division": "Scaled"},
    ]
    ambiguous = tools.get_leaderboard(date="2026-04-02")
    assert "candidates" in ambiguous and len(ambiguous["candidates"]) == 2


def test_get_leaderboard_for_workout_known_only_from_history(tools, api):
    tools.get_my_scores()
    resp = tools.get_leaderboard(workout_id="p-2026-03-31-b")
    assert resp["source"] == "live" and resp["date"] == "2026-03-31"


def test_falls_back_to_cache_when_session_expired(store, api):
    tools = Tools(store, client_factory=lambda: client_for(api))
    tools.get_workouts("2026-04-01", "2026-04-01")
    api.status = 401
    resp = tools.get_workouts("2026-04-01", "2026-04-01")
    assert resp["source"] == "cache" and resp["count"] == 2
    assert "uv run python -m pushpress_mcp.auth" in resp["warning"]


def test_missing_session_falls_back_with_relogin_hint(store):
    resp = Tools(store, client_factory=lambda: PushPressClient({})).get_my_scores()
    assert resp["source"] == "cache" and RELOGIN in resp["warning"]


def test_sync_resumes(tools, api):
    days_back = (dt.date.today() - dt.date(2026, 3, 31)).days  # reach the fake history
    first = tools.sync_cache(days_back=days_back, days_forward=0)
    assert "errors" not in first and first["scores_from_history"] == 2
    assert first["workouts_fetched"] == 2 * (days_back + 1)
    api.calls.clear()
    tools.sync_cache(days_back=days_back, days_forward=0)
    # Settled days are skipped; only the last few are re-fetched.
    assert api.calls.count("GetWorkoutOfDay") == 4


def test_export_roundtrip(tools, api, store, tmp_path):
    tools.get_workouts("2026-04-01", "2026-04-01")
    store.upsert_scores([Score("q", "w", "2026-04-02", "Got 1!", division="Rx", section='Box "30"', description="a\nb")])
    out = tmp_path / "scores.csv"
    resp = tools.export_scores_csv(str(out))
    rows = read_pushpress_export(out)
    assert resp["rows"] == len(rows) == store.count_scores() == 2
    assert (rows[0].title, rows[0].description, rows[0].score, rows[0].date) == ('Box "30"', "a\nb", "Got 1!", "2026-04-02")
    assert (rows[1].title, rows[1].score, rows[1].division) == ("Pre-workout strength", "1 @ 110", "Rx")


def test_export_rejects_non_csv(store, tmp_path):
    with pytest.raises(ValueError):
        Tools(store).export_scores_csv(str(tmp_path / "x.txt"))


# --- CSV ground truth --------------------------------------------------------


def test_reads_pushpress_export_quirks(tmp_path):
    p = tmp_path / "export.csv"
    p.write_text(
        '"Workout Title","Workout Description","Score","Division","Date Time"\n'
        '"","For time:\n30" box jumps\n","11:30","Rx",6/6/2024 12:0 AM\n'
        '"Day 1","Set 1, 2 reps","Complete","null",4/1/2026 12:0 AM\n'
    )
    rows = read_pushpress_export(p)
    assert [(r.description, r.score, r.date) for r in rows] == [
        ('For time:\n30" box jumps\n', "11:30", "2024-06-06"),
        ("Set 1, 2 reps", "Complete", "2026-04-01"),
    ]


def test_validator_matching_and_repeats():
    from validate_csv import build_report, match

    csv_rows = [
        ExportRow("", "Fran 21-15-9", "4:12", "Rx", "2026-04-01"),
        ExportRow("", "Fran 21-15-9", "4:12", "Rx", "2026-04-01"),  # export repeats rows
        ExportRow("", "Back squat 5x5", "5 @ 185", "Rx", "2026-04-01"),
        ExportRow("", "Grace", "3:00", "Rx", "2026-04-02"),
    ]
    cache = [
        Score("1", "w", "2026-04-01", "4:12", description="Fran 21-15-9"),
        Score("2", "w", "2026-04-01", "5 @ 190", description="Back squat 5x5"),
        Score("3", "w", "2026-04-03", "100", description="Cindy"),
    ]
    res = match(csv_rows, cache)
    assert len(res.matched) == 1 and len(res.different) == 1
    assert sorted(r.description for r in res.missing) == ["Fran 21-15-9", "Grace"]
    assert [s.description for s in res.extra] == ["Cindy"]
    report = build_report(Path("x.csv"), csv_rows, cache, sample=2, seed=1)
    assert "missing from cache: 1 (+1 repeated CSV rows)" in report


# --- discovery / server ------------------------------------------------------


def test_summarize_har_redacts_jwts_and_splits_graphql(tmp_path, capsys, monkeypatch):
    import summarize_har

    def entry(url, method="GET", post=None, body=None):
        return {
            "request": {"method": method, "url": url, "headers": [{"name": "Authorization", "value": f"Bearer {TOKEN}"}],
                        **({"postData": {"text": json.dumps(post)}} if post else {})},
            "response": {"status": 200, "content": {"mimeType": "application/json", "text": json.dumps(body or {})}},
        }

    gql = "https://api.example.com/v2/graph/graphql"
    har = {"log": {"entries": [
        entry(f"https://api.example.com/v2/auth/verify/{TOKEN}", body={"isValid": True}),
        entry(f"https://api.example.com/v1/workouts/12345?date=2026-04-01&access_token={TOKEN}", body={"data": []}),
        entry("https://api.example.com/v2/auth/login", "POST", body={"accessToken": TOKEN, "refreshToken": "r" * 40}),
        entry(gql, "POST", {"operationName": "Workouts", "query": "query Workouts($from: String) { workouts { id } }",
                            "variables": {"from": "2026-04-01", "t": TOKEN}}, {"data": {"workouts": [{"id": 1, "note": TOKEN}]}}),
        entry(gql, "POST", {"query": "{ me { id } }"}, {"data": {"me": {"id": 2}}}),
    ]}}
    path = tmp_path / "s.har"
    path.write_text(json.dumps(har))
    monkeypatch.setattr(summarize_har, "SAMPLES_DIR", tmp_path / "samples")
    monkeypatch.setattr(summarize_har, "ROOT", tmp_path)
    assert summarize_har.summarize(path, dump_samples=True) == 0
    out = capsys.readouterr().out
    assert TOKEN.split(".")[1] not in out and "eyJ" not in out
    assert "/v2/auth/verify/{jwt}" in out and "/v1/workouts/{id}" in out and "access_token=<redacted>" in out
    assert ":: query Workouts" in out and "$from=2026-04-01" in out and "$t=<redacted>" in out
    assert ":: query {me}" in out and "data: {workouts}" in out and "authorization" in out
    for f in (tmp_path / "samples").iterdir():
        text = f.read_text()
        assert "eyJ" not in text and "r" * 40 not in text, f.name
        assert f.stat().st_mode & 0o077 == 0


def test_server_registers_tools():
    from pushpress_mcp.server import server

    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {"get_workouts", "get_my_scores", "get_leaderboard", "sync_cache", "export_scores_csv"}


def test_gitignore_covers_secrets():
    ignored = (Path(__file__).parents[1] / ".gitignore").read_text()
    for pattern in ("state/", "discovery/", "*.db", "*.har"):
        assert pattern in ignored
