"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is read for convenience; real environment variables win)

Layout of this file
  1. Configuration, DB handle, index creation
  2. Time helpers (IST, epoch ms) and the business rules R1-R5
  3. Request models (validation)
  4. Serialisation helpers
  5. Query / pipeline builders (shared by the real endpoints AND /admin/explain)
  6. Endpoints
"""
import json
import os
import re
import threading
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration, DB handle, indexes
# --------------------------------------------------------------------------- #
load_dotenv()  # does NOT override variables that are already set in the environment

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive ones,
# so we can never confuse "local naive" with "UTC" by accident.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
ALL_STATUS = ["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
OFF_STATUS = ("ABSENT", "LEAVE")

# Index names are explicit so queries can be hinted by name (see section 5).
IDX_EMP_UNIQUE = "uq_emp_code"
IDX_EMP_DEPT = "dept_emp_code"
IDX_EMP_JOINED = "joined_on"
IDX_LOG_UNIQUE = "uq_emp_date"
IDX_LOG_DATE = "date_emp"

_index_lock = threading.Lock()
_indexes_ready = False


def ensure_indexes() -> bool:
    """Create every index idempotently. Safe to call many times / from many threads."""
    global _indexes_ready
    if _indexes_ready:
        return True
    with _index_lock:
        if _indexes_ready:
            return True
        try:
            db.employees.create_index([("emp_code", ASCENDING)], unique=True, name=IDX_EMP_UNIQUE)
            db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name=IDX_EMP_DEPT)
            db.employees.create_index([("joined_on", ASCENDING)], name=IDX_EMP_JOINED)
            db.attendance_logs.create_index(
                [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name=IDX_LOG_UNIQUE
            )
            db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name=IDX_LOG_DATE)
            _indexes_ready = True
        except PyMongoError as exc:  # DB not reachable yet: /health will keep retrying
            print(f"[startup] could not create indexes yet: {exc}")
        return _indexes_ready


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def _startup() -> None:
    ensure_indexes()


# --------------------------------------------------------------------------- #
# 2. Time helpers and business rules
# --------------------------------------------------------------------------- #
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000  # from openapi.yaml (EpochMillis)


def ms_to_dt(ms: int) -> datetime:
    """epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_utc_ms() -> datetime:
    """'Now' with millisecond precision, i.e. exactly what MongoDB can store."""
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:  # defensive: a naive value from MongoDB is always UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]  # zero-padded "HH:MM" compares correctly as text


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST calendar date of the punch-in; for an overnight shift, a punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and local.time() < hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day


def shift_start_at(day: date, emp: dict) -> datetime:
    return datetime.combine(day, hhmm(emp["shift_start"]), tzinfo=IST)


def shift_end_at(day: date, emp: dict) -> datetime:
    end = datetime.combine(day, hhmm(emp["shift_end"]), tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(emp) else end


def late_minutes_for(punch_in: datetime, day: date, emp: dict) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then floor(minutes since shift start)."""
    seconds = int((punch_in - shift_start_at(day, emp)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def overtime_minutes_for(punch_out: datetime, day: date, emp: dict) -> int:
    """R3: floor(minutes after shift end), counted only if >= 30."""
    seconds = int((punch_out - shift_end_at(day, emp)).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= 30 else 0


def half_up(value: Any, places: int) -> Optional[float]:
    """Round half-up (not banker's rounding, not float rounding). Works through the shortest decimal repr."""
    if value is None:
        return None
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def work_hours_for(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up (done in Decimal: exact)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derived_fields(emp: dict, day: date, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """The four API-computed fields for a presence record (see DATA_MODEL.md table)."""
    if punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": late_minutes_for(punch_in, day, emp),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        wh = work_hours_for(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = overtime_minutes_for(punch_out, day, emp)
        out["half_day"] = wh < 4.50  # R5: uses the ROUNDED work_hours
    return out


# --------------------------------------------------------------------------- #
# 3. Request models and parameter validation
# --------------------------------------------------------------------------- #
# strict=True: floats ("1.7e12"), strings and booleans are rejected with 422 instead of being coerced.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]
TIME_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"  # [0-9], not \d: \d would also match non-ASCII digits
DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


def fail(loc: tuple, msg: str, value: Any = None):
    """Raise a FastAPI-style 422 (detail is a list, same as automatic validation errors)."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


def parse_date(value: str, loc: tuple) -> date:
    if not re.fullmatch(DATE_RE, value or ""):
        fail(loc, "must be a date formatted YYYY-MM-DD", value)
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail(loc, "not a real calendar date", value)


def parse_month(value: str, loc: tuple = ("query", "month")) -> tuple[date, date]:
    if not re.fullmatch(MONTH_RE, value or ""):
        fail(loc, "must be a month formatted YYYY-MM", value)
    year, month = int(value[:4]), int(value[5:])
    try:
        first = date(year, month, 1)
    except ValueError:
        fail(loc, "year out of range", value)
    return first, date(year, month, monthrange(year, month)[1])


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_RE)
    shift_end: str = Field(default="18:30", pattern=TIME_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        if not re.fullmatch(DATE_RE, v):
            raise ValueError("must be YYYY-MM-DD")
        date.fromisoformat(v)  # raises ValueError for 2026-02-30
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    # Derived fields (work_hours, ...) are not declared: pydantic ignores unknown keys, as the contract asks.


PageQ = Annotated[int, Query(ge=1)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]
MonthQ = Annotated[str, Query(pattern=MONTH_RE)]


# --------------------------------------------------------------------------- #
# 4. Serialisation helpers (BSON -> API shape)
# --------------------------------------------------------------------------- #
def to_api_value(v: Any) -> Any:
    return dt_to_ms(v) if isinstance(v, datetime) else v


def history_out(entry: dict) -> dict:
    changes = {
        field: {"from": to_api_value(c.get("from")), "to": to_api_value(c.get("to"))}
        for field, c in (entry.get("changes") or {}).items()
    }
    return {"at": to_api_value(entry["at"]), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Attendance document -> API record. Never exposes _id; fills in the 'missing means default' fields."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_api_value(doc.get("punch_in")),
        "punch_out": to_api_value(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_api_value(doc.get("created_at")),
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 5. Query / pipeline builders. The endpoints AND /admin/explain both call these,
#    so the explained query is by construction the one the endpoint runs.
# --------------------------------------------------------------------------- #
class Plan:
    """A MongoDB operation: either a find (filter/sort/skip/limit) or an aggregate (pipeline)."""

    def __init__(self, collection: str, *, pipeline=None, find=None, hint: Optional[str] = None):
        self.collection = collection
        self.pipeline = pipeline
        self.find = find  # dict(filter=, sort=[(f, dir)], skip=, limit=)
        self.hint = hint

    def explain_command(self) -> dict:
        if self.pipeline is not None:
            spec = {"aggregate": self.collection, "pipeline": self.pipeline, "cursor": {}}
        else:
            f = self.find
            spec = {
                "find": self.collection,
                "filter": f["filter"],
                "sort": dict(f["sort"]),
                "skip": f["skip"],
                "limit": f["limit"],
            }
        if self.hint:
            spec["hint"] = self.hint
        return {"explain": spec, "verbosity": "executionStats"}


# ---- reusable aggregation expressions (they read the STORED derived values, never recompute) ----
IS_PRESENT = {"$in": ["$status", PRESENCE]}
IS_WEEKDAY = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}  # 1 = Sunday
HALF_DAY = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
# "present units" are counted in half-days (full = 2, half = 1) so every sum is an exact integer;
# we divide by 2 only when formatting the response.
PRESENT_UNITS_ANY_DAY = {"$cond": [IS_PRESENT, {"$cond": [HALF_DAY, 1, 2]}, 0]}
PRESENT_UNITS_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, {"$cond": [HALF_DAY, 1, 2]}, 0]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MIN, 0]}, 1, 0]}
HAS_WH = {"$and": [IS_PRESENT, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
# work_hours in integer hundredths -> exact sums; avoids float drift when averaging.
WH_CENTS = {"$cond": [HAS_WH, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}


def weekdays_between(first: date, last: date) -> int:
    """Number of Mon-Fri days in [first, last] (R7). Calendar arithmetic, at most 31 iterations."""
    n, d = 0, first
    while d <= last:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def build_attendance_list(emp_code, date_from, date_to, status, page, page_size) -> Plan:
    f_from = parse_date(date_from, ("query", "date_from")) if date_from else None
    f_to = parse_date(date_to, ("query", "date_to")) if date_to else None
    if f_from and f_to and f_from > f_to:
        fail(("query", "date_from"), "date_from must not be after date_to", date_from)
    if status is not None and status not in ALL_STATUS:
        fail(("query", "status"), f"status must be one of {ALL_STATUS}", status)
    flt: dict = {}
    if emp_code:
        flt["emp_code"] = emp_code
    if date_from or date_to:
        flt["date"] = {}
        if date_from:
            flt["date"]["$gte"] = date_from
        if date_to:
            flt["date"]["$lte"] = date_to
    if status:
        flt["status"] = status
    return Plan(
        "attendance_logs",
        find=dict(filter=flt, sort=[("date", DESCENDING), ("emp_code", ASCENDING)],
                  skip=(page - 1) * page_size, limit=page_size),
        hint=IDX_LOG_UNIQUE if emp_code else IDX_LOG_DATE,
    )


def build_employee_monthly(emp_code: str, month: str) -> Plan:
    first, last = parse_month(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_units": {"$sum": PRESENT_UNITS_WEEKDAY},                      # R5-R7: Mon-Fri only
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": IS_LATE},
            "total_late_minutes": {"$sum": LATE_MIN},                              # weekends included
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return Plan("attendance_logs", pipeline=pipeline, hint=IDX_LOG_UNIQUE)


def build_department_summary(month: str, department: Optional[str]) -> Plan:
    first, last = parse_month(month)
    start, end = first.isoformat(), last.isoformat()
    emp_match: dict = {"joined_on": {"$lte": end}}  # R9: joined on/before the end of the month
    if department:
        emp_match["department"] = department
    pipeline = [
        # Start from EMPLOYEES so people with zero logs still produce a row (headcount).
        {"$match": emp_match},
        # Per-employee totals computed inside the join, so each employee yields 0 or 1 small doc.
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$group": {
                    "_id": None,
                    "present_units": {"$sum": PRESENT_UNITS_WEEKDAY},
                    "late_count": {"$sum": IS_LATE},
                    "total_late_minutes": {"$sum": LATE_MIN},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                    "wh_cents": {"$sum": WH_CENTS},
                    "wh_n": {"$sum": {"$cond": [HAS_WH, 1, 0]}},
                }},
            ],
            "as": "s",
        }},
        # The lookup's per-employee group is an array with zero or one item. Unwind it
        # before the department-level group so each accumulator receives scalar values.
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        # Department level: sum of sums (and sum of counts) => average over RECORDS, not average of averages.
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_units": {"$sum": "$s.present_units"},
            "late_count": {"$sum": "$s.late_count"},
            "total_late_minutes": {"$sum": "$s.total_late_minutes"},
            "leave_count": {"$sum": "$s.leave_count"},
            "on_duty_count": {"$sum": "$s.on_duty_count"},
            "wh_cents": {"$sum": "$s.wh_cents"},
            "wh_n": {"$sum": "$s.wh_n"},
        }},
        {"$sort": {"_id": 1}},
    ]
    # Hint so the employees scan is an index scan even when no department filter is present.
    return Plan("employees", pipeline=pipeline, hint=IDX_EMP_DEPT if department else IDX_EMP_JOINED)


def build_late_leaderboard(month: str, limit: int, department: Optional[str]) -> Plan:
    first, last = parse_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank INSIDE the department
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1,2,2,4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return Plan("attendance_logs", pipeline=pipeline, hint=IDX_LOG_DATE)


def build_department_trend(department: str, d_from: date, d_to: date) -> Plan:
    if d_to < d_from:
        fail(("query", "to"), "'to' must not be before 'from'", d_to.isoformat())
    if (d_to - d_from).days + 1 > 92:
        fail(("query", "to"), "range must not exceed 92 days", d_to.isoformat())
    codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})]
    if not codes:
        raise HTTPException(404, "department not found")

    start, end = d_from.isoformat(), d_to.isoformat()
    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    to_dt = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC)
    pipeline = [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": start, "$lte": end}}},
        {"$project": {"_id": 0, "d": {"$dateFromString": {"dateString": "$date"}},
                      "units": PRESENT_UNITS_ANY_DAY, "late": IS_LATE}},
        # One zero-valued seed row on the first day, so the pipeline is never empty even if the department
        # has no logs at all in the range ($densify then has something to grow from).
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}}, {"$limit": 1},
            {"$project": {"_id": 0, "d": {"$literal": from_dt}, "units": {"$literal": 0}, "late": {"$literal": 0}}},
        ]}},
        {"$group": {"_id": "$d", "units": {"$sum": "$units"}, "late_count": {"$sum": "$late"}}},
        {"$project": {"_id": 0, "d": "$_id", "units": 1, "late_count": 1}},
        # Gap filling: one row per calendar day. Upper bound is exclusive, hence to_dt + 1 day,
        # plus a $match below as a belt-and-braces trim.
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt + timedelta(days=1)]}}},
        {"$match": {"d": {"$gte": from_dt, "$lte": to_dt}}},
        {"$set": {"units": {"$ifNull": ["$units", 0]}, "late_count": {"$ifNull": ["$late_count", 0]},
                  "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                  "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]}}},
        # R9 headcount per day: department employees who joined on or before that day.
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$count": "n"},
        ], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {"$set": {"rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$units", {"$multiply": ["$headcount", 2]}]},
            None,
        ]}}},
        # 7-day moving average over the current row + up to 6 previous rows INSIDE the range.
        # $avg skips nulls and yields null when every value in the window is null.
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "units": 1,
                      "late_count": 1, "rate": 1, "ma": 1}},
    ]
    return Plan("attendance_logs", pipeline=pipeline, hint=IDX_LOG_UNIQUE)


# --------------------------------------------------------------------------- #
# 6. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    ensure_indexes()  # no-op once done; heals the case where MongoDB was down at startup
    return {"status": "ok"}


# ------------------------------- employees -------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_ms()
    try:
        db.employees.insert_one(doc)  # the UNIQUE index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: PageSizeQ = 20):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# ------------------------------- attendance ------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes_for(ts, day, emp),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc_seconds()

    # Most recent record whose punch_in <= punched_at. Records without punch_in (ABSENT/LEAVE) never match
    # a date comparison, and "most recent date" == "most recent punch_in" because date derives from punch_in.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", DESCENDING)], hint=IDX_LOG_UNIQUE
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"]:
        fail(("body", "punched_at"), "punched_at must be after the punch-in", body.punched_at)
    if ts - rec["punch_in"] > timedelta(hours=24):
        fail(("body", "punched_at"), "punch-out is more than 24 hours after the punch-in", body.punched_at)

    day = date.fromisoformat(rec["date"])
    new = derived_fields(emp, day, rec["punch_in"], ts)
    new["punch_out"] = ts
    # Compare-and-set: only succeeds if the record is STILL open. Two simultaneous punch-outs -> one wins.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, {"$set": new}, return_document=True
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    plan = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    f = plan.find
    total = db.attendance_logs.count_documents(f["filter"])
    cursor = (db.attendance_logs.find(f["filter"]).sort(f["sort"]).skip(f["skip"]).limit(f["limit"]).hint(plan.hint))
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, body: RegularizeIn, date: str = Path(...)):  # noqa: A002 (name fixed by the contract)
    day = parse_date(date, ("path", "date"))
    emp = get_employee_or_404(emp_code)

    for _ in range(5):  # optimistic concurrency: re-read and retry if somebody else wrote in between
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")

        new_status = body.status or rec["status"]
        old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
        if new_status in OFF_STATUS:
            if body.punch_in is not None or body.punch_out is not None:
                fail(("body",), f"{new_status} clears the punch times; do not send punch_in/punch_out")
            new_in = new_out = None
        else:
            new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
            new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
            if new_in is None:
                fail(("body", "punch_in"), "a presence status requires a punch_in")
            if attendance_date(new_in, emp) != day:
                fail(("body", "punch_in"), "punch_in must stay on the record's attendance date", body.punch_in)
            if new_out is not None:
                if new_out <= new_in:
                    fail(("body", "punch_out"), "punch_out must be after punch_in")
                if new_out - new_in > timedelta(hours=24):
                    fail(("body", "punch_out"), "punch_out must be within 24 hours of punch_in")

        new = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
        new.update(derived_fields(emp, day, new_in, new_out))
        old = {
            "status": rec["status"], "punch_in": old_in, "punch_out": old_out,
            "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
            "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
        }
        changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
        if not changes:
            fail(("body",), "the request changes nothing")

        entry = {"at": now_utc_ms(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        # Compare-and-set on what we read: history length + the three user-editable fields.
        # If anyone appended history or punched out meanwhile, nothing matches and we re-read.
        updated = db.attendance_logs.find_one_and_update(
            {
                "_id": rec["_id"],
                "status": old["status"],
                "punch_in": old_in,
                "punch_out": old_out,
                "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
            },
            {"$set": new, "$push": {"history": entry}},
            return_document=True,
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "the record is being modified concurrently; retry")


# -------------------------------- analytics ------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    first, last = parse_month(month)
    emp = get_employee_or_404(emp_code)
    plan = build_employee_monthly(emp_code, month)
    row = next(iter(db[plan.collection].aggregate(plan.pipeline, hint=plan.hint)), None) or {}

    # R7: working days = Mon-Fri from max(first of month, joined_on) to month end.
    joined = date.fromisoformat(emp["joined_on"])
    working_days = weekdays_between(max(first, joined), last)
    units = int(row.get("present_units", 0))  # half-days: full day = 2, half day = 1
    pct = None
    if working_days > 0:
        pct = half_up(Decimal(units) / Decimal(2 * working_days) * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": units / 2,
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    plan = build_department_summary(month, department)
    items = []
    for r in db[plan.collection].aggregate(plan.pipeline, hint=plan.hint):
        avg = None
        if r["wh_n"] > 0:
            avg = half_up(Decimal(int(r["wh_cents"])) / Decimal(int(r["wh_n"]) * 100), 2)
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": r["present_units"] / 2,
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQ, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    plan = build_late_leaderboard(month, limit, department)
    items = [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in db[plan.collection].aggregate(plan.pipeline, hint=plan.hint)
    ]
    return {"month": month, "items": items}


def trend_rows(plan: Plan) -> list[dict]:
    return [
        {
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": r["units"] / 2,
            "late_count": int(r["late_count"]),
            "attendance_rate": half_up(r.get("rate"), 4),
            "moving_avg_7d": half_up(r.get("ma"), 4),
        }
        for r in db[plan.collection].aggregate(plan.pipeline, hint=plan.hint)
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from", pattern=DATE_RE)],
    to: Annotated[str, Query(pattern=DATE_RE)],
):
    d_from = parse_date(from_, ("query", "from"))
    d_to = parse_date(to, ("query", "to"))
    plan = build_department_trend(department, d_from, d_to)
    return {"department": department, "items": trend_rows(plan)}


# ---------------------------------- admin --------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    def need(**named):
        for k, v in named.items():
            if v is None or v == "":
                fail(("query", k), f"'{k}' is required for {endpoint}")

    if endpoint == "attendance_list":
        plan = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    elif endpoint == "employee_monthly":
        need(emp_code=emp_code, month=month)
        plan = build_employee_monthly(emp_code, month)
    elif endpoint == "department_summary":
        need(month=month)
        plan = build_department_summary(month, department)
    elif endpoint == "late_leaderboard":
        need(month=month)
        plan = build_late_leaderboard(month, limit, department)
    else:  # department_trend
        need(department=department, **{"from": from_, "to": to})
        plan = build_department_trend(department, parse_date(from_, ("query", "from")), parse_date(to, ("query", "to")))

    raw = db.command(plan.explain_command())
    # explain output contains BSON types (ObjectId, Int64, dates): convert to plain JSON.
    explain_json = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return JSONResponse({"endpoint": endpoint, "collection": plan.collection, "explain": explain_json})
