import os, sqlite3, tempfile
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["ADMIN_PASSWORD"] = "pw"
from fastapi.testclient import TestClient
from app import app, DB

cl, H = TestClient(app), {"X-Admin-Password": "pw"}
D1, D2, D3 = "2026-11-03", "2026-11-04", "2026-11-05"
def post(p, j=None): return cl.post("/api/" + p, json=j, headers=H)
def sched(d): return cl.get("/api/schedule/" + d, headers=H).json()["matches"]
def team(d, code): return [p["name"] for m in sched(d) if m["code"] == code for p in m["people"]]

def test_flow():
    for n in "abcd": assert post("people", {"name": n, "dept": "DECLARATION"}).status_code == 200
    for i in range(5): post("people", {"name": f"f{i}", "dept": "FANCY"})
    assert post("people", {"name": "A", "dept": "FANCY"}).status_code == 409        # duplicate
    post("matches/bulk", {"day": D1, "dept": "DECLARATION", "fmt": "ODI", "text": "M1"})
    r = post("matches/bulk", {"day": D1, "dept": "DECLARATION", "fmt": "T20", "text": "M2\nM1"})
    assert r.json()["added"] == 1 and r.json()["warnings"]                           # dup skipped
    assert post("generate/" + D1).status_code == 200
    assert team(D1, "M1") == ["a", "b"] and team(D1, "M2") == ["c", "d"]
    ids = {m["code"]: m["id"] for m in sched(D1)}
    r = post("close/" + D1, {"matches": [{"id": ids["M1"], "status": "completed", "done": "10:00"},
                                         {"id": ids["M2"], "status": "completed", "done": "18:00"}]})
    assert r.status_code == 200
    assert post("generate/" + D1).status_code == 409                                  # closed
    post("matches/bulk", {"day": D2, "dept": "DECLARATION", "fmt": "T20", "text": "M3"})
    post("generate/" + D2)
    assert team(D2, "M3") == ["a", "b"]            # a,b finished earlier -> priority

def test_no_format_cap_and_department_rules():
    post("matches/bulk", {"day": D3, "dept": "DECLARATION", "fmt": "ODI", "text": "X1\nX2"})
    post("generate/" + D3)
    ids = {m["code"]: m["id"] for m in sched(D3)}
    assert team(D3, "X1") == team(D3, "X2") == ["a", "b"]      # default 2 per format: same pair can take both
    post("matches/bulk", {"day": D3, "dept": "DECLARATION", "fmt": "ODI", "text": "X3"})
    ids = {m["code"]: m["id"] for m in sched(D3)}
    r = post("assign", {"match_id": ids["X3"], "person_id": "P0001"})
    assert r.status_code == 409 and r.json()["detail"].startswith("OVERRIDE:")     # 3rd needs admin override
    assert post("assign", {"match_id": ids["X3"], "person_id": "P0001", "override": True}).status_code == 200
    assert post("assign", {"match_id": ids["X3"], "person_id": "P0005"}).status_code == 400  # Fancy person

def test_lock_transfer_auth_persistence():
    ids = {m["code"]: m["id"] for m in sched(D3)}
    post("lock/" + D3)
    p = sched(D3)[0]["people"][0]["id"]
    assert post("unassign", {"match_id": ids["X1"], "person_id": p}).status_code == 409
    assert cl.post("/api/transfer", json={"person_ids": ["P0005"], "new_dept": "DECLARATION"}).status_code == 401
    r = post("transfer", {"person_ids": ["P0005"], "new_dept": "DECLARATION"}).json()
    assert r["needs_confirm"]
    assert post("transfer", {"person_ids": ["P0005"], "new_dept": "DECLARATION", "confirm": True}).json()["moved"] == 1
    h = cl.get("/api/history", headers=H).json()
    assert h["transfers"][0]["from_dept"] == "FANCY" and h["audit"]
    assert sqlite3.connect(DB).execute("SELECT COUNT(*) FROM people").fetchone()[0] == 9

def test_test_rest_days_ranji_and_international():
    import itertools
    # Ranji Test on 11-10 for X1,X2; International Test on 11-20 for Y1,Y2 (fresh people)
    for n in ("X1", "X2", "Y1", "Y2", "Z1", "Z2"):
        post("people", {"name": n, "dept": "DECLARATION"})
    def day(d, code, fmt, comp="INTERNATIONAL"):
        post("matches/bulk", {"day": d, "dept": "DECLARATION", "fmt": fmt, "comp": comp, "text": code})
    day("2026-11-10", "T1", "TEST", "RANJI"); post("generate/2026-11-10")
    first = team("2026-11-10", "T1")
    assert len(first) == 2
    test_id = next(m["id"] for m in sched("2026-11-10") if m["code"] == "T1")
    assert post("close/2026-11-10", {"matches": [{"id": test_id, "status": "completed",
                                                    "done": "2026-11-13T17:00"}]}).status_code == 200
    # Block everyone else so only the resting pair could fill the slots
    others = [r["id"] for r in cl.get("/api/people", headers=H).json() if r["name"] not in first]
    for pid in others: cl.post(f"/api/people/{pid}/active?active=false", headers=H)
    day("2026-11-17", "R7", "T20"); post("generate/2026-11-17")      # final Ranji recovery day
    assert team("2026-11-17", "R7") == []
    day("2026-11-18", "R8", "T20"); post("generate/2026-11-18")      # free after 4 recovery days
    assert sorted(team("2026-11-18", "R8")) == sorted(first)
    # Manual assignment is rejected through the final recovery day.
    ids = {m["code"]: m["id"] for m in sched("2026-11-17")}
    pid = [r["id"] for r in cl.get("/api/people", headers=H).json() if r["name"] == first[0]][0]
    cl.post(f"/api/people/{pid}/active?active=true", headers=H)
    r = post("assign", {"match_id": ids["R7"], "person_id": pid})
    assert r.status_code == 400 and "resting" in r.json()["detail"]


def test_add_matches_with_end_date():
    body = {"dept": "DECLARATION", "fmt": "T20", "text": "RNG1\nRNG2, ODI"}
    r = post("matches/bulk", {**body, "day": "2026-12-01"})                      # no end date -> start date only
    assert r.json()["added"] == 2 and r.json()["days"] == 1 and len(sched("2026-12-02")) == 0
    r = post("matches/bulk", {**body, "day": "2026-12-01", "end_day": "2026-12-03"})
    assert r.json()["days"] == 3 and r.json()["added"] == 4                      # day 1 duplicates skipped
    assert len(r.json()["warnings"]) == 2
    for d in ("2026-12-01", "2026-12-02", "2026-12-03"): assert len(sched(d)) == 2
    assert len(sched("2026-12-04")) == 0
    assert post("matches/bulk", {**body, "day": "2026-12-05", "end_day": "2026-12-05"}).json()["days"] == 1
    assert post("matches/bulk", {**body, "day": "2026-12-05", "end_day": "2026-12-01"}).status_code == 400
    assert post("matches/bulk", {**body, "day": "2026-12-05", "end_day": "bad"}).status_code == 400
    assert post("matches/bulk", {**body, "day": "2026-12-05", "end_day": ""}).status_code == 200


def test_custom_required_slots_for_matches():
    post("people", {"name": "c1", "dept": "DECLARATION"})
    post("people", {"name": "c2", "dept": "DECLARATION"})
    post("people", {"name": "c3", "dept": "DECLARATION"})
    post("people", {"name": "c4", "dept": "DECLARATION"})
    day = "2026-12-10"
    r = post("matches/bulk", {"day": day, "dept": "DECLARATION", "fmt": "T20", "need": 3, "text": "REQ1"})
    assert r.status_code == 200
    match = next(m for m in sched(day) if m["code"] == "REQ1")
    assert match["need"] == 3
    assert len(match["people"]) == 0


def test_leave_preview_and_undo_and_delete():
    r = post("people", {"name": "l1", "dept": "DECLARATION"})
    pid = r.json()["id"]
    for person in cl.get("/api/people", headers=H).json():
        if person["id"] != pid:
            cl.post(f"/api/people/{person['id']}/active?active=false", headers=H)
    assert post(f"people/{pid}/leave", {"date": "2026-12-20"}).status_code == 200
    post("matches/bulk", {"day": "2026-12-20", "dept": "DECLARATION", "fmt": "T20", "text": "LTEST"})
    preview = cl.get("/api/preview/2026-12-20", headers=H)
    assert preview.status_code == 200 and preview.json()["unfilled"][0]["missing"] == 2
    assert post("generate/2026-12-20").status_code == 200
    assert cl.post("/api/undo", headers=H).status_code == 200
    assert cl.delete(f"/api/people/{pid}", headers=H).status_code == 200
    next_person = post("people", {"name": "l2", "dept": "DECLARATION"}).json()
    assert int(next_person["id"][1:]) > int(pid[1:])
    assert post(f"people/{next_person['id']}/leave", {"date": "2026-12-21"}).status_code == 200
    assert cl.delete(f"/api/people/{next_person['id']}/leave?day=2026-12-21", headers=H).status_code == 200
    roster = cl.get("/api/people", headers=H).json()
    assert next(person for person in roster if person["id"] == next_person["id"])["leave"] == ""
