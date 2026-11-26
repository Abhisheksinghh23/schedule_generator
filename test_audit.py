"""Rule-by-rule audit of the original allocation conditions."""
import os, sqlite3, subprocess, sys, tempfile
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "audit.db"))
os.environ.setdefault("ADMIN_PASSWORD", "pw")
from fastapi.testclient import TestClient
from app import app

cl, H = TestClient(app), {"X-Admin-Password": os.environ["ADMIN_PASSWORD"]}
def post(p, j=None): return cl.post("/api/" + p, json=j, headers=H)
def sched(d): return cl.get("/api/schedule/" + d, headers=H).json()["matches"]
def team(d, code): return sorted(p["name"] for m in sched(d) if m["code"] == code for p in m["people"])
def mid(d, code): return next(m["id"] for m in sched(d) if m["code"] == code)
def pid(name): return next(p["id"] for p in cl.get("/api/people", headers=H).json() if p["name"] == name)

D = "2027-01-04"   # a Monday; every test uses its own dates/people so tests stay independent

def test_auth_required_everywhere_and_public_page_only():
    import app as A
    for p in ["people", "schedule/2027-01-04", "preview/2027-01-04", "history", "report"]:
        assert cl.get("/api/" + p).status_code == 401
        A._auth_fails.clear()
    for p, j in [("generate/2027-01-04", None), ("undo", None), ("people", {"name": "x", "dept": "DECLARATION"}),
                 ("matches/bulk", {"day": D, "dept": "DECLARATION", "text": "M"}), ("lock/2027-01-04", None),
                 ("transfer", {"person_ids": [], "new_dept": "FANCY"})]:
        assert cl.post("/api/" + p, json=j).status_code == 401
        A._auth_fails.clear()
    assert cl.get("/").status_code == 200          # static page itself is public, data is not

def test_team_sizes_and_department_isolation():
    for n in ("A1", "A2", "A3", "A4"): post("people", {"name": n, "dept": "DECLARATION"})
    for n in ("B1", "B2"): post("people", {"name": n, "dept": "FANCY"})
    post("matches/bulk", {"day": D, "dept": "DECLARATION", "fmt": "T20", "text": "D-T20"})
    post("matches/bulk", {"day": D, "dept": "FANCY", "fmt": "T20", "text": "F-T20"})
    post("generate/" + D)
    assert len(team(D, "D-T20")) == 2                       # Declaration: exactly 2
    assert set(team(D, "F-T20")) <= {"B1", "B2"}            # Fancy match filled only from Fancy people
    r = post("generate/" + D).json()
    assert any(u["code"] == "F-T20" and u["missing"] == 3 for u in r["unfilled"])   # 3 slots stay empty, no cross-dept fill
    r = post("assign", {"match_id": mid(D, "D-T20"), "person_id": pid("B1")})
    assert r.status_code == 400 and "department" in r.json()["detail"].lower()

def test_daily_load_default_two_then_admin_override_logged():
    d = "2027-01-05"
    post("matches/bulk", {"day": d, "dept": "DECLARATION", "fmt": "T20", "text": "L1\nL2"})
    post("generate/" + d)
    assert team(d, "L1") == team(d, "L2")                   # T20 cap 2: same pair may hold both
    d2 = "2027-01-04"
    post("matches/bulk", {"day": d2, "dept": "DECLARATION", "fmt": "ODI", "text": "M1\nM2"})
    post("generate/" + d2)
    first = team(d2, "M1"); assert team(d2, "M2") != first  # ODI cap 1: different people
    post("matches/bulk", {"day": d2, "dept": "DECLARATION", "fmt": "ODI", "text": "M3"})
    r = post("assign", {"match_id": mid(d2, "M3"), "person_id": pid(first[0])})
    assert r.status_code == 409 and r.json()["detail"].startswith("OVERRIDE:")
    assert post("assign", {"match_id": mid(d2, "M3"), "person_id": pid(first[0]), "override": True}).status_code == 200
    log = cl.get("/api/history", headers=H).json()["audit"]
    assert any("limit override" in a["detail"] for a in log)     # override is on record

def test_impossible_schedule_reports_unfilled_with_reasons():
    d = "2027-01-06"
    for p in cl.get("/api/people", headers=H).json():
        cl.post(f"/api/people/{p['id']}/active?active=false", headers=H)
    post("matches/bulk", {"day": d, "dept": "DECLARATION", "fmt": "ODI", "text": "IMP"})
    r = post("generate/" + d).json()
    assert r["unfilled"] and r["unfilled"][0]["missing"] == 2 and r["unfilled"][0]["reasons"]
    for p in cl.get("/api/people", headers=H).json():
        cl.post(f"/api/people/{p['id']}/active?active=true", headers=H)

def test_rest_boundaries_ranji_and_international():
    for n in ("R1", "R2", "R3", "R4", "R5", "R6"): post("people", {"name": n, "dept": "DECLARATION"})
    post("matches/bulk", {"day": "2027-02-01", "dept": "DECLARATION", "fmt": "TEST", "comp": "RANJI", "text": "RT"})
    post("generate/2027-02-01")
    rt = team("2027-02-01", "RT"); assert len(rt) == 2
    post("close/2027-02-01", {"matches": [{"id": mid("2027-02-01", "RT"), "status": "completed", "done": "2027-02-04T17:00"}]})
    others = [p["id"] for p in cl.get("/api/people", headers=H).json() if p["name"] not in rt]
    for o in others: cl.post(f"/api/people/{o}/active?active=false", headers=H)
    for blocked in ("2027-02-04",):                          # final Test day (no recovery days)
        post("matches/bulk", {"day": blocked, "dept": "DECLARATION", "fmt": "T20", "text": "B" + blocked[-2:]})
        post("generate/" + blocked)
        assert team(blocked, "B" + blocked[-2:]) == []        # still resting
    post("matches/bulk", {"day": "2027-02-05", "dept": "DECLARATION", "fmt": "T20", "text": "FREE"})
    post("generate/2027-02-05")
    assert team("2027-02-05", "FREE") == rt                   # free the day after the Test
    r = post("assign", {"match_id": mid("2027-02-04", "B04"), "person_id": pid(rt[0])})
    assert r.status_code == 400 and "booked on a Test" in r.json()["detail"]   # manual assignment also blocked
    # International: 5 days, no recovery
    post("matches/bulk", {"day": "2027-03-01", "dept": "DECLARATION", "fmt": "TEST", "comp": "INTERNATIONAL", "text": "IT"})
    post("generate/2027-03-01"); it = team("2027-03-01", "IT"); assert len(it) == 2
    post("close/2027-03-01", {"matches": [{"id": mid("2027-03-01", "IT"), "status": "completed", "done": "2027-03-05T17:00"}]})
    post("matches/bulk", {"day": "2027-03-05", "dept": "DECLARATION", "fmt": "T20", "text": "IB"})
    post("generate/2027-03-05"); assert team("2027-03-05", "IB") == []
    post("matches/bulk", {"day": "2027-03-06", "dept": "DECLARATION", "fmt": "T20", "text": "IF"})
    post("generate/2027-03-06"); assert team("2027-03-06", "IF") == it

def test_cancelled_test_does_not_block():
    post("matches/bulk", {"day": "2027-04-01", "dept": "DECLARATION", "fmt": "TEST", "comp": "RANJI", "text": "CT"})
    post("generate/2027-04-01")
    post("close/2027-04-01", {"matches": [{"id": mid("2027-04-01", "CT"), "status": "cancelled"}]})
    for p in cl.get("/api/people", headers=H).json():
        cl.post(f"/api/people/{p['id']}/active?active=true", headers=H)
    post("matches/bulk", {"day": "2027-04-02", "dept": "DECLARATION", "fmt": "T20", "text": "NX"})
    post("generate/2027-04-02")
    assert len(team("2027-04-02", "NX")) == 2

def test_release_times_and_next_day_priority():
    d1, d2, d3 = "2027-05-03", "2027-05-04", "2027-05-05"
    post("matches/bulk", {"day": d1, "dept": "DECLARATION", "fmt": "T20", "text": "E1\nE2"})
    post("generate/" + d1)
    ids = {m["code"]: m["id"] for m in sched(d1)}
    post("close/" + d1, {"matches": [{"id": ids["E1"], "status": "completed", "done": "12:00"},
                                     {"id": ids["E2"], "status": "completed", "done": "19:30"}]})
    early, late = team(d1, "E1"), team(d1, "E2")
    post("matches/bulk", {"day": d2, "dept": "DECLARATION", "fmt": "T20", "text": "N1\nN2"})
    post("generate/" + d2)
    assert team(d2, "N1") == early and team(d2, "N2") == late   # earliest previous-day release first
    ids = {m["code"]: m["id"] for m in sched(d2)}
    post("close/" + d2, {"matches": [{"id": ids["N1"], "status": "completed", "done": "21:00"},
                                     {"id": ids["N2"], "status": "completed", "done": "13:00"}]})
    post("matches/bulk", {"day": d3, "dept": "DECLARATION", "fmt": "T20", "text": "C1"})
    post("generate/" + d3)
    assert team(d3, "C1") == late                               # 13:00 finishers beat 21:00 finishers next day

def test_lock_unlock_and_closed_day():
    d = "2027-06-07"
    post("matches/bulk", {"day": d, "dept": "DECLARATION", "fmt": "T20", "text": "K1"})
    post("generate/" + d); t = team(d, "K1")
    post("lock/" + d)
    post("generate/" + d)
    assert team(d, "K1") == t                                 # locked assignments survive regenerate
    r = post("unassign", {"match_id": mid(d, "K1"), "person_id": pid(t[0])})
    assert r.status_code == 409 and "locked" in r.json()["detail"]
    r = cl.delete("/api/matches/" + str(mid(d, "K1")), headers=H)
    assert r.status_code == 409                               # locked match cannot be deleted
    post(f"lock/{d}?locked=false")
    post("close/" + d, {"matches": [{"id": mid(d, "K1"), "status": "completed", "done": "13:00"}]})
    assert post("generate/" + d).status_code == 409           # closed day is frozen
    assert post("matches/bulk", {"day": d, "dept": "DECLARATION", "text": "K2"}).status_code == 409
    assert post("assign", {"match_id": mid(d, "K1"), "person_id": pid("A3")}).status_code == 409
    post("reopen/" + d)
    assert post("matches/bulk", {"day": d, "dept": "DECLARATION", "text": "K2"}).json()["added"] == 1
    log = cl.get("/api/history", headers=H).json()["audit"]
    assert any(a["action"] == "close_day" for a in log) and any(a["action"] == "reopen_day" for a in log)

def test_leave_blocks_generate_and_manual():
    d = "2027-07-05"
    post(f"people/{pid('A1')}/leave", {"date": d})
    post("matches/bulk", {"day": d, "dept": "DECLARATION", "fmt": "T20", "text": "LV"})
    post("generate/" + d)
    assert "A1" not in team(d, "LV")
    r = post("assign", {"match_id": mid(d, "LV"), "person_id": pid("A1")})
    assert r.status_code == 400 and "leave" in r.json()["detail"].lower()
    cl.delete(f"/api/people/{pid('A1')}/leave?day={d}", headers=H)

def test_transfers_admin_only_confirmed_and_logged():
    r = cl.post("/api/transfer", json={"person_ids": [pid("B1")], "new_dept": "DECLARATION"})
    assert r.status_code == 401
    r = post("transfer", {"person_ids": [pid("B1")], "new_dept": "DECLARATION"}).json()
    assert r["needs_confirm"]
    assert post("transfer", {"person_ids": [pid("B1")], "new_dept": "DECLARATION", "confirm": True}).json()["moved"] == 1
    h = cl.get("/api/history", headers=H).json()["transfers"]
    assert h[0]["from_dept"] == "FANCY" and h[0]["to_dept"] == "DECLARATION"

def test_duplicates_rejected():
    assert post("people", {"name": "a1", "dept": "DECLARATION"}).status_code == 409   # case-insensitive
    d = "2027-08-02"
    post("matches/bulk", {"day": d, "dept": "DECLARATION", "fmt": "T20", "text": "DU"})
    r = post("matches/bulk", {"day": d, "dept": "DECLARATION", "fmt": "T20", "text": "DU"}).json()
    assert r["added"] == 0 and r["warnings"]
    post("generate/" + d)
    r = post("assign", {"match_id": mid(d, "DU"), "person_id": team(d, "DU")[0] and pid(team(d, "DU")[0])})
    assert r.status_code == 400 and "duplicate" in r.json()["detail"].lower()

def test_undo_and_persistence_across_restart():
    d = "2027-09-06"
    assert post("people", {"name": "Persist One", "dept": "DECLARATION"}).status_code == 200
    assert post("undo").status_code == 200
    assert not any(p["name"] == "Persist One" for p in cl.get("/api/people", headers=H).json())
    post("people", {"name": "Persist Two", "dept": "DECLARATION"})
    env = dict(os.environ)
    out = subprocess.run([sys.executable, "-c",
        "import os; from fastapi.testclient import TestClient; from app import app;"
        "ps = TestClient(app).get('/api/people', headers={'X-Admin-Password':os.environ['ADMIN_PASSWORD']}).json();"
        "print(any(p['name']=='Persist Two' for p in ps))"],
        env=env, capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
    assert out.stdout.strip() == "True"                        # fresh process still sees the data


def test_auth_bruteforce_lockout():
    import app as A
    old = A.MAX_AUTH_FAILS
    A.MAX_AUTH_FAILS = 3
    try:
        for _ in range(3):
            assert cl.get("/api/people", headers={"X-Admin-Password": "wrong"}).status_code == 401
        assert cl.get("/api/people", headers={"X-Admin-Password": "wrong"}).status_code == 429
        assert cl.get("/api/people", headers=H).status_code == 429          # even correct password is blocked during lockout
    finally:
        A.MAX_AUTH_FAILS = old
        A._auth_fails.clear()
    assert cl.get("/api/people", headers=H).status_code == 200              # clears after reset

def test_deployed_startup_requires_strong_password():
    env = {k: v for k, v in os.environ.items() if k != "ADMIN_PASSWORD"}
    env["DATABASE_URL"] = "postgresql://x:x@localhost:1/x"                  # marks "deployed"
    code = "import importlib,sys; sys.argv=['x']; import app" if False else (
        "import runpy; runpy.run_path('app.py')")
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    assert r.returncode != 0 and "ADMIN_PASSWORD" in (r.stderr + r.stdout)  # fails closed with no password
    env["ADMIN_PASSWORD"] = "admin123"
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    assert r.returncode != 0 and "ADMIN_PASSWORD" in (r.stderr + r.stdout)  # fails closed with the default


def test_spoofed_xff_cannot_bypass_lockout():
    import app as A
    old_id, old_peer = A.MAX_AUTH_FAILS, A.PEER_AUTH_FAILS
    A.MAX_AUTH_FAILS, A.PEER_AUTH_FAILS = 100, 5          # peer backstop trips first
    try:
        for i in range(5):
            r = cl.get("/api/people", headers={"X-Admin-Password": "wrong", "X-Forwarded-For": f"10.0.0.{i}"})
            assert r.status_code == 401
        r = cl.get("/api/people", headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.9.9.9"})
        assert r.status_code == 429                       # rotating spoofed XFF still blocked via peer bucket
    finally:
        A.MAX_AUTH_FAILS, A.PEER_AUTH_FAILS = old_id, old_peer
        A._auth_fails.clear()
    assert cl.get("/api/people", headers=H).status_code == 200

def test_cf_connecting_ip_separates_clients():
    import app as A
    old = A.MAX_AUTH_FAILS
    A.MAX_AUTH_FAILS = 2
    try:
        bad = {"X-Admin-Password": "wrong", "CF-Connecting-IP": "203.0.113.9"}
        assert cl.get("/api/people", headers=bad).status_code == 401
        assert cl.get("/api/people", headers=bad).status_code == 401
        assert cl.get("/api/people", headers=bad).status_code == 429          # that client locked out
        assert cl.get("/api/people", headers=H).status_code == 200            # another client unaffected
    finally:
        A.MAX_AUTH_FAILS = old
        A._auth_fails.clear()


def test_second_same_day_test_needs_override_and_third_is_blocked():
    # Admin may stack a second Test starting the same day (override, max 2); never a third, never automatic.
    d = "2027-08-02"
    post("people", {"name": "TX1", "dept": "DECLARATION"})
    post("matches/bulk", {"day": d, "dept": "DECLARATION", "fmt": "TEST", "comp": "RANJI", "text": "TA\nTB\nTC"})
    p = pid("TX1")
    assert post("assign", {"match_id": mid(d, "TA"), "person_id": p}).status_code == 200
    r = post("assign", {"match_id": mid(d, "TB"), "person_id": p})
    assert r.status_code == 409 and "OVERRIDE" in r.json()["detail"]
    assert post("assign", {"match_id": mid(d, "TB"), "person_id": p, "override": True}).status_code == 200
    r = post("assign", {"match_id": mid(d, "TC"), "person_id": p, "override": True})
    assert r.status_code == 400 and "maximum (2)" in r.json()["detail"]
    # still blocked through the final Test day (day 4), free on day 5
    d4, d5 = "2027-08-05", "2027-08-06"
    post("matches/bulk", {"day": d4, "dept": "DECLARATION", "fmt": "T20", "text": "TD4"})
    post("matches/bulk", {"day": d5, "dept": "DECLARATION", "fmt": "T20", "text": "TD5"})
    r = post("assign", {"match_id": mid(d4, "TD4"), "person_id": p, "override": True})
    assert r.status_code == 400 and "booked" in r.json()["detail"]
    assert post("assign", {"match_id": mid(d5, "TD5"), "person_id": p}).status_code == 200
    # a Test starting a different day inside the span is still refused
    d2 = "2027-08-03"
    post("matches/bulk", {"day": d2, "dept": "DECLARATION", "fmt": "TEST", "comp": "RANJI", "text": "TE"})
    r = post("assign", {"match_id": mid(d2, "TE"), "person_id": p, "override": True})
    assert r.status_code == 400
