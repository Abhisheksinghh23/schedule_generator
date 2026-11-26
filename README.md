# Smart Match Allocation System
FastAPI + SQLite/PostgreSQL + single-page web UI (works on desktop, tablet, Android browser).

## Run locally
- Linux/Mac: `./run.sh`   Windows: `run.bat`
- Open http://localhost:8000 (other devices: http://<your-pc-ip>:8000)
- Admin password: env `ADMIN_PASSWORD` (default `admin123` — CHANGE IT). Data is in `smartmatch.db` (copy it to back up).
- Tests: `python -m pytest -q`

## Security
- Every `/api/*` route requires the admin password (constant-time compare). The static page is public but exposes no data.
- Failed logins are rate-limited in the app: 8 failures in 10 minutes locks that client for 15 minutes
  (env-tunable: `MAX_AUTH_FAILS`, `AUTH_WINDOW_SEC`, `AUTH_BLOCK_SEC`).
  Client identity uses `CF-Connecting-IP` when present (Render routes all traffic through Cloudflare, which
  overwrites this header, so spoofed `X-Forwarded-For` cannot rotate identity), otherwise the right-most
  `X-Forwarded-For` entry; a separate per-connection-peer bucket (default 40, `PEER_AUTH_FAILS`) backstops
  header rotation on direct deployments. The limiter is in-process: it resets on restart and assumes the
  single worker this service runs with.
- Deployed startup fails closed: with `DATABASE_URL`/`RENDER`/`VERCEL` set, the app refuses to boot unless
  `ADMIN_PASSWORD` is set, at least 12 characters and not the default. Local development still defaults to `admin123`.
- Keep the generated admin password in a password manager; it is never written to the repo or logs.

## Database
- Local/default: SQLite at `DB_PATH` (default `./smartmatch.db`; `/tmp/smartmatch.db` on Render/Vercel when no DB is configured).
- Production: set `DATABASE_URL` to a PostgreSQL connection string (e.g. Neon free tier). The schema is created and
  migrated automatically on startup. SQLite and Postgres are both covered by the test suite.
- Day start is env `DAY_START` (default `09:00`); set `TZ=Asia/Kolkata` on the host so log timestamps are IST.

## Rules implemented
- Declaration 2 people / Fancy 5 people per match; departments fully isolated.
- Automatic generation: each person's day is either 2 T20s, or 1 ODI, or 1 Test. Formats are never mixed, no one
  exceeds the maximum, and Test days are blocked. If not enough eligible people remain, the slot is left unfilled
  and reported.
- Admin manual edits: an admin can assign anyone in the right department, active, to any match. Breaking a scheduling
  rule (daily format limit, mixing formats, being booked on a Test through its final day, leave, a Test overlapping
  other matches) shows the reasons and asks "Assign anyway?"; confirming allows it and logs a limit override.
  This is how an admin makes it two ODIs or two Tests. Hard stops that cannot be overridden: a person already on
  2 matches that day (ceiling), a locked assignment, a duplicate, a full team, an inactive person, a wrong department.
  Auto-generate never uses these exceptions. Authentication is unchanged: all of this requires the admin login.
- Test availability: Ranji Tests occupy 4 calendar days and International Tests 5, with no extra recovery days.
  The assigned people are unavailable through the final Test day and free the day after. Enter the actual completion
  date and time in Close Day when known; otherwise the schedule estimates the final day from the Test type.
  The rule is enforced for generated and manual assignments, including matches booked inside a future Test's
  duration. Cancelled Tests don't block. Tag each Test Ranji or International (Matches tab,
  or per line: `MATCH 103, TEST, RANJI`).
- Match length: T20 4h and ODI 8h. Matches run back to back from the day start
  (env `DAY_START`, default 09:00). Release time = end of last match; actual completion
  times entered in Close Day override the estimate; cancelled matches are ignored.
- Next day priority: earliest previous-day release first. Ties: fewer assignments today,
  fewer in last 7 days, longest since last assignment, ID order. People with no history rank after those with history.
- Admin-only transfers with confirmation, warnings for upcoming assignments, history kept.
- Lock/unlock, manual add/replace/remove (validated), Close Day / Reopen (logged), audit log.
- Leave dates per person: excluded from generation on those dates.
- Custom required team size per match (default: department size).
- Workload report per date range (per person: totals by format, days worked, last assignment) with CSV export.

## Daily workflow
Pick date → Matches tab (paste lines; optional End date adds the same matches to every date from start to end, blank = start date only) → Schedule → Generate → fix manually → Lock all →
Close Day tab (enter completion times) → Close day. Next day's Generate uses that data.

## Deploy (free tier)
- Web service: Render free tier, build `pip install -r requirements.txt`, start `uvicorn app:app --host 0.0.0.0 --port $PORT`.
- Env vars: `ADMIN_PASSWORD` (strong), `DATABASE_URL` (Neon Postgres), `TZ=Asia/Kolkata`, optional `DAY_START`.

## Not included
Excel import, React/Alembic, Playwright tests.
