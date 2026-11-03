# Smart Match Allocation System
FastAPI + SQLite + single-page web UI (works on desktop, tablet, Android browser).

## Run
- Linux/Mac: `./run.sh`   Windows: `run.bat`
- Open http://localhost:8000 (other devices: http://<your-pc-ip>:8000)
- Admin password: env `ADMIN_PASSWORD` (default `admin123` — CHANGE IT). Data is in `smartmatch.db` (copy it to back up).
- Tests: `python -m pytest -q`

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

## Daily workflow
Pick date → Matches tab (paste lines; optional End date adds the same matches to every date from start to end, blank = start date only) → Schedule → Generate → fix manually → Lock all →
Close Day tab (enter completion times) → Close day. Next day's Generate uses that data.

## Not included
CSV/Excel import/export, reports/analytics, PostgreSQL migration, React/Alembic, Playwright tests.
