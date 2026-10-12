# PushPress member-app endpoints

Discovered in Phase 0 (2026-10-12) from a HAR capture of `members.pushpress.com`
(`scripts/discover.py` + `scripts/summarize_har.py --dump-samples`).
Implemented in [src/pushpress_mcp/endpoints.py](../src/pushpress_mcp/endpoints.py).
Unofficial: this can change without notice.

## Gate: passed

- [x] Workouts by date come back as JSON (`GetWorkoutTypes` + `GetWorkoutOfDay`)
- [x] My scores / history come back as JSON (`UserScoreHistory`)
- [x] A workout's leaderboard comes back as JSON (`GetWorkoutScores`)

Everything comes from PushPress's own GraphQL API (PushPress "Train"), not a third
party like SugarWOD.

## API host and auth

| | |
|---|---|
| Member app | `https://members.pushpress.com` (Flutter web app) |
| API | `POST https://api.pushpress.com/v2/graph/graphql`, JSON body `{operationName, query, variables}` |
| Auth | `Authorization: Bearer <JWT>`. No cookies or other custom headers needed. |
| Token source | `POST /v2/auth/login` returns `accessToken` / `refreshToken`. `auth.py` captures the bearer header from the app's own GraphQL requests instead of handling the login itself. |
| Token lifetime | 60 days (JWT `exp` − `iat`). Claims include `sub` (user id), `clientUuid` (gym), `role: member`. |
| Expired session | Expect HTTP 401/403 or a GraphQL `errors[].extensions.code` of `UNAUTHENTICATED`. Both map to the re-login message. The client also checks `exp` locally before calling. |

Other calls seen and **not** used: `/v2/auth/verify/{jwt}`, `/v2/client/client/{id}` (gym
settings and features), `/v2/social-feed/*`, Segment, Firebase and Google Analytics. Also
`GetProfiles`, `GetAttendanceStats`, `AppointmentHistory`, `GetUpcomingReservations`,
`BenchmarkTypes`, `GetBenchmarkWorkouts` and `GetWeightliftingWorkoutHistory`, which
are possible future tools.

## Workouts by date

No date-range query: one day at a time, in two steps.

1. **`GetWorkoutTypes($classDate: String!)`**: `getClassTypes(getClassTypesInput: {date})`
   returns the programs ("class types") with programming that day: `[{name, uid}]`,
   e.g. the main CrossFit program, HYROX, a monthly challenge.
2. **`GetWorkoutOfDay($classDate: String!, $classTypeId: String!)`**:
   `getWorkoutOfDay(getWorkoutOfDayInput: {date, classTypeUid})` returns a list of workouts:
   - workout: `uid` (= `workoutUid`), `id` / `classTypeId` (numeric, needed for leaderboards),
     `title`, `workoutState` (`PUBLISHED`), `publishingDate`
   - `parts[]`: `workoutPartUid`, `title` (e.g. "Topsy", or a date code like "260401"),
     `workoutTitle` (a section label like "Pre-workout strength"; often empty),
     `description`, `scoreType` (`"Reps"`, `"Time"`, …), `divisions`, `sets`
   - `parts[].score`: **my** score for that part, or null: `id`, `division`, `primaryScore`,
     `secondaryScore`, `athleteComment`, `sets[]`

| | |
|---|---|
| Date format | `YYYY-MM-DD` |
| Requests per day | 1 + number of programs that day (~4) |
| Pagination | none |

We store one `workouts` row per **part** (`id = workoutPartUid`), because scores and
leaderboards are per part.

## My scores / history

**`UserScoreHistory($keyword: String!)`** with `keyword: ""`:
`userScoreHistory(userScoreHistoryInput: {keyword})` returns **all** history in one call, newest
first. Fields: `workoutScoreId`, `workoutUid`, `workoutPartUid`, `workoutDate`
(`2026-10-12T00:00:00.000Z`), `title`, `workoutTitle`, `scoreType` (`FOR_TIME`, …),
`primaryScore`, `secondaryScore`, `measurementUnit`.

- **No pagination, no date filter.** We filter by date client-side.
- **No division, description or comment text.** These come from the workout's attached
  `parts[].score`. It has the same id as `workoutScoreId`, so the cache merges the two.
- **History starts 2025-05-01** for this account (647 scores as of 2026-10-12). Older scores
  only appear via `GetWorkoutOfDay`'s attached score, if at all; a full sync will show.

### Score rendering

Scores come back as two numbers. Rendering rules, checked against all 442 overlapping
rows of the CSV export:

| `scoreType` | Rendered | Example (primary, secondary) |
|---|---|---|
| `FOR_TIME` | `p:ss` (tenths kept) | (13, 1) → `13:01`; (8, 9.1) → `8:09.1` |
| `FOR_WEIGHT` | `p @ s` (reps @ load) | (1, 110) → `1 @ 110` |
| `FOR_ROUNDS_REPS` | `p + s` | (6, 21) → `6 + 21` |
| `FOR_REPS`, `FOR_CALORIES`, `FOR_DISTANCE` | `p` | (374, 0) → `374` |
| `FOR_COMPLETION` | `Complete` / `Incomplete` | (1, 0) → `Complete` |
| `FOR_COMMENT` | the athlete comment text | `35#, 55#, 75#` |

Workout parts use different names for the same types (`"Reps"`, `"Time"`); these are
normalized to `FOR_*`.

## Leaderboard for a workout part

**`GetWorkoutScores($workoutUid: String, $classTypeId: Float, $workoutPartUid: String!, $date: String!)`**:
`workoutGetScores(workoutGetScoresInput: {...}).scores[]`, in display order (best first). Fields:
`athleteDisplayName`, `division` (`Rx` / `Scaled`, or `Public` for challenges),
`primaryScore`, `secondaryScore`, `athleteComment`, `mine`.

- No explicit rank. We rank by position within each division.
- The app's query also pulls other members' photos, likes and comments. Ours doesn't.

## Notes and surprises

- **GraphQL aliases.** The app's queries alias fields (`workoutDateRaw: workoutDate`,
  `rawDate: date`, `rawUnit: unit`). Our trimmed queries use the plain names.
- **The CSV export repeats rows.** In 2025-05-01..2026-04-01 the export has 520 rows but
  only 442 distinct scores. One score can appear up to 14 times. The API's 442 matches the
  distinct count exactly.
- **The CSV export doesn't escape quotes** inside fields (e.g. `30" box jump`);
  `csv_format.read_pushpress_export` handles that.
- **The CSV's "Workout Title" is the part's `workoutTitle`** (our `section`), not its `title`.
- The login returned tokens for two `clientUuid`s. GraphQL calls use whichever token the
  app sends; that's the one `auth.py` captures.
