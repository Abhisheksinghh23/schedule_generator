"""Smart Match Allocation - core engine (pure Python, no I/O)."""
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Optional

SLOTS = {"DECLARATION": 2, "FANCY": 5}
LIMITS = {"T20": 2, "ODI": 2, "TEST": 2}   # default daily load per person (every format = 2).
                                           # Auto-generate keeps to it when it can; the admin can
                                           # override it manually (needs confirmation).
REST_DAYS = {"RANJI": 4, "INTERNATIONAL": 5}      # no matches for N days after a Test
DURATION = {"T20": 4, "ODI": 8, "TEST": 8}   # hours per match


@dataclass
class Person:
    id: str
    name: str
    dept: str
    active: bool = True
    prev_release: Optional[datetime] = None   # previous-day final release
    last_assigned: Optional[date] = None
    recent_count: int = 0                      # assignments in recent period
    rest_until: Optional[date] = None          # last blocked day after a past Test
    future: list = field(default_factory=list)  # dates of already-booked later matches


@dataclass
class Match:
    id: str
    dept: str
    fmt: str
    order: int = 0
    status: str = "scheduled"                  # scheduled/completed/cancelled
    completed_at: Optional[datetime] = None
    start: Optional[datetime] = None
    comp: Optional[str] = None                 # RANJI / INTERNATIONAL (Tests)
    day: Optional[date] = None
    need: int = 0                              # custom required team size for this match


@dataclass
class Result:
    assignments: dict = field(default_factory=dict)  # match_id -> [person ids]
    unfilled: list = field(default_factory=list)     # (match_id, missing, reasons)
    warnings: list = field(default_factory=list)
    overflow: list = field(default_factory=list)     # (match_id, [names]) filled beyond usual daily load

    @property
    def complete(self):
        return not self.unfilled


def final_release_time(person_id, assignments, matches):
    """Completion time of the person's LAST completed match. Cancelled or
    uncompleted matches are ignored. None if nothing completed."""
    times = [matches[m].completed_at for m, ps in assignments.items()
             if person_id in ps and matches[m].status == "completed"
             and matches[m].completed_at]
    return max(times) if times else None


def ineligible_reason(p, m, load, limits=True):
    """Hard-rule check. load: {person_id: {fmt: count}} for the date.
    limits=False skips the usual daily format load (manual assignment and the
    auto-generate overflow pass); all other hard rules still apply."""
    if not p.active:
        return "inactive"
    if p.dept != m.dept:
        return "wrong department"
    used = load.get(p.id, {})
    if limits and used.get(m.fmt, 0) >= LIMITS[m.fmt]:
        return f"daily {m.fmt} limit ({LIMITS[m.fmt]}) reached"
    if m.day and p.rest_until and m.day <= p.rest_until:
        return f"resting after a Test until {p.rest_until}"
    if m.fmt == "TEST" and m.day:
        end = m.day + timedelta(days=REST_DAYS.get(m.comp or "INTERNATIONAL", 5))
        if any(m.day < f <= end for f in p.future):
            return "has other matches inside the post-Test rest window"
    return None


def _key(p, load):
    never = datetime.max
    return (p.prev_release is None,          # known release times first
            p.prev_release or never,         # earliest finisher first
            sum(load.get(p.id, {}).values()),  # fewer assignments today
            p.recent_count,                  # fewer recent assignments
            p.last_assigned or date.min,     # longest since last assignment
            p.id)                            # stable tie-break


def allocate(matches, people, locked=None):
    """Allocate people to matches. `locked` = {match_id: [person ids]} are kept
    untouched and counted toward daily load. Pass 1 respects the usual daily
    format load; if a match is still short, pass 2 reuses people beyond it
    (least-loaded first) instead of leaving the slot empty."""
    locked = locked or {}
    res, load = Result(), {}
    by_id = {p.id: p for p in people}

    def book(pid, fmt):
        load.setdefault(pid, {})
        load[pid][fmt] = load[pid].get(fmt, 0) + 1

    for m in matches:                        # locked slots first
        for pid in locked.get(m.id, []):
            book(pid, m.fmt)
    if any(p.active and p.prev_release is None for p in people):
        res.warnings.append("Some people have no previous-day release time; "
                            "fairness tie-breakers used for them.")

    for m in sorted(matches, key=lambda x: x.order):
        if m.status == "cancelled":
            continue
        need = m.need or SLOTS[m.dept]
        chosen = list(locked.get(m.id, []))
        pool = [p for p in people if p.id not in chosen
                and ineligible_reason(p, m, load) is None]
        pool.sort(key=lambda p: _key(p, load))
        for p in pool[:max(0, need - len(chosen))]:
            chosen.append(p.id)
            book(p.id, m.fmt)
        if len(chosen) < need:               # pass 2: no daily format cap
            extra = [p for p in people if p.id not in chosen
                     and ineligible_reason(p, m, load, limits=False) is None]
            extra.sort(key=lambda p: (sum(load.get(p.id, {}).values()), _key(p, load)))
            added = extra[:need - len(chosen)]
            for p in added:
                chosen.append(p.id)
                book(p.id, m.fmt)
            if added:
                res.overflow.append((m.id, [p.name for p in added]))
        reasons = {}
        if len(chosen) < need:
            for p in people:
                if p.id not in chosen:
                    r = ineligible_reason(p, m, load, limits=False)
                    if r: reasons[p.name] = r
        res.assignments[m.id] = chosen
        if len(chosen) < need:
            res.unfilled.append((m.id, need - len(chosen), reasons))
    return res


class LimitReached(ValueError):
    """Default daily format load reached; the admin may override it."""


def validate_manual(person, match, assignments, matches, locked_ids=(), override=False):
    """Manual add/replace check; raises ValueError on any hard-rule breach.
    Going past the default daily load raises LimitReached unless override=True."""
    if match.id in locked_ids:
        raise ValueError("assignment is locked")
    if person.id in assignments.get(match.id, []):
        raise ValueError("duplicate assignment")
    load = {}
    for mid, ps in assignments.items():
        for pid in ps:
            load.setdefault(pid, {})
            f = matches[mid].fmt
            load[pid][f] = load[pid].get(f, 0) + 1
    r = ineligible_reason(person, match, load, limits=False)
    if r:
        raise ValueError(r)
    if not override and load.get(person.id, {}).get(match.fmt, 0) >= LIMITS[match.fmt]:
        raise LimitReached(f"daily {match.fmt} limit ({LIMITS[match.fmt]}) reached")
    need = match.need or SLOTS[match.dept]
    if len(assignments.get(match.id, [])) >= need:
        raise ValueError("match team is full")


def transfer(person, new_dept, is_admin, admin_id, history, upcoming=()):
    """Admin-only transfer. Past assignments are never touched; returns
    warnings if the person has upcoming assignments in the old department."""
    if not is_admin:
        raise PermissionError("only administrators can transfer people")
    if new_dept not in SLOTS or new_dept == person.dept:
        raise ValueError("invalid transfer target")
    warnings = ([f"{len(upcoming)} upcoming assignment(s) in {person.dept}"]
                if upcoming else [])
    history.append({"person": person.id, "from": person.dept, "to": new_dept,
                    "admin": admin_id, "at": datetime.now()})
    person.dept = new_dept
    return warnings


def release_for(pid, assignments, matches, day_start):
    """Release time = when the person's last match ends. Matches run back to
    back from day_start (or their own start time); each lasts DURATION hours
    (T20 4h, ODI/Test 8h) unless an actual completion time was entered."""
    mine = sorted((matches[m] for m, ps in assignments.items()
                   if pid in ps and matches[m].status != "cancelled"),
                  key=lambda m: m.order)
    if not mine:
        return None
    cur = day_start
    for m in mine:
        st = max(m.start, cur) if m.start else cur
        cur = (m.completed_at if m.status == "completed" and m.completed_at
               else st + timedelta(hours=DURATION[m.fmt]))
    return cur
