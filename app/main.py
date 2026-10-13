"""FastAPI routes and MongoDB queries for employee attendance."""
import calendar
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False (default): real environment variables take priority over .env

log = logging.getLogger("attendance")

# Keep MongoDB datetimes timezone-aware so time comparisons stay consistent.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
    maxPoolSize=100,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
NON_PRESENCE = ("ABSENT", "LEAVE")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

# (collection, keys, options). Index choices are explained in DECISIONS.md.
INDEXES = [
    ("employees", [("emp_code", 1)], {"unique": True, "name": "uniq_emp_code"}),
    ("employees", [("department", 1), ("emp_code", 1)], {"name": "dept_emp_code"}),
    ("employees", [("department", 1), ("joined_on", 1)], {"name": "dept_joined_on"}),
    ("employees", [("joined_on", 1)], {"name": "joined_on"}),
    ("attendance_logs", [("emp_code", 1), ("date", 1)], {"unique": True, "name": "uniq_emp_date"}),
    ("attendance_logs", [("date", -1), ("emp_code", 1)], {"name": "date_emp"}),
    ("attendance_logs", [("status", 1), ("date", -1), ("emp_code", 1)], {"name": "status_date_emp"}),
    ("attendance_logs", [("emp_code", 1), ("punch_in", -1)], {"name": "emp_punch_in"}),
]
_idx_lock = threading.Lock()
_indexes_ready = False


def ensure_indexes() -> bool:
    """Idempotent. Returns False if MongoDB is not reachable (the caller may retry later)."""
    global _indexes_ready
    with _idx_lock:
        if _indexes_ready:
            return True
        try:
            client.admin.command("ping")
        except PyMongoError:
            return False
        for coll, keys, opts in INDEXES:
            try:
                db[coll].create_index(keys, **opts)
            except PyMongoError as exc:  # e.g. pre-existing duplicates: keep serving, but say so
                log.error("could not create index %s on %s: %s", opts.get("name"), coll, exc)
        _indexes_ready = True
        return True


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.exception("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Generic helpers
# --------------------------------------------------------------------------- #
def to_utc(dt: datetime) -> datetime:
    """Always work with aware UTC datetimes (naive ones are assumed to be UTC, which is what BSON stores)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else (to_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def now_dt() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def round_half_up(value: Any, places: int) -> Optional[float]:
    """Half-up rounding (R8). Rounds to 12 places first to wipe binary-float noise such as 0.12344999999999."""
    if value is None:
        return None
    quant = Decimal(1).scaleb(-places)
    return float(Decimal(repr(round(float(value), 12))).quantize(quant, rounding=ROUND_HALF_UP))


def validation_error(where: str, name: str, msg: str) -> RequestValidationError:
    return RequestValidationError([{"type": "value_error", "loc": (where, name), "msg": msg, "input": None}])


_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def parse_date(value: str, name: str, where: str = "query") -> Date:
    """Strict YYYY-MM-DD (rejects '2026-7-6', '20260706' and impossible days such as 2026-02-30)."""
    if not _DATE_RE.match(value):
        raise validation_error(where, name, "must be a date in YYYY-MM-DD format")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise validation_error(where, name, "not a valid calendar date")


def month_bounds(month: str) -> tuple[Date, Date]:
    if not _MONTH_RE.match(month):
        raise validation_error("query", "month", "must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:])
    return Date(y, m, 1), Date(y, m, calendar.monthrange(y, m)[1])


def count_weekdays(start: Date, end: Date) -> int:
    """Number of Mon-Fri days in [start, end] (R7). Pure calendar arithmetic, no data involved."""
    if end < start:
        return 0
    n = (end - start).days + 1
    full, rem = divmod(n, 7)
    extra = sum(1 for i in range(rem) if (start + timedelta(days=i)).weekday() < 5)
    return full * 5 + extra


# --------------------------------------------------------------------------- #
# 3. Business rules R1-R5 (pure functions)
# --------------------------------------------------------------------------- #
def _hhmm(s: str) -> time:
    return time(int(s[:2]), int(s[3:5]))


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]  # zero-padded HH:MM compares correctly as text


def ist_instant(day: str, hhmm: str, plus_days: int = 0) -> datetime:
    return datetime.combine(Date.fromisoformat(day) + timedelta(days=plus_days), _hhmm(hhmm), tzinfo=IST)


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch belongs to the previous day."""
    ist = to_utc(punch_in).astimezone(IST)
    day = ist.date()
    if is_overnight(emp) and ist.time() < _hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def calc_late_minutes(punch_in: datetime, day: str, emp: dict) -> int:
    """R2: strictly more than 10:00 after shift start; then whole minutes since shift start."""
    delta = int((to_utc(punch_in) - ist_instant(day, emp["shift_start"])).total_seconds())
    return delta // 60 if delta > 600 else 0


def calc_overtime_minutes(punch_out: datetime, day: str, emp: dict) -> int:
    """R3: whole minutes after shift end, only if >= 30. Overnight shifts end on the next calendar day."""
    end = ist_instant(day, emp["shift_end"], 1 if is_overnight(emp) else 0)
    minutes = int((to_utc(punch_out) - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def calc_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up, using exact decimal arithmetic."""
    secs = int((to_utc(punch_out) - to_utc(punch_in)).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive(emp: dict, status: str, day: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All four derived fields for a record in its final state (used by punch-in, punch-out and PATCH)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = calc_late_minutes(punch_in, day, emp)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = calc_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": calc_overtime_minutes(punch_out, day, emp),
        "half_day": Decimal(str(hours)) < Decimal("4.50"),  # R5: on the ROUNDED value
    }


# --------------------------------------------------------------------------- #
# 4. Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not _DATE_RE.match(v):
            raise ValueError("must be YYYY-MM-DD")
        Date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _history_value(v: Any) -> Any:
    return dt_to_ms(v) if isinstance(v, datetime) else v


def history_out(entry: dict) -> dict:
    return {
        "at": dt_to_ms(entry["at"]),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            k: {"from": _history_value(c.get("from")), "to": _history_value(c.get("to"))}
            for k, c in (entry.get("changes") or {}).items()
        },
    }


def record_out(doc: dict) -> dict:
    """Missing legacy fields are defaulted; _id never leaves the service; instants become epoch ms."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
MonthParam = Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]


# --------------------------------------------------------------------------- #
# 5a. System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    ensure_indexes()  # no-op once done; heals the case where Mongo came up after the app
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_dt()
    try:
        db.employees.insert_one(dict(doc))  # uniqueness is enforced by the unique index, not by a prior read
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q: dict = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 5b. Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, body.status, day, ts, None),
        "history": [],
    }
    try:
        # Race-safe: the unique (emp_code, date) index decides the winner; losers get DuplicateKeyError.
        db.attendance_logs.insert_one(dict(doc))
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    for _ in range(3):
        rec = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, {"_id": 0}, sort=[("punch_in", -1)]
        )
        if rec is None:
            raise HTTPException(404, "no punch-in found")
        if rec.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        pin = to_utc(rec["punch_in"])
        if ts <= pin:
            raise HTTPException(422, "punched_at must be after punch_in")
        if ts - pin > timedelta(hours=24):
            raise HTTPException(422, "punch_out is more than 24 hours after punch_in")
        derived = derive(emp, rec["status"], rec["date"], pin, ts)
        # Atomic compare-and-set: only the request that still sees an open record (with the punch_in we
        # computed from) can win. A concurrent punch-out or correction makes this match nothing.
        res = db.attendance_logs.update_one(
            {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None, "punch_in": rec["punch_in"]},
            {"$set": {"punch_out": ts, **derived}},
        )
        if res.matched_count == 1:
            rec.update({"punch_out": ts, **derived})
            return record_out(rec)
        # lost a race: re-read, which turns into a clean 409 if somebody else closed it
    raise HTTPException(409, "record was modified concurrently, please retry")


def build_attendance_find(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str],
    page: int, page_size: int,
) -> dict:
    """Shared by GET /attendance and its explain, so the explained query is the real one."""
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise validation_error("query", "date_from", "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    # With emp_code fixed, "date desc, emp_code asc" is just "date desc", which lets the
    # (emp_code, date) index serve the sort without an in-memory SORT stage.
    sort = {"date": -1} if emp_code else {"date": -1, "emp_code": 1}
    return {"filter": q, "sort": sort, "skip": (page - 1) * page_size, "limit": page_size}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    spec = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
    coll = db.attendance_logs
    total = coll.count_documents(spec["filter"]) if spec["filter"] else coll.estimated_document_count()
    cur = coll.find(spec["filter"], {"_id": 0}).sort(list(spec["sort"].items())).skip(spec["skip"]).limit(spec["limit"])
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def plan_regularization(old: dict, emp: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Returns (new field values, history changes) or raises 422. Pure: no database access."""
    day = old["date"]
    new_status = body.status or old["status"]
    if new_status in NON_PRESENCE:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"{new_status} clears punch times; do not send punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old.get("punch_in")
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        new_in = to_utc(new_in)
        if attendance_date(new_in, emp) != day:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old.get("punch_out")
        if new_out is not None:
            new_out = to_utc(new_out)
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derive(emp, new_status, day, new_in, new_out)}
    before = {
        "status": old["status"],
        "punch_in": to_utc(old["punch_in"]) if old.get("punch_in") else None,
        "punch_out": to_utc(old["punch_out"]) if old.get("punch_out") else None,
        "work_hours": old.get("work_hours"),
        "late_minutes": old.get("late_minutes") or 0,
        "overtime_minutes": old.get("overtime_minutes") or 0,
        "half_day": bool(old.get("half_day", False)),
    }
    if all(new[k] == before[k] for k in ("status", "punch_in", "punch_out")):
        raise HTTPException(422, "the request changes nothing")
    changes = {k: {"from": before[k], "to": new[k]} for k in FIELDS if new[k] != before[k]}
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date, "date", where="path").isoformat()
    emp = get_employee_or_404(emp_code)
    for _ in range(5):
        old = db.attendance_logs.find_one({"emp_code": emp_code, "date": day}, {"_id": 0})
        if old is None:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = plan_regularization(old, emp, body)
        entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        old_history = old.get("history") or []
        # Optimistic concurrency: the write only applies if status, punches and history length are still what
        # we planned from. A concurrent punch-out / PATCH makes it match nothing and we re-plan on fresh data,
        # so no history entry is ever lost and no stale derived value is ever written.
        guard: dict = {
            "emp_code": emp_code, "date": day,
            "status": old["status"], "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out"),
        }
        if old_history:
            guard["history"] = {"$size": len(old_history)}
        else:
            guard["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}]
        res = db.attendance_logs.update_one(guard, {"$set": new, "$push": {"history": entry}})
        if res.matched_count == 1:
            return record_out({**old, **new, "history": old_history + [entry]})
    raise HTTPException(409, "record is being modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# 6. Analytics: pipeline builders (shared with the explain endpoint)
# --------------------------------------------------------------------------- #
_DOW = {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}  # date strings are calendar dates


def _present_weight(status: str = "$status", half: str = "$half_day") -> dict:
    """1 for a present record, 0.5 for a present half day (R5/R6), 0 otherwise."""
    return {"$cond": [{"$in": [status, list(PRESENCE)]}, {"$cond": [{"$ifNull": [half, False]}, 0.5, 1]}, 0]}


def _month_stats_group(group_id: Any) -> dict:
    """One $group computing every per-month measure from STORED derived values (never recomputed)."""
    present = {"$in": ["$status", list(PRESENCE)]}
    has_hours = {"$and": [present, {"$isNumber": "$work_hours"}]}
    late = {"$ifNull": ["$late_minutes", 0]}
    return {"$group": {
        "_id": group_id,
        "present_days": {"$sum": {"$cond": [{"$lte": [_DOW, 5]}, _present_weight(), 0]}},  # Mon-Fri only (R7)
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": late},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        # work hours are summed as integer hundredths so the later average is exact decimal arithmetic
        "hours_cents": {"$sum": {"$cond": [has_hours, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}},
        "hours_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
    }}


def _date_range(first: Date, last: Date) -> dict:
    return {"$gte": first.isoformat(), "$lte": last.isoformat()}


def build_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [{"$match": {"emp_code": emp_code, "date": _date_range(first, last)}}, _month_stats_group(None)]


def build_summary_pipeline(month: str, department: Optional[str]) -> list:
    """Starts from employees (so people without logs are counted) and joins their month of logs."""
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": _date_range(first, last)}}, _month_stats_group(None)],
            "as": "s",
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},  # keeps zero-log employees
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$s.present_days"},
            "hours_cents": {"$sum": "$s.hours_cents"},
            "hours_n": {"$sum": "$s.hours_n"},
            "late_count": {"$sum": "$s.late_count"},
            "total_late_minutes": {"$sum": "$s.total_late_minutes"},
            "leave_count": {"$sum": "$s.leave_count"},
            "on_duty_count": {"$sum": "$s.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": _date_range(first, last), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4), computed by the database over the total only.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def build_trend_pipeline(department: str, d_from: Date, d_to: Date) -> list:
    """
    One row per calendar day, generated inside MongoDB ($range), so empty days and weekends exist by construction.
    Two branches are merged by day: (a) the day series with headcount, (b) per-day counts from the department's logs.
    """
    n_days = (d_to - d_from).days + 1
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    day_series = [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "joined": {"$push": "$joined_on"}}},
        {"$project": {"_id": 0, "joined": 1, "days": {"$map": {
            "input": {"$range": [0, n_days]}, "as": "n",
            "in": {"$dateToString": {"format": "%Y-%m-%d",
                                     "date": {"$add": [start, {"$multiply": ["$$n", 86_400_000]}]}}},
        }}}},
        {"$unwind": "$days"},
        {"$project": {"date": "$days", "present_count": {"$literal": 0}, "late_count": {"$literal": 0},
                      "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$days"]}}}}}},  # R9
    ]
    return [
        {"$match": {"department": department}},
        {"$lookup": {  # only this department's logs, via the (emp_code, date) index
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": _date_range(d_from, d_to)}},
                         {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}}],
            "as": "logs",
        }},
        {"$unwind": "$logs"},
        {"$group": {
            "_id": "$logs.date",
            "present_count": {"$sum": _present_weight("$logs.status", "$logs.half_day")},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1, "headcount": {"$literal": 0}}},
        {"$unionWith": {"coll": "employees", "pipeline": day_series}},
        {"$group": {"_id": "$date", "headcount": {"$max": "$headcount"},
                    "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$sort": {"_id": 1}},
        {"$set": {"date": "$_id", "is_working_day": {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}, 5]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                                                {"$divide": ["$present_count", "$headcount"]}, None]}}},
        # trailing 7-row window (current + 6 preceding), restricted to the requested range; $avg skips nulls
        {"$setWindowFields": {"sortBy": {"date": 1},
                              "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                      "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


def validate_trend_range(from_: str, to: str) -> tuple[Date, Date]:
    d_from, d_to = parse_date(from_, "from"), parse_date(to, "to")
    if d_to < d_from:
        raise validation_error("query", "to", "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > 92:
        raise validation_error("query", "to", "range must not exceed 92 days")
    return d_from, d_to


# --------------------------------------------------------------------------- #
# 6b. Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthParam):
    first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    working_days = count_weekdays(max(first, Date.fromisoformat(emp["joined_on"])), last)  # R7
    stats = next(db.attendance_logs.aggregate(build_monthly_pipeline(emp_code, month)), None) or {}
    present = stats.get("present_days", 0) or 0
    pct = round_half_up(present / working_days * 100, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": int(stats.get("leave_count", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthParam, department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(build_summary_pipeline(month, department)):
        n = int(r.get("hours_n", 0))
        avg = None
        if n:
            avg = float((Decimal(int(r["hours_cents"])) / Decimal(n) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r.get("present_days", 0), 2),
            "avg_work_hours": avg,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthParam, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    rows = db.attendance_logs.aggregate(build_leaderboard_pipeline(month, limit, department))
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in rows
    ]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[str, Query(alias="from")], to: str):
    d_from, d_to = validate_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")
    items = []
    for r in db.employees.aggregate(build_trend_pipeline(department, d_from, d_to)):
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(r.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(r.get("moving_avg_7d"), 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 7. Explain
# --------------------------------------------------------------------------- #
def _need(**params: Any) -> None:
    for name, value in params.items():
        if value is None:
            raise validation_error("query", name, "field required")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        spec = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
        coll, command = "attendance_logs", {"find": "attendance_logs", **spec}
    else:
        if endpoint == "employee_monthly":
            _need(emp_code=emp_code, month=month)
            coll, pipeline = "attendance_logs", build_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            _need(month=month)
            coll, pipeline = "employees", build_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            _need(month=month)
            coll, pipeline = "attendance_logs", build_leaderboard_pipeline(month, limit, department)
        else:
            _need(department=department, **{"from": from_}, to=to)
            d_from, d_to = validate_trend_range(from_, to)
            coll, pipeline = "employees", build_trend_pipeline(department, d_from, d_to)
        command = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    # explain output can contain BSON types (Timestamp, Binary, ...): convert to plain JSON
    safe = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": coll, "explain": safe}

