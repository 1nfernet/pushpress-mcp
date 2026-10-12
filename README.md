# PushPress MCP

A local, **read-only** MCP server that lets Claude read my PushPress member data:
programmed workouts (past and future), my logged scores, and per-workout
leaderboards. It calls the same GraphQL API the member web app
(`members.pushpress.com`) uses, authenticated with my own login session.

Unofficial and unsupported. Personal, low-volume use only. Never writes to PushPress.

> **Status:** Phase 0 discovery is done ([docs/endpoints.md](docs/endpoints.md)) and the
> endpoints are implemented. An offline check of real score history against the CSV export
> matches (442 vs 442 distinct scores). Not yet run against the live API: next steps are
> [log in](#2-log-in), [sync](#sync), [validate](#validate-against-the-csv-export).

See [docs/specs/pushpress_mcp_spec.md](docs/specs/pushpress_mcp_spec.md) for the spec and
[docs/README.md](docs/README.md) for implementation notes.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync
uv run playwright install chromium
```

## 1. Phase 0: discover the endpoints (done)

Rerun this if PushPress changes its API:

```bash
uv run python scripts/discover.py                     # log in, click through, press Enter
uv run python scripts/summarize_har.py --dump-samples
```

`discover.py` records the app's traffic to `discovery/session.har`. That file contains
live tokens: never share it. `summarize_har.py` prints one row per endpoint and per
GraphQL operation, with tokens redacted. It also writes scrubbed sample responses to
`discovery/samples/`.

## 2. Log in

```bash
uv run python -m pushpress_mcp.auth
```

Log in in the browser window (including any 2FA) and open the Workouts tab, then press
Enter. The bearer token the app uses is captured from its own requests and saved to
`state/session.json` (mode 600). Your password is never stored. The token lasts about
60 days; when it expires, tools tell you to run this again.

Smoke check: fetch this week's workouts and print them.

```bash
uv run python -m pushpress_mcp.client
```

## 3. Connect to Claude

Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{"mcpServers": {"pushpress": {"command": "uv",
  "args": ["--directory", "/path/to/pushpress-mcp", "run", "pushpress-mcp"]}}}
```

Claude Code:

```bash
claude mcp add pushpress -- uv --directory /path/to/pushpress-mcp run pushpress-mcp
```

Then ask things like *"what were my last 10 scored WODs?"* or *"what's programmed this week?"*

## Tools

| Tool | Args | Returns |
|---|---|---|
| `get_workouts` | `start_date`, `end_date` (YYYY-MM-DD) | One entry per workout part: `{date, program, section, title, description, type, id}` |
| `get_my_scores` | `start_date?`, `end_date?`, `limit?` (default 50) | `{date, workout_id, title, section, description, score, score_type, division, rx}`, newest first |
| `get_leaderboard` | `workout_id`, or `date` + optional `title` | `{rank, name, score, division, me?}`, ranked within each division |
| `sync_cache` | `days_back=365`, `days_forward=14` | Counts of scores and workouts pulled into the cache |
| `export_scores_csv` | `path` (must end in `.csv`) | Writes cached scores in PushPress's 5-column export format |

- `program` is the PushPress program (e.g. CrossFit, HYROX). `section` is the part's
  label (e.g. "Pre-workout strength"). Scores read like the app shows them: `13:01`,
  `1 @ 110`, `6 + 21`, `Complete`.
- The `get_*` tools fetch live data and save it to the local SQLite cache
  (`state/pushpress.db`). If the live call fails (expired session, network error, API
  change), they return cached data with `"source": "cache"` and a `warning` explaining why.
- Score history doesn't include Rx/Scaled or the workout description. Those are filled in
  from synced workouts, so run a sync to get them.

### Sync

Requests are sequential, at most one per second. Score history is a single request.
Workouts are one day at a time, about 4 requests per day (1 + one per program), so a
year of programming takes roughly 25 minutes. Run long syncs from the terminal rather
than through Claude:

```bash
uv run python -m pushpress_mcp.tools sync --days-back 365
```

Syncs can be resumed: already-synced days are skipped, except the last 3 days, where
scores may still be logged. If a sync is interrupted, run it again.

## Validate against the CSV export

After a sync covering the CSV's period, compare the cache to a PushPress score export:

```bash
uv run python scripts/validate_csv.py --csv ~/Downloads/cfd_score_export_pushpress_20260401.csv
```

The report:
- compares only the period both sides cover. API score history starts 2025-05-01, so
  older CSV rows are counted but not matched.
- runs the count check (target: within 2%) against *distinct* CSV rows, because the export
  repeats some scores verbatim.
- includes a spot check of 10 random rows.
- lists every mismatch: different score strings (with a "Looks right" column to fill in),
  rows missing from the cache, repeated CSV rows, and scores only in the cache.

It's saved to `state/validation_report.md`.

## Privacy and security

- Your password is never stored, only the API token (`state/session.json`, mode 600).
- `state/`, `discovery/`, `*.db`, `*.har` and `*.csv` are gitignored.
- Tokens, headers, query strings and request bodies never appear in logs, errors or tool
  output. `summarize_har.py` redacts JWTs and scrubs token fields from the samples it writes.
- Only GraphQL queries are sent; the client refuses mutations.
- Other members' leaderboard entries (display name, score, division only) are kept only in
  the local cache and are never exported.

## Development

```bash
uv run pytest
```

The tests run offline: a fake GraphQL server, built from the shapes captured in Phase 0,
stands in for PushPress.

```
src/pushpress_mcp/
  server.py      MCP entrypoint (stdio)
  tools.py       tool logic, live-then-cache fallback; `sync` CLI
  endpoints.py   PushPress GraphQL queries, parsers, score formatting
  client.py      httpx client: bearer auth, rate limiting, retries
  auth.py        Playwright login -> state/session.json
  store.py       SQLite cache (merges scores from history + workouts)
  csv_format.py  PushPress CSV export: tolerant reader and writer
  models.py, errors.py
scripts/
  discover.py, summarize_har.py   Phase 0
  validate_csv.py                 acceptance checks against the CSV export
```
