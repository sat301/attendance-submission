"""Local tests: pure rule helpers + CRUD endpoints against mongomock (no real MongoDB needed)."""
import sys, threading
from datetime import datetime, timezone
import mongomock, pytest
import os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient
from app import main as m

UTC = timezone.utc
def ms(y, mo, d, h, mi, s=0, ms_=0, tz=m.IST):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=tz).timestamp()) * 1000 + ms_

@pytest.fixture()
def api():
    mc = mongomock.MongoClient(tz_aware=True)
    m.db = mc["t"]
    m.db.employees.create_index("emp_code", unique=True)
    m.db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    return TestClient(m.app, raise_server_exceptions=False)

DAY = {"emp_code": "EMP0001", "name": "A", "email": "a@x.io", "department": "Eng", "joined_on": "2026-01-05"}
NIGHT = {**DAY, "emp_code": "EMP0005", "shift_start": "22:00", "shift_end": "06:00"}

# ---------- pure rules ----------
EMP = {"shift_start": "09:30", "shift_end": "18:30"}
NEMP = {"shift_start": "22:00", "shift_end": "06:00"}
def dt(h, mi, s=0, day=6): return datetime(2026, 7, day, h, mi, s, tzinfo=m.IST)

def test_late_boundaries():
    f = lambda h, mi, s: m.calc_late_minutes(dt(h, mi, s), "2026-07-06", EMP)
    assert f(9, 40, 0) == 0 and f(9, 40, 1) == 10 and f(10, 15, 59) == 45 and f(9, 0, 0) == 0

def test_overtime_boundaries():
    f = lambda h, mi, s: m.calc_overtime_minutes(dt(h, mi, s), "2026-07-06", EMP)
    assert f(18, 59, 59) == 0 and f(19, 0, 0) == 30 and f(19, 10, 0) == 40 and f(17, 0, 0) == 0

def test_overnight():
    assert m.attendance_date(dt(21, 55), NEMP) == "2026-07-06"
    assert m.attendance_date(dt(5, 59, 59, day=7), NEMP) == "2026-07-06"
    assert m.attendance_date(dt(6, 0, 0, day=7), NEMP) == "2026-07-07"
    assert m.calc_overtime_minutes(dt(6, 40, day=7), "2026-07-06", NEMP) == 40
    assert m.calc_late_minutes(dt(21, 55), "2026-07-06", NEMP) == 0

def test_work_hours_half_up_and_half_day():
    pin = dt(9, 0)
    # 4.495 h would be a tie only in theory; 16182 s = 4.495 h -> 4.50 (half-up) -> NOT half day
    assert m.calc_work_hours(pin, datetime(2026, 7, 6, 9, 0, tzinfo=m.IST).replace(second=0) + __import__("datetime").timedelta(seconds=16182)) == 4.5
    d = m.derive(EMP, "PRESENT", "2026-07-06", pin, pin + __import__("datetime").timedelta(seconds=16182))
    assert d["half_day"] is False
    d = m.derive(EMP, "WFH", "2026-07-06", pin, pin + __import__("datetime").timedelta(hours=3.5))
    assert d["half_day"] is True and d["work_hours"] == 3.5
    assert m.calc_work_hours(pin, pin + __import__("datetime").timedelta(seconds=3)) == 0.0   # 0.000833 -> 0.00
    assert m.calc_work_hours(pin, pin + __import__("datetime").timedelta(seconds=18)) == 0.01  # 0.005 -> 0.01 (half-up)

def test_round_half_up():
    assert m.round_half_up(0.12345, 4) == 0.1235 and m.round_half_up(2.675, 2) == 2.68 and m.round_half_up(None, 2) is None

def test_weekdays():
    from datetime import date
    assert m.count_weekdays(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert m.count_weekdays(date(2026, 7, 13), date(2026, 7, 31)) == 15
    assert m.count_weekdays(date(2026, 8, 1), date(2026, 7, 31)) == 0

# ---------- employees ----------
def test_employee_create_validation_and_conflict(api):
    r = api.post("/employees", json=DAY); assert r.status_code == 201
    b = r.json(); assert b["shift_start"] == "09:30" and isinstance(b["created_at"], int) and "_id" not in b
    assert api.post("/employees", json=DAY).status_code == 409
    for bad in ({**DAY, "emp_code": "X1"}, {**DAY, "emp_code": "EMP0002", "email": "nope"},
                {**DAY, "emp_code": "EMP0002", "shift_start": "9:30"}, {**DAY, "emp_code": "EMP0002", "joined_on": "2026-02-30"},
                {**DAY, "emp_code": "EMP0002", "shift_start": "09:30", "shift_end": "09:30"},
                {**DAY, "emp_code": "EMP0002", "name": ""}):
        assert api.post("/employees", json=bad).status_code == 422, bad

def test_employee_list_pagination(api):
    for i in range(1, 6):
        api.post("/employees", json={**DAY, "emp_code": f"EMP000{i}", "department": "A" if i < 4 else "B"})
    r = api.get("/employees", params={"department": "A", "page": 2, "page_size": 2}).json()
    assert r["total"] == 3 and [e["emp_code"] for e in r["items"]] == ["EMP0003"]
    r = api.get("/employees", params={"page_size": 100}).json()
    assert r["total"] == 5 and [e["emp_code"] for e in r["items"]] == [f"EMP000{i}" for i in range(1, 6)]
    assert api.get("/employees", params={"page": 0}).status_code == 422
    assert api.get("/employees", params={"page_size": 101}).status_code == 422

# ---------- punch-in / out ----------
def test_punch_in_rules(api):
    api.post("/employees", json=DAY)
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 40, 1, 900)})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 10 and b["punch_in"] == ms(2026, 7, 6, 9, 40, 1) \
        and b["history"] == [] and b["work_hours"] is None and "id" not in b
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 12, 0)}).status_code == 409
    assert api.post("/attendance/punch-in", json={"emp_code": "NOPE1"}).status_code == 404
    for bad in ({"emp_code": "EMP0001", "punched_at": 1783312500}, {"emp_code": "EMP0001", "punched_at": 1.7e12},
                {"emp_code": "EMP0001", "punched_at": "1783312500000"}, {"emp_code": "EMP0001", "status": "ABSENT"},
                {"emp_code": "EMP0001", "punched_at": 0}):
        assert api.post("/attendance/punch-in", json=bad).status_code == 422, bad

def test_punch_in_date_is_ist_not_utc(api):
    api.post("/employees", json=DAY)
    # 00:10 IST on the 7th is still the 6th in UTC
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 0, 10)})
    assert r.json()["date"] == "2026-07-07"

def test_overnight_flow(api):
    api.post("/employees", json=NIGHT)
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)}).json()
    assert r["date"] == "2026-07-06" and r["late_minutes"] == 0
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)})
    assert r.status_code == 200, r.text
    b = r.json(); assert b["work_hours"] == 8.75 and b["overtime_minutes"] == 40 and b["half_day"] is False and b["date"] == "2026-07-06"
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 50)}).status_code == 409

def test_punch_out_errors(api):
    api.post("/employees", json=DAY)
    assert api.post("/attendance/punch-out", json={"emp_code": "NOPE1"}).status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}).status_code == 404
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)})
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}).status_code == 422   # equal
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 0, 1)}).status_code == 422  # >24h
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 12, 30)}).status_code == 200

def test_concurrent_punch_in_and_out(api):
    api.post("/employees", json=DAY)
    res = []
    def go(): res.append(api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}).status_code)
    ts = [threading.Thread(target=go) for _ in range(12)]; [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(res) == [201] + [409] * 11
    res.clear()
    def out(): res.append(api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 0)}).status_code)
    ts = [threading.Thread(target=out) for _ in range(12)]; [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(res) == [200] + [409] * 11

# ---------- list ----------
def test_attendance_list(api):
    api.post("/employees", json=DAY); api.post("/employees", json={**DAY, "emp_code": "EMP0002"})
    for d in (6, 7, 8):
        for e in ("EMP0002", "EMP0001"):
            api.post("/attendance/punch-in", json={"emp_code": e, "punched_at": ms(2026, 7, d, 9, 0), "status": "WFH" if d == 7 else "PRESENT"})
    r = api.get("/attendance").json()
    assert r["total"] == 6 and [(i["date"], i["emp_code"]) for i in r["items"]][:2] == [("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002")]
    r = api.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-07", "date_to": "2026-07-08"}).json()
    assert r["total"] == 2 and [i["date"] for i in r["items"]] == ["2026-07-08", "2026-07-07"]
    r = api.get("/attendance", params={"status": "WFH", "page_size": 1, "page": 2}).json()
    assert r["total"] == 2 and len(r["items"]) == 1
    assert api.get("/attendance", params={"date_from": "2026-07-09", "date_to": "2026-07-01"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "bad"}).status_code == 422
    assert api.get("/attendance", params={"status": "NOPE"}).status_code == 422

# ---------- PATCH ----------
def test_regularize(api):
    api.post("/employees", json=DAY)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 18, 30)})
    base = {"reason": "biometric glitch", "regularized_by": "hr.admin"}
    r = api.patch("/attendance/EMP0001/2026-07-07", json={**base, "punch_in": ms(2026, 7, 7, 9, 28), "work_hours": 99})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["late_minutes"] == 0 and b["work_hours"] == 9.03 and len(b["history"]) == 1
    ch = b["history"][0]["changes"]
    assert set(ch) == {"punch_in", "late_minutes", "work_hours"}, ch
    assert ch["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)} and ch["late_minutes"] == {"from": 35, "to": 0}
    assert b["history"][0]["by"] == "hr.admin" and isinstance(b["history"][0]["at"], int)
    # no-op, validation errors, 404s
    assert api.patch("/attendance/EMP0001/2026-07-07", json={**base, "punch_in": ms(2026, 7, 7, 9, 28)}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-07", json={**base, "status": "LEAVE", "punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-07", json={**base, "punch_in": ms(2026, 7, 8, 9, 0)}).status_code == 422   # other day
    assert api.patch("/attendance/EMP0001/2026-07-07", json={**base, "punch_out": ms(2026, 7, 7, 9, 0)}).status_code == 422   # before in
    assert api.patch("/attendance/EMP0001/2026-07-07", json={"reason": "abc", "regularized_by": "x"}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-09", json={**base, "status": "LEAVE"}).status_code == 404
    assert api.patch("/attendance/NOPE9/2026-07-07", json={**base, "status": "LEAVE"}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-13-01", json={**base, "status": "LEAVE"}).status_code == 422
    # to LEAVE: clears everything, second history entry appended (not overwritten)
    r = api.patch("/attendance/EMP0001/2026-07-07", json={**base, "status": "LEAVE"}).json()
    assert r["punch_in"] is None and r["punch_out"] is None and r["work_hours"] is None and r["half_day"] is False and len(r["history"]) == 2
    assert api.patch("/attendance/EMP0001/2026-07-07", json={**base, "status": "PRESENT"}).status_code == 422   # no punch_in
    r = api.patch("/attendance/EMP0001/2026-07-07", json={**base, "status": "WFH", "punch_in": ms(2026, 7, 7, 9, 0), "punch_out": ms(2026, 7, 7, 12, 0)}).json()
    assert r["status"] == "WFH" and r["half_day"] is True and r["work_hours"] == 3.0 and len(r["history"]) == 3

def test_regularize_legacy_doc_and_concurrency(api):
    api.post("/employees", json=DAY)
    m.db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
        "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC), "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=UTC), "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    assert api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]["history"] == []
    res = []
    def go(i): res.append(api.patch("/attendance/EMP0001/2026-07-14", json={"reason": f"fix number {i}", "regularized_by": "hr", "punch_out": ms(2026, 7, 14, 17, i)}).status_code)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]; [t.start() for t in ts]; [t.join() for t in ts]
    n_ok = res.count(200)
    hist = m.db.attendance_logs.find_one({"emp_code": "EMP0001"})["history"]
    assert n_ok >= 1 and len(hist) == n_ok, (res, len(hist))
