"""Smart Match Allocation System - FastAPI + SQLite backend."""
import hmac, json, os, sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Optional
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import engine as E

def database_path():
    return os.environ.get("DB_PATH") or ("/tmp/smartmatch.db" if os.environ.get("VERCEL") else "smartmatch.db")


DB = database_path()
PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
SH, SM = map(int, os.environ.get("DAY_START", "09:00").split(":"))
HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Smart Match Allocation")
DEPT = "CHECK(dept IN('DECLARATION','FANCY'))"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS people(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
 dept TEXT NOT NULL {DEPT}, active INTEGER NOT NULL DEFAULT 1, notes TEXT DEFAULT '', added TEXT NOT NULL, leave TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS matches(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, day TEXT NOT NULL,
 dept TEXT NOT NULL {DEPT}, fmt TEXT NOT NULL CHECK(fmt IN('T20','ODI','TEST')), ord INTEGER NOT NULL, start TEXT, comp TEXT,
 status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN('scheduled','completed','cancelled')), done TEXT, need INTEGER NOT NULL DEFAULT 0,
 UNIQUE(code,day,dept));
CREATE TABLE IF NOT EXISTS assign(match_id INTEGER NOT NULL REFERENCES matches(id), person_id TEXT NOT NULL REFERENCES people(id),
 locked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(match_id,person_id));
CREATE TABLE IF NOT EXISTS transfers(id INTEGER PRIMARY KEY AUTOINCREMENT, person_id TEXT, from_dept TEXT, to_dept TEXT, admin TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS closures(day TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS id_sequences(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_m ON matches(day,dept);
CREATE INDEX IF NOT EXISTS ix_a ON assign(person_id);
"""

@contextmanager
def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()

with db() as _c:
    _c.executescript(SCHEMA)
    cols = [r[1] for r in _c.execute("PRAGMA table_info(matches)")]
    if "comp" not in cols:
        _c.execute("ALTER TABLE matches ADD COLUMN comp TEXT")      # upgrade older databases
    if "need" not in cols:
        _c.execute("ALTER TABLE matches ADD COLUMN need INTEGER NOT NULL DEFAULT 0")
    pcols = [r[1] for r in _c.execute("PRAGMA table_info(people)")]
    if "leave" not in pcols:
        _c.execute("ALTER TABLE people ADD COLUMN leave TEXT NOT NULL DEFAULT ''")
    max_person_id = _c.execute("SELECT COALESCE(MAX(CAST(SUBSTR(id,2) AS INTEGER)),0) FROM people").fetchone()[0]
    _c.execute("INSERT OR IGNORE INTO id_sequences(name,value) VALUES('person',?)", (max_person_id,))

def admin(x_admin_password: str = Header("")):
    if not hmac.compare_digest(x_admin_password.encode(), PASSWORD.encode()):
        raise HTTPException(401, "Admin login required")

def now(): return datetime.now().isoformat(timespec="seconds")
def log(c, a, d=""): c.execute("INSERT INTO audit(at,action,detail) VALUES(?,?,?)", (now(), a, d))
def pday(s):
    try: return date.fromisoformat(s)
    except ValueError: raise HTTPException(400, "Invalid date (use YYYY-MM-DD)")
def start_of(d): return datetime(d.year, d.month, d.day, SH, SM)
def dt(s): return datetime.fromisoformat(s) if s else None
def parse_leave(raw): return [x for x in (raw or "").split(",") if x and x.strip()]
def leave_dates_for_person(row): return parse_leave(row.get("leave") if isinstance(row, dict) else row["leave"])
def snapshot_state(c):
    return json.dumps({
        "people": [dict(r) for r in c.execute("SELECT * FROM people ORDER BY id")],
        "matches": [dict(r) for r in c.execute("SELECT * FROM matches ORDER BY id")],
        "assign": [dict(r) for r in c.execute("SELECT * FROM assign ORDER BY match_id,person_id")],
        "transfers": [dict(r) for r in c.execute("SELECT * FROM transfers ORDER BY id")],
        "audit": [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id")],
        "closures": [dict(r) for r in c.execute("SELECT * FROM closures ORDER BY day")],
    })
def restore_snapshot(c, payload):
    c.execute("DELETE FROM assign")
    c.execute("DELETE FROM matches")
    c.execute("DELETE FROM closures")
    c.execute("DELETE FROM transfers")
    c.execute("DELETE FROM audit")
    c.execute("DELETE FROM people")
    for row in payload.get("people", []):
        c.execute("INSERT INTO people(id,name,dept,active,notes,added,leave) VALUES(?,?,?,?,?,?,?)",
                  (row["id"], row["name"], row["dept"], row["active"], row["notes"], row["added"], row.get("leave", "")))
    for row in payload.get("matches", []):
        c.execute("INSERT INTO matches(id,code,day,dept,fmt,ord,start,comp,status,done,need) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (row["id"], row["code"], row["day"], row["dept"], row["fmt"], row["ord"], row.get("start"), row.get("comp"), row["status"], row.get("done"), row.get("need", 0)))
    for row in payload.get("assign", []):
        c.execute("INSERT INTO assign(match_id,person_id,locked) VALUES(?,?,?)", (row["match_id"], row["person_id"], row["locked"]))
    for row in payload.get("transfers", []):
        c.execute("INSERT INTO transfers(id,person_id,from_dept,to_dept,admin,at) VALUES(?,?,?,?,?,?)",
                  (row["id"], row.get("person_id"), row.get("from_dept"), row.get("to_dept"), row.get("admin"), row.get("at")))
    for row in payload.get("audit", []):
        c.execute("INSERT INTO audit(id,at,action,detail) VALUES(?,?,?,?)", (row["id"], row["at"], row["action"], row["detail"]))
    for row in payload.get("closures", []):
        c.execute("INSERT INTO closures(day,at) VALUES(?,?)", (row["day"], row["at"]))
def closed(c, day): return c.execute("SELECT 1 FROM closures WHERE day=?", (day,)).fetchone() is not None
def not_closed(c, day):
    if closed(c, day): raise HTTPException(409, "Day is closed. Reopen it (logged correction) to change it.")

def load_day(c, day):
    ms, asg, lk = {}, {}, {}
    for r in c.execute("SELECT * FROM matches WHERE day=? ORDER BY ord,id", (day,)):
        k = str(r["id"])
        ms[k] = E.Match(k, r["dept"], r["fmt"], r["ord"], r["status"], dt(r["done"]), dt(r["start"]),
                         r["comp"], date.fromisoformat(day), r["need"] or E.SLOTS[r["dept"]])
        asg[k] = []
    for r in c.execute("SELECT a.* FROM assign a JOIN matches m ON m.id=a.match_id WHERE m.day=? ORDER BY a.rowid", (day,)):
        asg[str(r["match_id"])].append(r["person_id"])
        if r["locked"]: lk.setdefault(str(r["match_id"]), []).append(r["person_id"])
    return ms, asg, lk

def prev_releases(c, day, dept):
    pd = c.execute("SELECT MAX(m.day) FROM matches m JOIN assign a ON a.match_id=m.id WHERE m.day<? AND m.dept=?",
                   (day, dept)).fetchone()[0]
    if not pd: return None, {}
    ms, asg, _ = load_day(c, pd)
    ss = start_of(date.fromisoformat(pd))
    return pd, {p: E.release_for(p, asg, ms, ss) for p in {p for v in asg.values() for p in v}}

def rest_info(c, pid, day):
    """(last blocked day after an assigned Test, dates of overlapping booked matches)."""
    ru = None
    for r in c.execute("SELECT m.day, m.comp, m.done FROM assign a JOIN matches m ON m.id=a.match_id WHERE a.person_id=? "
                       "AND m.fmt='TEST' AND m.status!='cancelled' AND m.day<=?", (pid, day)):
        done = datetime.fromisoformat(r["done"]) if r["done"] else None
        e = E.rest_until_after_test(date.fromisoformat(r["day"]), r["comp"], done)
        ru = max(ru, e) if ru else e
    max_window = max(E.TEST_DAYS[c] - 1 + E.REST_DAYS[c] for c in E.TEST_DAYS)
    hi = (date.fromisoformat(day) + timedelta(days=max_window)).isoformat()
    fut = [date.fromisoformat(r[0]) for r in c.execute(
        "SELECT DISTINCT m.day FROM assign a JOIN matches m ON m.id=a.match_id WHERE a.person_id=? "
        "AND m.status!='cancelled' AND m.day>=? AND m.day<=?", (pid, day, hi))]
    return ru, fut

def people_objs(c, day, dept, rel):
    lo = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
    out = []
    for p in c.execute("SELECT * FROM people WHERE dept=?", (dept,)).fetchall():
        if day in parse_leave(p["leave"]):
            continue
        q = "SELECT %s FROM assign a JOIN matches m ON m.id=a.match_id WHERE a.person_id=? AND m.day<?"
        n = c.execute(q % "COUNT(*)" + " AND m.day>=?", (p["id"], day, lo)).fetchone()[0]
        la = c.execute(q % "MAX(m.day)", (p["id"], day)).fetchone()[0]
        ru, fut = rest_info(c, p["id"], day)
        out.append(E.Person(p["id"], p["name"], dept, bool(p["active"]), rel.get(p["id"]),
                            date.fromisoformat(la) if la else None, n, ru, fut))
    return out


def snapshot_mutation(c):
    c.execute("INSERT INTO snapshots(at,payload) VALUES(?,?)", (now(), snapshot_state(c)))
    c.execute("DELETE FROM snapshots WHERE id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT 20)")

def add_person(c, name, dept, notes=""):
    name = name.strip()
    if not name or dept not in E.SLOTS: raise HTTPException(400, "Name and a valid department are required")
    if c.execute("SELECT 1 FROM people WHERE name=?", (name,)).fetchone():
        raise HTTPException(409, f"Duplicate person: {name}")
    next_id = c.execute("SELECT value+1 FROM id_sequences WHERE name='person'").fetchone()[0]
    c.execute("UPDATE id_sequences SET value=? WHERE name='person'", (next_id,))
    pid = "P%04d" % next_id
    c.execute("INSERT INTO people(id,name,dept,active,notes,added,leave) VALUES(?,?,?,?,?,?,?)",
              (pid, name, dept, 1, notes, now(), ""))
    log(c, "person_added", f"{pid} {name} {dept}")
    return pid

class NewPerson(BaseModel): name: str; dept: str; notes: str = ""
class BulkP(BaseModel): text: str; dept: str = "DECLARATION"
class BulkM(BaseModel): day: str; end_day: Optional[str] = None; dept: str; fmt: str = "T20"; text: str; comp: str = "INTERNATIONAL"; need: Optional[int] = None
class Assign(BaseModel): match_id: int; person_id: str; replace_person_id: Optional[str] = None; override: bool = False
class Un(BaseModel): match_id: int; person_id: str
class Transfer(BaseModel): person_ids: list[str]; new_dept: str; confirm: bool = False
class Done(BaseModel): id: int; status: str; done: Optional[str] = None
class Close(BaseModel): matches: list[Done]

@app.get("/")
def index(): return FileResponse(os.path.join(HERE, "static", "index.html"))

@app.get("/api/people")
def people(_=Depends(admin)):
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT p.*,(SELECT COUNT(*) FROM assign a WHERE a.person_id=p.id) total,"
            "(SELECT MAX(m.day) FROM assign a JOIN matches m ON m.id=a.match_id WHERE a.person_id=p.id) last "
            "FROM people p ORDER BY p.dept,p.name")]

@app.post("/api/people")
def new_person(p: NewPerson, _=Depends(admin)):
    with db() as c:
        snapshot_mutation(c)
        return {"id": add_person(c, p.name, p.dept.upper(), p.notes)}


@app.post("/api/people/{pid}/leave")
def set_leave(pid: str, body: dict, _=Depends(admin)):
    date_str = body.get("date") or body.get("dates")
    if isinstance(date_str, list):
        dates = [str(d).strip() for d in date_str if str(d).strip()]
    elif date_str:
        dates = [str(date_str).strip()]
    else:
        raise HTTPException(400, "A leave date is required")
    for leave_day in dates:
        pday(leave_day)
    with db() as c:
        person = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not person: raise HTTPException(404, "Person not found")
        existing = set(parse_leave(person["leave"]))
        existing.update(d for d in dates if d)
        leave = ",".join(sorted(existing))
        snapshot_mutation(c)
        c.execute("UPDATE people SET leave=? WHERE id=?", (leave, pid))
        log(c, "person_leave_updated", f"{pid} leave={leave}")
        return {"ok": True, "leave": leave.split(",") if leave else []}


@app.delete("/api/people/{pid}/leave")
def remove_leave(pid: str, day: str, _=Depends(admin)):
    pday(day)
    with db() as c:
        person = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not person: raise HTTPException(404, "Person not found")
        existing = parse_leave(person["leave"])
        if day not in existing: raise HTTPException(404, "Leave date not found")
        snapshot_mutation(c)
        leave = ",".join(d for d in existing if d != day)
        c.execute("UPDATE people SET leave=? WHERE id=?", (leave, pid))
        log(c, "person_leave_removed", f"{pid} leave={day}")
        return {"ok": True, "leave": leave.split(",") if leave else []}


@app.delete("/api/people/{pid}")
def delete_person(pid: str, _=Depends(admin)):
    with db() as c:
        person = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not person: raise HTTPException(404, "Person not found")
        snapshot_mutation(c)
        c.execute("DELETE FROM assign WHERE person_id=?", (pid,))
        c.execute("DELETE FROM people WHERE id=?", (pid,))
        log(c, "person_deleted", f"{pid} {person['name']}")
        return {"ok": True}


@app.get("/api/preview/{day}")
def preview(day: str, _=Depends(admin)):
    pday(day); warns, unfilled = [], []
    with db() as c:
        ms, _a, lk = load_day(c, day)
        code = {r["id"]: r["code"] for r in c.execute("SELECT id,code FROM matches WHERE day=?", (day,))}
        for dept in E.SLOTS:
            dm = [m for m in ms.values() if m.dept == dept]
            if not dm: continue
            pd, rel = prev_releases(c, day, dept)
            r = E.allocate(dm, people_objs(c, day, dept, rel), lk)
            warns += [f"{dept}: {w}" for w in r.warnings if pd]
            warns += [f"{dept}: {code[int(mid)]} used {', '.join(n)} beyond the usual daily format load (not enough free staff)."
                      for mid, n in r.overflow]
            for mid, miss, why in r.unfilled:
                unfilled.append({"code": code[int(mid)], "dept": dept, "missing": miss, "reasons": why})
        return {"warnings": warns, "unfilled": unfilled}


@app.post("/api/undo")
def undo(_=Depends(admin)):
    with db() as c:
        snap = c.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if not snap: raise HTTPException(404, "No earlier state is available to undo")
        payload = json.loads(snap["payload"])
        restore_snapshot(c, payload)
        c.execute("DELETE FROM snapshots WHERE id=?", (snap["id"],))
        log(c, "undo", f"restored snapshot {snap['id']}")
        return {"ok": True}

@app.post("/api/people/bulk")
def bulk_people(b: BulkP, _=Depends(admin)):
    added, warns = 0, []
    with db() as c:
        snapshot_mutation(c)
        for line in filter(str.strip, b.text.splitlines()):
            name, _s, d = line.partition(",")
            try: add_person(c, name, (d.strip() or b.dept).upper()); added += 1
            except HTTPException as e: warns.append(f"{line.strip()}: {e.detail}")
    return {"added": added, "warnings": warns}

@app.post("/api/people/{pid}/active")
def set_active(pid: str, active: bool, _=Depends(admin)):
    with db() as c:
        snapshot_mutation(c)
        if not c.execute("UPDATE people SET active=? WHERE id=?", (int(active), pid)).rowcount:
            raise HTTPException(404, "Person not found")
        log(c, "person_active", f"{pid} -> {active}")
    return {"ok": True}

@app.post("/api/transfer")
def transfer(t: Transfer, _=Depends(admin)):
    new = t.new_dept.upper()
    if new not in E.SLOTS: raise HTTPException(400, "Invalid department")
    warns, objs = [], []
    with db() as c:
        snapshot_mutation(c)
        today = date.today().isoformat()
        for pid in t.person_ids:
            r = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
            if not r: raise HTTPException(404, f"Unknown person {pid}")
            if r["dept"] == new: raise HTTPException(400, f"{r['name']} is already in {new}")
            up = c.execute("SELECT COUNT(*) FROM assign a JOIN matches m ON m.id=a.match_id "
                           "WHERE a.person_id=? AND m.day>=? AND m.dept=?", (pid, today, r["dept"])).fetchone()[0]
            if up: warns.append(f"{r['name']} has {up} upcoming {r['dept']} assignment(s); they stay unchanged.")
            objs.append(E.Person(pid, r["name"], r["dept"]))
        if not t.confirm: return {"needs_confirm": True, "warnings": warns}
        hist = []
        for p in objs:
            old = p.dept
            E.transfer(p, new, True, "admin", hist)
            c.execute("UPDATE people SET dept=? WHERE id=?", (new, p.id))
            c.execute("INSERT INTO transfers(person_id,from_dept,to_dept,admin,at) VALUES(?,?,?,?,?)",
                      (p.id, old, new, "admin", now()))
            log(c, "transfer", f"{p.id} {old}->{new}")
    return {"needs_confirm": False, "warnings": warns, "moved": len(objs)}

MAX_RANGE_DAYS = 366

@app.post("/api/matches/bulk")
def bulk_matches(b: BulkM, _=Depends(admin)):
    d0 = pday(b.day)
    d1 = pday(b.end_day) if b.end_day and b.end_day.strip() else d0      # no end date -> start date
    if d1 < d0: raise HTTPException(400, "End date cannot be before the start date")
    if (d1 - d0).days >= MAX_RANGE_DAYS: raise HTTPException(400, f"Date range too long (max {MAX_RANGE_DAYS} days)")
    dept, fmt = b.dept.upper(), b.fmt.upper()
    if dept not in E.SLOTS or fmt not in E.LIMITS: raise HTTPException(400, "Invalid department or format")
    days = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
    added, warns = 0, []
    with db() as c:
        snapshot_mutation(c)
        if len(days) == 1: not_closed(c, days[0])
        for day in days:
            if closed(c, day):
                warns.append(f"{day}: day is closed, skipped"); continue
            o = c.execute("SELECT COALESCE(MAX(ord),0) FROM matches WHERE day=?", (day,)).fetchone()[0]
            for line in filter(str.strip, b.text.splitlines()):
                parts = [x.strip() for x in line.split(",")]
                code = parts[0]
                f = parts[1].upper() if len(parts) > 1 and parts[1] else fmt
                comp = (parts[2].upper() if len(parts) > 2 and parts[2] else b.comp.upper()) if f == "TEST" else None
                if f not in E.LIMITS or (comp and comp not in E.REST_DAYS):
                    if day == days[0]: warns.append(f"{line.strip()}: invalid format or Test type (RANJI / INTERNATIONAL)")
                    continue
                try:
                    o += 1
                    req = max(1, int(b.need or 0)) if b.need is not None else 0
                    c.execute("INSERT INTO matches(code,day,dept,fmt,comp,ord,need) VALUES(?,?,?,?,?,?,?)",
                              (code, day, dept, f, comp, o, req if req > 0 else 0))
                    added += 1
                except sqlite3.IntegrityError:
                    o -= 1; warns.append(f"{code} ({day}): duplicate for this date/department, skipped")
        log(c, "matches_added", f"{days[0]}" + (f"..{days[-1]}" if len(days) > 1 else "") + f" {dept} +{added}")
    return {"added": added, "days": len(days), "warnings": warns}

@app.delete("/api/matches/{mid}")
def del_match(mid: int, _=Depends(admin)):
    with db() as c:
        m = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        if not m: raise HTTPException(404, "Match not found")
        not_closed(c, m["day"])
        if c.execute("SELECT 1 FROM assign WHERE match_id=? AND locked=1", (mid,)).fetchone():
            raise HTTPException(409, "Match has locked assignments; unlock first")
        snapshot_mutation(c)
        c.execute("DELETE FROM assign WHERE match_id=?", (mid,)); c.execute("DELETE FROM matches WHERE id=?", (mid,))
        log(c, "match_deleted", f"{m['code']} {m['day']}")
    return {"ok": True}

@app.post("/api/generate/{day}")
def generate(day: str, _=Depends(admin)):
    pday(day); warns, unfilled = [], []
    with db() as c:
        c.execute("BEGIN IMMEDIATE")          # serialises concurrent generation
        snapshot_mutation(c)
        not_closed(c, day)
        ms, _a, lk = load_day(c, day)
        code = {r["id"]: r["code"] for r in c.execute("SELECT id,code FROM matches WHERE day=?", (day,))}
        c.execute("DELETE FROM assign WHERE locked=0 AND match_id IN (SELECT id FROM matches WHERE day=?)", (day,))
        for dept in E.SLOTS:
            dm = [m for m in ms.values() if m.dept == dept]
            if not dm: continue
            pd, rel = prev_releases(c, day, dept)
            yest = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            if pd and pd != yest: warns.append(f"{dept}: no assignments yesterday; used release times from {pd}.")
            if not pd: warns.append(f"{dept}: no previous-day data; fairness tie-breakers used.")
            r = E.allocate(dm, people_objs(c, day, dept, rel), lk)
            warns += [f"{dept}: {w}" for w in r.warnings if pd]
            warns += [f"{dept}: {code[int(mid)]} used {', '.join(n)} beyond the usual daily format load (not enough free staff)."
                      for mid, n in r.overflow]
            for mid, ps in r.assignments.items():
                for p in ps:
                    if p not in lk.get(mid, []):
                        c.execute("INSERT INTO assign(match_id,person_id) VALUES(?,?)", (int(mid), p))
            for mid, miss, why in r.unfilled:
                unfilled.append({"code": code[int(mid)], "dept": dept, "missing": miss, "reasons": why})
        log(c, "generate", f"{day} unfilled={len(unfilled)}")
    return {"warnings": warns, "unfilled": unfilled}

@app.get("/api/schedule/{day}")
def schedule(day: str, _=Depends(admin)):
    pday(day)
    with db() as c:
        _m, asg, lk = load_day(c, day)
        names = {r["id"]: r["name"] for r in c.execute("SELECT id,name FROM people")}
        rows = [dict(r) for r in c.execute("SELECT * FROM matches WHERE day=? ORDER BY dept,ord,id", (day,))]
        for r in rows:
            r["need"] = r["need"] or E.SLOTS[r["dept"]]
            r["people"] = [{"id": p, "name": names[p], "locked": p in lk.get(str(r["id"]), [])}
                           for p in asg[str(r["id"])]]
        return {"day": day, "closed": closed(c, day), "matches": rows}

@app.post("/api/matches/{mid}/need")
def set_match_need(mid: int, body: dict, _=Depends(admin)):
    need = int(body.get("need", 0))
    if need < 1:
        raise HTTPException(400, "Required slots must be at least 1")
    with db() as c:
        m = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        if not m: raise HTTPException(404, "Match not found")
        not_closed(c, m["day"])
        snapshot_mutation(c)
        c.execute("UPDATE matches SET need=? WHERE id=?", (need, mid))
        log(c, "match_need_updated", f"{m['code']} {m['day']} need={need}")
    return {"ok": True, "need": need}

@app.post("/api/assign")
def assign(a: Assign, _=Depends(admin)):
    with db() as c:
        m = c.execute("SELECT * FROM matches WHERE id=?", (a.match_id,)).fetchone()
        pr = c.execute("SELECT * FROM people WHERE id=?", (a.person_id,)).fetchone()
        if not m or not pr: raise HTTPException(404, "Match or person not found")
        not_closed(c, m["day"])
        snapshot_mutation(c)
        ms, asg, lk = load_day(c, m["day"]); k = str(a.match_id)
        if a.replace_person_id:
            if a.replace_person_id in lk.get(k, []): raise HTTPException(409, "That assignment is locked")
            asg[k] = [p for p in asg[k] if p != a.replace_person_id]
        try:
            ru, fut = rest_info(c, pr["id"], m["day"])
            E.validate_manual(E.Person(pr["id"], pr["name"], pr["dept"], bool(pr["active"]),
                                       rest_until=ru, future=fut), ms[k], asg, ms, override=a.override)
        except E.LimitReached as e:
            raise HTTPException(409, f"OVERRIDE: {e}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        if a.replace_person_id:
            c.execute("DELETE FROM assign WHERE match_id=? AND person_id=?", (a.match_id, a.replace_person_id))
        c.execute("INSERT INTO assign(match_id,person_id) VALUES(?,?)", (a.match_id, a.person_id))
        log(c, "manual_assign", f"{m['code']} {m['day']} +{a.person_id} -{a.replace_person_id}" + (" (limit override)" if a.override else ""))
    return {"ok": True}

@app.post("/api/unassign")
def unassign(u: Un, _=Depends(admin)):
    with db() as c:
        m = c.execute("SELECT * FROM matches WHERE id=?", (u.match_id,)).fetchone()
        if not m: raise HTTPException(404, "Match not found")
        not_closed(c, m["day"])
        snapshot_mutation(c)
        r = c.execute("SELECT locked FROM assign WHERE match_id=? AND person_id=?", (u.match_id, u.person_id)).fetchone()
        if not r: raise HTTPException(404, "Assignment not found")
        if r["locked"]: raise HTTPException(409, "That assignment is locked")
        c.execute("DELETE FROM assign WHERE match_id=? AND person_id=?", (u.match_id, u.person_id))
        log(c, "manual_unassign", f"{m['code']} {m['day']} -{u.person_id}")
    return {"ok": True}

@app.post("/api/lock/{day}")
def lock(day: str, locked: bool = True, _=Depends(admin)):
    pday(day)
    with db() as c:
        snapshot_mutation(c)
        c.execute("UPDATE assign SET locked=? WHERE match_id IN (SELECT id FROM matches WHERE day=?)", (int(locked), day))
        log(c, "lock" if locked else "unlock", day)
    return {"ok": True}

@app.post("/api/close/{day}")
def close(day: str, body: Close, _=Depends(admin)):
    d = pday(day); missing = []
    with db() as c:
        snapshot_mutation(c)
        not_closed(c, day)
        for x in body.matches:
            if x.status not in ("scheduled", "completed", "cancelled"): raise HTTPException(400, "Invalid status")
            done = None
            if x.done:
                try: done = (datetime.combine(d, time.fromisoformat(x.done)) if len(x.done) <= 5
                             else datetime.fromisoformat(x.done)).isoformat()
                except ValueError: raise HTTPException(400, f"Invalid completion time: {x.done}")
            c.execute("UPDATE matches SET status=?, done=? WHERE id=? AND day=?", (x.status, done, x.id, day))
            if x.status == "completed" and not done:
                missing.append(c.execute("SELECT code FROM matches WHERE id=?", (x.id,)).fetchone()[0])
        ms, asg, _l = load_day(c, day)
        names = {r["id"]: r["name"] for r in c.execute("SELECT id,name FROM people")}
        rel = {}
        for p in {p for v in asg.values() for p in v}:
            r = E.release_for(p, asg, ms, start_of(d))
            if r: rel[names[p]] = r.isoformat(timespec="minutes")
        c.execute("INSERT INTO closures VALUES(?,?)", (day, now()))
        log(c, "close_day", day)
    return {"releases": rel, "warnings": [f"No completion time for {m}; estimated from format duration." for m in missing]}

@app.post("/api/reopen/{day}")
def reopen(day: str, _=Depends(admin)):
    pday(day)
    with db() as c:
        snapshot_mutation(c)
        if not c.execute("DELETE FROM closures WHERE day=?", (day,)).rowcount: raise HTTPException(404, "Day is not closed")
        log(c, "reopen_day", f"{day} (correction)")
    return {"ok": True}

@app.get("/api/history")
def history(_=Depends(admin)):
    with db() as c:
        return {"audit": [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100")],
                "transfers": [dict(r) for r in c.execute("SELECT * FROM transfers ORDER BY id DESC LIMIT 50")]}
