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
- Default daily load: 2 matches per person for every format (T20, ODI, Test). Auto-generate keeps to it and only goes
  beyond it if a slot would otherwise stay empty (warning lists who was reused).
- Admin override: manually adding a person past the default of 2 asks for confirmation ("Assign anyway?"); confirm to
  assign any number of matches in any format. Logged in the audit log as a limit override.
- Test availability: Ranji Tests occupy 4 calendar days and International Tests 5. The assigned people are
  unavailable for 4 or 5 recovery days, respectively, after the final Test day. Enter the actual completion
  date and time in Close Day when known; otherwise the schedule estimates the final day from the Test type.
  The rule is enforced for generated and manual assignments, including matches booked inside a future Test's
  duration or recovery window. Cancelled Tests don't block. Tag each Test Ranji or International (Matches tab,
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
