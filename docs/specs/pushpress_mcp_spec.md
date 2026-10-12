# PushPress MCP Server: MVP Spec

## 1. Goal
Build a local, read-only MCP server that pulls my workouts (past and future), my logged scores, and leaderboards from the PushPress **member** web app (`https://members.pushpress.com`) using my own login. Test how reliable this is before investing further.

**Success = from Claude, I can ask "what were my last 10 scored WODs" or "what's programmed this week" and get accurate data that matches what I see in the app.**

## 2. Non-goals (MVP)
- No writing to PushPress (no logging scores, no booking classes).
- No multi-user support. Single account (mine).
- No hosting or deployment. Runs locally over stdio.
- No scraping of other members' profiles. Leaderboards only as shown for a given workout.
- No UI.

## 3. Context
- The official PushPress Platform API (`api.pushpress.com/v3`) is gym-owner scoped (API key + company-id) and doesn't appear to expose workouts or scores, so it is **not** used.
- The member web app already loads workouts and results as JSON behind my login. We call those same endpoints with my session.
- Unofficial and unsupported. Keep it read-only, low volume, personal use only.
- I have an existing CSV export (`cfd_score_export_pushpress_20260401.csv`, 924 rows; columns: Workout Title, Workout Description, Score, Division, Date Time). Use it as a **ground-truth check** for the scores tool.

## 4. Stack
- Python 3.11+, managed with `uv`
- `mcp` Python SDK (FastMCP), stdio transport
- `httpx` for API calls
- `playwright` for login only (captures session cookies/tokens)
- `sqlite3` (stdlib) for a local cache
- `pytest` for a few smoke tests

## 5. Architecture
```
Claude  <--stdio-->  MCP server (FastMCP)
                        |-- client.py   (httpx, session auth, retry)
                        |-- auth.py     (Playwright login, session.json)
                        |-- store.py    (SQLite cache)
                        `-- tools.py    (MCP tool definitions)
```

## 6. Phase 0: Endpoint discovery (do this first)
Don't guess endpoints. Discover them.

1. Write `scripts/discover.py`: a Playwright script that opens the members app **headed**, lets me log in manually, then records all XHR/fetch traffic to `discovery/session.har` while I click through:
   - the Workouts tab (today, a past week, a future week)
   - my results/history
   - a workout's leaderboard
2. Write `scripts/summarize_har.py`: reads the HAR and prints a table of unique API calls (method, URL pattern, query params, auth headers present, response top-level keys). Redact tokens in output.
3. From that output, write `docs/endpoints.md` listing the endpoints for: workouts by date range, my scores/history, leaderboard for a workout, plus pagination and date format.

**Gate:** if there are no usable JSON endpoints for scores or leaderboards (e.g. server-rendered HTML or a different backend such as SugarWOD), stop and report what was found. Don't build further.

## 7. Auth
- `auth.py` uses Playwright to open a headed login. I complete login (including any 2FA) manually. It saves cookies/tokens to `state/session.json` (gitignored, chmod 600).
- `client.py` loads `state/session.json` and attaches the needed cookies/headers.
- On a 401/403, raise a clear error: "Session expired. Run `uv run python -m pushpress_mcp.auth`."
- Never log tokens. Credentials are never stored, only the session.

## 8. MCP tools
All read-only. Return compact JSON.

| Tool | Args | Returns |
|---|---|---|
| `get_workouts` | `start_date`, `end_date` (YYYY-MM-DD) | List of `{date, title, description, type, id}` |
| `get_my_scores` | `start_date?`, `end_date?`, `limit?` | List of `{date, workout_id, title, description, score, score_type, division, rx}` |
| `get_leaderboard` | `workout_id` or (`date`, `title`) | List of `{rank, name, score, division}` |
| `sync_cache` | `days_back=365`, `days_forward=14` | Pulls and stores in SQLite; returns counts |
| `export_scores_csv` | `path` | Writes cached scores to CSV in the same 5-column shape as the PushPress export |

Notes:
- `get_*` tools read from the live API and upsert into the cache. If the API fails, fall back to the cache and say so in the response.
- Be polite to the API: sequential requests, 300-500ms delay, max ~1 request per second, no parallel hammering.

## 9. Data model (SQLite)
```
workouts(id TEXT PK, date TEXT, title TEXT, description TEXT, raw_json TEXT)
scores(id TEXT PK, workout_id TEXT, date TEXT, score TEXT, score_type TEXT,
       division TEXT, raw_json TEXT)
leaderboard_entries(workout_id TEXT, rank INT, name TEXT, score TEXT,
       division TEXT, fetched_at TEXT)
```
Always keep `raw_json` so we can re-parse without re-fetching if the schema turns out different.

## 10. Project layout
```
pushpress-mcp/
  pyproject.toml
  README.md
  .gitignore            # state/, discovery/, *.db
  docs/endpoints.md     # produced in Phase 0
  scripts/discover.py
  scripts/summarize_har.py
  src/pushpress_mcp/
    __init__.py
    server.py           # FastMCP entrypoint
    auth.py
    client.py
    store.py
    tools.py
  tests/test_smoke.py
```

## 11. Build order
1. Scaffold project (`uv init`, deps, layout, `.gitignore`).
2. Phase 0 discovery. **Pause and show me `docs/endpoints.md`.**
3. `auth.py` plus `client.py`: authenticated call to the workouts endpoint, printed to terminal.
4. `store.py` plus parsers for workouts and scores.
5. `tools.py` and `server.py`: register tools, run over stdio.
6. Validation against the CSV (see section 12).
7. README with the Claude config snippet:
   ```json
   {"mcpServers": {"pushpress": {"command": "uv",
     "args": ["--directory", "/path/to/pushpress-mcp", "run", "pushpress-mcp"]}}}
   ```

## 12. Acceptance criteria
- [ ] `get_workouts` for the current week matches what the app shows.
- [ ] `sync_cache` over the full history completes without errors and stores a score count close to the CSV's row count (924 as of 2026-04-01, plus newer).
- [ ] Spot check: 10 random rows from the CSV match the cache on date, title and score text.
- [ ] Score count for the same period is within ~2% of the CSV. **List every mismatch**: missing, extra, or different score strings. (The PushPress export is known to be inaccurate, so differences may mean the API is the better source. Report which side looks right.)
- [ ] `get_leaderboard` returns ranked entries for a recent workout and matches the app.
- [ ] Expired session produces the clear re-login message, not a stack trace.
- [ ] No tokens in logs, repo or tool output.

## 13. Known risks
- **Unofficial API:** endpoints or auth can change without notice. Mitigated by keeping `raw_json`.
- **Terms of service:** personal, read-only, low-volume use only. Don't share or distribute.
- **Backend may not be PushPress-native:** programming and scores may come from a third party (e.g. SugarWOD). Phase 0 will tell us.
- **Leaderboard privacy:** other members' names and scores. Don't persist beyond the local cache and don't export.
- **Score formats:** times, reps, loads, rounds+reps and "scaled" variants need care. Store the raw score string and a parsed form separately where possible.

## 14. Stretch (after MVP works)
- Parse scores into structured numeric values (seconds, reps, lbs) to enable trend analysis.
- Tag workouts by movement/modality and flag Open-style repeats.
- Auto-sync daily via scheduled task.
- Feed into KiviWellness.

## 15. Kickoff prompt for Claude Code
> Read `pushpress-mcp-spec.md` and implement it in order. Start with scaffolding and Phase 0 discovery only. Stop after producing `docs/endpoints.md` and show me a summary before building auth, tools or the server. Don't guess endpoints, and don't print or commit any tokens or cookies.
>

