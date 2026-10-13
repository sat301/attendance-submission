"""
OPTIONAL self-check (not part of the submission). Needs a real MongoDB 6+:
    docker run -d -p 27017:27017 mongo:7
    MONGO_URI=mongodb://localhost:27017 MONGO_DB=verify_db python tools/verify_analytics.py [--big]
Seeds awkward data directly into MongoDB (ties, no-log employees, weekend records, mid-month joiner, overnight shift,
orphan logs, legacy docs without half_day/history), computes every analytic answer with a plain-Python oracle and compares
it with the real endpoints. --big also loads ~100k attendance records and checks that every explain plan has IXSCAN, no COLLSCAN.
"""
import json, os, random, sys
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient
from app import main as m

random.seed(7)
PRES = {"PRESENT", "WFH", "ON_DUTY"}
hu = lambda x, n: float(Decimal(str(x)).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP))
hu_frac = lambda f, n: float((Decimal(f.numerator) / Decimal(f.denominator)).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP))
db = m.db
db.employees.delete_many({}); db.attendance_logs.delete_many({})

emps = []
def emp(code, dept, joined, ss="09:30", se="18:30"):
    emps.append({"emp_code": code, "name": "N" + code, "email": "a@b.io", "department": dept, "shift_start": ss, "shift_end": se,
                 "joined_on": joined, "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
N = 40
for i in range(1, N + 1):
    emp(f"EMP{i:04d}", ["Eng", "Sales", "Support"][i % 3], "2026-01-05")
emp("EMP0100", "Eng", "2026-07-15")            # mid-month joiner
emp("EMP0101", "Eng", "2026-08-03")            # joins after July
emp("EMP0102", "Support", "2026-01-05", "22:00", "06:00")  # overnight
emp("EMP0103", "Ops", "2026-01-05")            # department whose people have NO logs at all
emp("EMP0104", "Ops", "2026-07-31")            # joins on the last day
db.employees.insert_many([dict(e) for e in emps])

logs, seen = [], set()
def add(code, d, status, late=0, ot=0, wh=None, half=None):
    if (code, d) in seen: return
    seen.add((code, d))
    doc = {"emp_code": code, "date": d, "status": status, "punch_in": datetime(2026, 1, 1, tzinfo=timezone.utc) if status in PRES else None,
           "punch_out": None, "work_hours": wh, "late_minutes": late, "overtime_minutes": ot}
    if half is not None: doc["half_day"] = half
    if random.random() < 0.9: doc["history"] = []
    logs.append(doc)
def rand_logs(codes, d0, d1):
    d = d0
    while d <= d1:
        for c in codes:
            r = random.random()
            if r < 0.15: continue                                 # no record at all (not the same as ABSENT)
            if r < 0.22: add(c, d.isoformat(), "ABSENT"); continue
            if r < 0.30: add(c, d.isoformat(), "LEAVE"); continue
            st = random.choice(["PRESENT", "PRESENT", "WFH", "ON_DUTY"])
            late = random.choice([0, 0, 0, 11, 25, 25, 40]); wh = round(random.uniform(2, 11), 2)
            add(c, d.isoformat(), st, late, random.choice([0, 0, 35, 70]), wh if random.random() < 0.9 else None, wh < 4.5 if random.random() < 0.9 else None)
        d += timedelta(days=1)
codes = [e["emp_code"] for e in emps if e["emp_code"] not in ("EMP0103", "EMP0104")]
rand_logs(codes, date(2026, 6, 25), date(2026, 8, 5))
for i in range(30): add("GHOST01", f"2026-07-{i+1:02d}", "PRESENT", 50)  # orphan logs: must be ignored
for c in ("EMP0001", "EMP0002", "EMP0003"): add(c, "2026-07-04", "PRESENT", 100, 0, 8.0, False)  # Saturday, same late -> ties
db.attendance_logs.insert_many(logs)
print(f"seeded {len(emps)} employees, {len(logs)} logs")

E = {e["emp_code"]: e for e in emps}
def month_rng(month):
    y, mo = int(month[:4]), int(month[5:]); a = date(y, mo, 1)
    b = (date(y + (mo == 12), mo % 12 + 1, 1)) - timedelta(days=1); return a, b
def in_month(l, month): return l["date"][:7] == month and l["emp_code"] in E
def present_w(l): return (0.5 if l.get("half_day") else 1) if l["status"] in PRES and date.fromisoformat(l["date"]).weekday() < 5 else 0
def weekdays(a, b): return sum(1 for i in range((b - a).days + 1) if (a + timedelta(i)).weekday() < 5) if b >= a else 0

failures = 0
def check(name, got, want):
    global failures
    if got != want:
        failures += 1; print("MISMATCH", name); print("  got :", json.dumps(got)[:600]); print("  want:", json.dumps(want)[:600])

with TestClient(m.app) as c:
    for month in ("2026-06", "2026-07", "2026-08"):
        a, b = month_rng(month)
        # employee monthly
        for code in ("EMP0001", "EMP0100", "EMP0101", "EMP0103", "EMP0102", "EMP0007"):
            ls = [l for l in logs if l["emp_code"] == code and l["date"][:7] == month]
            wd = weekdays(max(a, date.fromisoformat(E[code]["joined_on"])), b); pd_ = sum(present_w(l) for l in ls)
            want = {"emp_code": code, "month": month, "working_days": wd, "present_days": hu(pd_, 2),
                    "leave_days": sum(l["status"] == "LEAVE" for l in ls), "late_count": sum(l["late_minutes"] > 0 for l in ls),
                    "total_late_minutes": sum(l["late_minutes"] for l in ls), "total_overtime_minutes": sum(l["overtime_minutes"] for l in ls),
                    "attendance_pct": hu_frac(Fraction(pd_).limit_denominator(10) / wd * 100, 2) if wd else None}
            check(f"monthly {code} {month}", c.get(f"/analytics/employees/{code}/monthly", params={"month": month}).json(), want)
        # department summary
        want = []
        for dept in sorted({e["department"] for e in emps}):
            hc = [e for e in emps if e["department"] == dept and e["joined_on"] <= b.isoformat()]
            if not hc: continue
            # R9: an employee cannot contribute attendance before joining; future joiners
            # are excluded from both the month-end headcount and that month's totals.
            ls = [l for l in logs if in_month(l, month) and E[l["emp_code"]]["department"] == dept
                  and E[l["emp_code"]]["joined_on"] <= b.isoformat()]
            hrs = [Decimal(str(l["work_hours"])) for l in ls if l["status"] in PRES and l.get("work_hours") is not None]
            want.append({"department": dept, "headcount": len(hc), "present_days": hu(sum(present_w(l) for l in ls), 2),
                         "avg_work_hours": float((sum(hrs) / len(hrs)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if hrs else None,
                         "late_count": sum(l["late_minutes"] > 0 for l in ls), "total_late_minutes": sum(l["late_minutes"] for l in ls),
                         "leave_count": sum(l["status"] == "LEAVE" for l in ls), "on_duty_count": sum(l["status"] == "ON_DUTY" for l in ls)})
        check(f"summary {month}", c.get("/analytics/departments/summary", params={"month": month}).json(), {"month": month, "items": want})
        # leaderboard (ties + limit after ranking + department scope)
        for dept, limit in ((None, 10), (None, 3), ("Eng", 5), ("Support", 50)):
            tot = {}
            for l in logs:
                if in_month(l, month) and l["late_minutes"] > 0 and (dept is None or E[l["emp_code"]]["department"] == dept):
                    t = tot.setdefault(l["emp_code"], [0, 0]); t[0] += l["late_minutes"]; t[1] += 1
            rows = sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0])); out = []
            for i, (code, (mins, n)) in enumerate(rows):
                rank = 1 + sum(1 for _, (m2, _n) in rows if m2 > mins)
                if rank <= limit:
                    out.append({"rank": rank, "emp_code": code, "name": E[code]["name"], "department": E[code]["department"], "total_late_minutes": mins, "late_count": n})
            p = {"month": month, "limit": limit}; p.update({"department": dept} if dept else {})
            check(f"leaderboard {month} {dept} {limit}", c.get("/analytics/leaderboard/late", params=p).json(), {"month": month, "items": out})
    # trend
    for dept, f, t in (("Eng", "2026-07-01", "2026-07-31"), ("Ops", "2026-07-25", "2026-08-05"), ("Support", "2026-06-28", "2026-07-10"), ("Eng", "2026-07-04", "2026-07-04")):
        d0, d1 = date.fromisoformat(f), date.fromisoformat(t); rows, rates = [], []
        for i in range((d1 - d0).days + 1):
            day = d0 + timedelta(i); ds = day.isoformat()
            hc = sum(1 for e in emps if e["department"] == dept and e["joined_on"] <= ds)
            ls = [l for l in logs if l["date"] == ds and l["emp_code"] in E and E[l["emp_code"]]["department"] == dept]
            pc = sum((0.5 if l.get("half_day") else 1) for l in ls if l["status"] in PRES)
            wd = day.weekday() < 5
            rate = pc / hc if wd and hc > 0 else None; rates.append(rate)
            win = [r for r in rates[-7:] if r is not None]
            rows.append({"date": ds, "is_working_day": wd, "headcount": hc, "present_count": hu(pc, 2), "late_count": sum(l["late_minutes"] > 0 for l in ls),
                         "attendance_rate": hu(rate, 4) if rate is not None else None, "moving_avg_7d": hu(sum(win) / len(win), 4) if win else None})
        got = c.get(f"/analytics/departments/{dept}/trend", params={"from": f, "to": t}).json()
        check(f"trend {dept} {f}..{t}", got, {"department": dept, "items": rows})
    assert c.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-10-05"}).status_code == 422

    if "--big" in sys.argv:
        big, d = [], date(2025, 1, 1)
        docs = [{**emps[0], "emp_code": f"EMP9{e:03d}", "department": f"D{e % 8}"} for e in range(300)]
        for x in docs: x.pop("_id", None)
        db.employees.insert_many(docs)
        for e in range(300):
            for i in range(330):
                big.append({"emp_code": f"EMP9{e:03d}", "date": (d + timedelta(i)).isoformat(), "status": "PRESENT", "punch_in": datetime(2026, 1, 1, tzinfo=timezone.utc),
                            "punch_out": None, "work_hours": 8.0, "late_minutes": random.choice([0, 0, 15]), "overtime_minutes": 0, "half_day": False, "history": []})
        db.attendance_logs.insert_many(big); print("loaded", db.attendance_logs.estimated_document_count(), "logs")
        for ep, p in (("attendance_list", {}), ("attendance_list", {"emp_code": "EMP9001", "date_from": "2025-02-01"}), ("attendance_list", {"status": "PRESENT", "page": 3}),
                      ("employee_monthly", {"emp_code": "EMP9001", "month": "2025-03"}), ("department_summary", {"month": "2025-03"}),
                      ("department_summary", {"month": "2025-03", "department": "D1"}), ("late_leaderboard", {"month": "2025-03"}),
                      ("department_trend", {"department": "D1", "from": "2025-03-01", "to": "2025-05-30"})):
            r = c.get(f"/admin/explain/{ep}", params=p); txt = json.dumps(r.json())
            ok = r.status_code == 200 and "IXSCAN" in txt and "COLLSCAN" not in txt
            print(("OK  " if ok else "FAIL"), ep, p); failures += (not ok)
        import time
        for path, p in (("/attendance", {}), ("/analytics/departments/summary", {"month": "2025-03"}), ("/analytics/leaderboard/late", {"month": "2025-03"}),
                        ("/analytics/departments/D1/trend", {"from": "2025-03-01", "to": "2025-05-30"})):
            t0 = time.time(); c.get(path, params=p); print(f"{path:45s} {1000*(time.time()-t0):7.0f} ms")
print("ALL ANALYTICS MATCH THE ORACLE" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
