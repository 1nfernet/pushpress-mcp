# Docs

- [specs/pushpress_mcp_spec.md](specs/pushpress_mcp_spec.md): the MVP spec (goal, tools, data model, acceptance criteria).
- [endpoints.md](endpoints.md): the member-app GraphQL API as discovered in Phase 0.

## Implementation notes (vs. the spec)

- **MCP SDK v2.** The `mcp` package is on 2.x, where FastMCP is now `MCPServer`
  (`mcp.server.mcpserver`). Same decorator model. Bad arguments are raised as `ToolError`
  so the message reaches Claude; other exceptions are masked by the SDK.
- **`endpoints.py`.** Everything specific to PushPress's unofficial API (GraphQL queries,
  parsing, score formatting, auth headers) is isolated in `src/pushpress_mcp/endpoints.py`.
- **Auth is a bearer JWT, not cookies.** `auth.py` captures the `Authorization` header from
  the app's own GraphQL requests during a headed login. The token lasts 60 days, and the
  client checks its `exp` before each session.
- **Workouts are per part.** A PushPress workout has parts (strength, metcon, …), and scores
  and leaderboards attach to parts, so a `workouts` row is one part (`id = workoutPartUid`).
  Two columns were added to the spec's tables: `program` (e.g. CrossFit, HYROX) and
  `section` (the part's `workoutTitle`, e.g. "Pre-workout strength"; the CSV export uses
  it as "Workout Title").
- **Scores from two sources, merged.** Score history (one request, all of it) lacks
  division, description and comment text. The workout-of-day response includes my score for
  each part, with those fields and the same id. `store.upsert_scores` merges the two sources
  (non-empty values win; `raw_json` from both is kept).
- **Extra columns.** `scores` also stores `title`, `description`, `section` and `rx`;
  `leaderboard_entries` stores `is_me`. A `synced_days` table makes long syncs resumable.
- **Score text is rendered from numbers** (`primaryScore`, `secondaryScore`) using rules
  checked against the CSV (see endpoints.md).
- **CSV ground truth.** The export has 920 rows, not 924. It doesn't escape quote
  characters inside fields, which breaks standard CSV parsers; `read_pushpress_export`
  handles that. It also repeats some rows verbatim (78 extra copies between 2025-05-01 and
  2026-04-01), so the validator compares counts against distinct rows.
- **API history starts 2025-05-01**, so the 400 CSV rows before that can only be
  checked if a full workout sync finds scores attached to those older workouts.
- **Expired sessions.** HTTP 401/403, a GraphQL `UNAUTHENTICATED` error, an HTML response,
  a missing token, or an expired `exp` all produce the spec's "Session expired" message.
- **Rate limit.** Requests are sequential with at least 1s between them, plus up to 3
  attempts with backoff on 429/5xx/network errors.
