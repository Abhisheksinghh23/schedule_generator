import pytest
from datetime import datetime as D, date
from engine import *

def P(i, dept="DECLARATION", rel=None, **k):
    return Person(i, i, dept, prev_release=rel, **k)
def t(h, m=0): return D(2026, 11, 2, h, m)

def test_earliest_finisher_first_and_latest_last():
    ps = [P("late", rel=t(15)), P("early", rel=t(10, 30)), P("mid", rel=t(12))]
    r = allocate([Match("1", "DECLARATION", "ODI")], ps)
    assert r.assignments["1"] == ["early", "mid"]

def test_team_sizes():
    ps = [P(f"d{i}") for i in range(4)] + [P(f"f{i}", "FANCY") for i in range(5)]
    r = allocate([Match("1", "DECLARATION", "ODI"), Match("2", "FANCY", "ODI")], ps)
    assert len(r.assignments["1"]) == 2 and len(r.assignments["2"]) == 5

def test_no_cross_department():
    ps = [P("d1"), P("d2"), P("f1", "FANCY")]
    r = allocate([Match("1", "DECLARATION", "T20")], ps)
    assert "f1" not in r.assignments["1"]

@pytest.mark.parametrize("fmt,usual", [("T20", 2), ("ODI", 2), ("TEST", 2)])
def test_usual_load_first_then_overflow_beyond_old_cap(fmt, usual):
    ps = [P("a", rel=t(9)), P("b", rel=t(10))]   # only 2 people
    ms = [Match(str(i), "DECLARATION", fmt, i) for i in range(usual + 2)]
    r = allocate(ms, ps)
    assert r.complete                             # no hard cap: every slot is filled
    for p in ps:                                  # people can now exceed the old limit
        assert sum(p.id in v for v in r.assignments.values()) == usual + 2
    assert r.overflow                             # and the overflow is reported

def test_enough_staff_still_spreads_by_usual_load():
    ps = [P(x, rel=t(9 + i)) for i, x in enumerate("abcd")]
    ms = [Match(str(i), "DECLARATION", "ODI", i) for i in (1, 2, 3)]
    r = allocate(ms, ps)
    assert r.assignments["1"] == ["a", "b"] and r.assignments["2"] == ["a", "b"] and not r.overflow
    assert r.assignments["3"] == ["c", "d"]       # default 2 per format reached -> next people

def test_limits_include_locked_assignments():
    ps = [P("a"), P("b"), P("c")]
    ms = [Match("1", "DECLARATION", "ODI", 1), Match("2", "DECLARATION", "ODI", 2)]
    r = allocate(ms, ps, locked={"1": ["a", "b"]})
    assert r.assignments["1"] == ["a", "b"]
    assert r.assignments["2"][0] == "c"           # locked load counts: free person goes first
    assert len(r.assignments["2"]) == 2           # a/b still within the default of 2 per format

def test_inactive_excluded_and_new_person_ok():
    ps = [P("x", active=False), P("new"), P("y", rel=t(11))]
    r = allocate([Match("1", "DECLARATION", "T20")], ps)
    assert sorted(r.assignments["1"]) == ["new", "y"] and r.warnings

def test_missing_release_times_dont_crash():
    r = allocate([Match("1", "FANCY", "T20")], [P(f"f{i}", "FANCY") for i in range(5)])
    assert r.complete

def test_tie_uses_recent_then_last_assigned():
    ps = [P("busy", rel=t(10), recent_count=5), P("rested", rel=t(10), recent_count=1)]
    r = allocate([Match("1", "DECLARATION", "TEST")], ps + [P("z", rel=t(10), recent_count=9)])
    assert r.assignments["1"] == ["rested", "busy"]

def test_workload_updates_dynamically():
    ps = [P("a", rel=t(9)), P("b", rel=t(9)), P("c", rel=t(9)), P("d", rel=t(9))]
    ms = [Match("1", "DECLARATION", "T20", 1), Match("2", "DECLARATION", "T20", 2)]
    r = allocate(ms, ps)
    assert set(r.assignments["1"]).isdisjoint(r.assignments["2"])

def test_cancelled_skipped_and_release_time_uses_last_completed():
    ms = {"1": Match("1", "DECLARATION", "T20", status="completed", completed_at=t(10)),
          "2": Match("2", "DECLARATION", "T20", status="completed", completed_at=t(12, 30)),
          "3": Match("3", "DECLARATION", "T20", status="cancelled", completed_at=t(18))}
    asg = {"1": ["a"], "2": ["a"], "3": ["a"]}
    assert final_release_time("a", asg, ms) == t(12, 30)
    assert final_release_time("zz", asg, ms) is None

def test_manual_validation():
    a, b = P("a"), P("b")
    ms = {"1": Match("1", "DECLARATION", "ODI"), "2": Match("2", "DECLARATION", "ODI")}
    asg = {"1": ["a"], "2": []}
    validate_manual(a, ms["2"], asg, ms)          # 2nd ODI: within default of 2
    asg3 = {"1": ["a"], "2": ["a"], "3": []}
    ms3 = {**ms, "3": Match("3", "DECLARATION", "ODI")}
    with pytest.raises(ValueError, match="limit"): validate_manual(a, ms3["3"], asg3, ms3)
    validate_manual(a, ms3["3"], asg3, ms3, override=True)   # admin override: a 3rd
    with pytest.raises(ValueError, match="duplicate"): validate_manual(a, ms["1"], asg, ms)
    with pytest.raises(ValueError, match="locked"): validate_manual(b, ms["1"], asg, ms, {"1"})
    with pytest.raises(ValueError, match="department"): validate_manual(P("f", "FANCY"), ms["2"], asg, ms)
    validate_manual(b, ms["2"], asg, ms)

def test_transfer_rules():
    p, hist = P("a"), []
    with pytest.raises(PermissionError): transfer(p, "FANCY", False, "u", hist)
    w = transfer(p, "FANCY", True, "admin1", hist, upcoming=["m9"])
    assert p.dept == "FANCY" and w and hist[0]["from"] == "DECLARATION"
    r = allocate([Match("1", "DECLARATION", "ODI")], [p, P("x")])
    assert p.id not in r.assignments["1"]       # no longer eligible in old dept

def test_repeat_generation_is_deterministic():
    ps = [P(f"d{i}", rel=t(9 + i % 3)) for i in range(40)]
    ms = [Match(str(i), "DECLARATION", "T20", i) for i in range(30)]
    assert allocate(ms, ps).assignments == allocate(ms, ps).assignments

def test_release_uses_durations_and_actuals():
    ms = {"1": Match("1", "DECLARATION", "T20", 1), "2": Match("2", "DECLARATION", "ODI", 2)}
    asg = {"1": ["a"], "2": ["a"]}
    assert release_for("a", asg, ms, t(9)) == t(21)          # 9 + 4 + 8
    ms["2"].status, ms["2"].completed_at = "completed", t(16)
    assert release_for("a", asg, ms, t(9)) == t(16)          # actual wins
    assert release_for("zz", asg, ms, t(9)) is None

def test_rest_after_test_blocks_and_future_window():
    from datetime import timedelta
    d = date(2026, 11, 10)
    def m(day, fmt="T20", comp=None): return Match("1", "DECLARATION", fmt, comp=comp, day=day)
    p = P("a"); p.rest_until = d + timedelta(days=4)             # Ranji Test on d
    assert allocate([m(d + timedelta(days=4))], [p]).unfilled      # day 4: blocked
    assert not allocate([m(d + timedelta(days=5))], [p, P("b")]).unfilled   # day 5: free
    # giving a Test to someone already booked inside the window is refused
    q = P("q"); q.future = [d + timedelta(days=3)]
    assert allocate([m(d, "TEST", "RANJI")], [q]).unfilled
    q.future = [d + timedelta(days=5)]                           # just outside Ranji window
    assert allocate([m(d, "TEST", "RANJI")], [q, P("r")]).complete
    assert allocate([m(d, "TEST", "INTERNATIONAL")], [q]).unfilled   # inside 5-day window
