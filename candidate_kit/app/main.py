"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience)
"""
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    """Create all necessary indexes at startup, idempotently."""
    # employees: unique emp_code (enforces concurrent duplicate creates -> one 201, rest 409)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_employees_emp_code")

    # attendance_logs: unique composite key (emp_code, date) - enforces one record per employee per day
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="idx_attendance_emp_date",
    )
    # attendance_logs: list queries filter/sort by date desc, emp_code asc
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_empcode",
    )
    # attendance_logs: analytics filter by emp_code + date range
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        name="idx_attendance_empcode_date",
    )


# ---------------------------------------------------------------------------
# Helpers: time / computation
# ---------------------------------------------------------------------------
EPOCH_MIN = 100_000_000_000  # 100 billion ms — below this looks like seconds
EPOCH_MAX = 4_102_444_800_000

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}


def ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC-aware datetime, truncated to whole seconds."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a datetime (naive treated as UTC) to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def dt_to_ist(dt: datetime) -> datetime:
    """Convert a UTC (or naive-UTC) datetime to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Determine the attendance date (IST calendar day) per R1.
    For overnight shifts (shift_end <= shift_start), a punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    ist = dt_to_ist(punch_in_utc)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if is_overnight:
        ist_minutes = ist.hour * 60 + ist.minute
        end_minutes = eh * 60 + em
        if ist_minutes < end_minutes:
            # This punch-in is on the "morning" side of an overnight shift → belongs to yesterday
            ist -= timedelta(days=1)
    return ist.strftime("%Y-%m-%d")


def half_up_round(value: float, decimals: int) -> float:
    """Half-up rounding (not banker's rounding)."""
    factor = 10 ** decimals
    return math.floor(value * factor + 0.5) / factor


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late if strictly more than 10 minutes after shift_start. Returns floor minutes."""
    h, m = map(int, shift_start.split(":"))
    # shift_start on the attendance date in IST
    date_ist = datetime.fromisoformat(date_str).replace(tzinfo=IST)
    start = date_ist.replace(hour=h, minute=m, second=0, microsecond=0)

    # punch_in is UTC; compare in UTC
    start_utc = start.astimezone(timezone.utc)
    diff_secs = (punch_in - start_utc).total_seconds()
    diff_minutes = int(diff_secs // 60)  # floor

    if diff_minutes > 10:
        return diff_minutes
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) / 3600, rounded to 2 decimals half-up."""
    secs = (punch_out - punch_in).total_seconds()
    return half_up_round(secs / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    """R3: floor minutes between shift_end and punch_out, only if >= 30; otherwise 0."""
    h, m = map(int, shift_end.split(":"))
    sh, sm = map(int, shift_start.split(":"))

    # Determine whether overnight shift
    is_overnight = (h * 60 + m) <= (sh * 60 + sm)

    date_ist = datetime.fromisoformat(date_str).replace(tzinfo=IST)
    end_ist = date_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    if is_overnight:
        end_ist += timedelta(days=1)

    end_utc = end_ist.astimezone(timezone.utc)
    diff_minutes = int((punch_out - end_utc).total_seconds() // 60)
    return diff_minutes if diff_minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def format_record(doc: dict) -> dict:
    """Convert a MongoDB attendance document to API response format."""
    history = []
    for entry in doc.get("history", []):
        changes = {}
        for field, chg in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                changes[field] = {
                    "from": dt_to_ms(chg["from"]) if chg["from"] is not None else None,
                    "to": dt_to_ms(chg["to"]) if chg["to"] is not None else None,
                }
            else:
                changes[field] = {"from": chg["from"], "to": chg["to"]}
        history.append({
            "at": dt_to_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def format_employee(doc: dict) -> dict:
    """Convert a MongoDB employee document to API response format."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
def validate_epoch_ms(v):
    """Validate that v is an integer epoch milliseconds value."""
    if v is None:
        return v
    if not isinstance(v, int):
        raise ValueError("Must be an integer (epoch milliseconds)")
    if v < EPOCH_MIN or v > EPOCH_MAX:
        raise ValueError(
            f"Epoch milliseconds must be between {EPOCH_MIN} and {EPOCH_MAX} "
            f"(looks like seconds if < 1e11)"
        )
    return v


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @model_validator(mode="after")
    def shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_ms(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {PRESENCE_STATUSES}")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_ms(cls, v):
        return validate_epoch_ms(v)


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_ms(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {ALL_STATUSES}")
        return v

    @field_validator("reason")
    @classmethod
    def reason_length(cls, v):
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason must be 5–200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def by_length(cls, v):
        if len(v) < 1 or len(v) > 50:
            raise ValueError("regularized_by must be 1–50 characters")
        return v


# ---------------------------------------------------------------------------
# Endpoints: System
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB not reachable")


# ---------------------------------------------------------------------------
# Endpoints: Employees
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    )
    return {
        "items": [format_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Endpoints: Attendance
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

    date_str = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, emp["shift_start"], date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Employee already has a record for this attendance date")

    doc.pop("_id", None)
    return format_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

    # Find the most recent open record where punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "punch_out": None,
            "status": {"$in": list(PRESENCE_STATUSES)},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "No open punch-in found for this employee")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validate: punch_out must be after punch_in and within 24 hours
    if ts <= punch_in_dt:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Compute derived fields
    wh = compute_work_hours(punch_in_dt, ts)
    ot = compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"])
    hd = compute_half_day(wh)

    # Atomic update with optimistic concurrency: only update if punch_out is still null
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": wh,
            "overtime_minutes": ot,
            "half_day": hd,
        }},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "Record was already punched out by a concurrent request")

    return format_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        if status not in ALL_STATUSES:
            raise HTTPException(422, f"status must be one of {ALL_STATUSES}")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "No attendance record found for this date")

    new_status = body.status if body.status is not None else record["status"]
    new_pin_ms = body.punch_in
    new_pout_ms = body.punch_out

    punch_in_dt = record.get("punch_in")
    if punch_in_dt and punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)
    punch_out_dt = record.get("punch_out")
    if punch_out_dt and punch_out_dt.tzinfo is None:
        punch_out_dt = punch_out_dt.replace(tzinfo=timezone.utc)

    # R: setting ABSENT/LEAVE clears punch times; can't also supply punch times
    if new_status in ("ABSENT", "LEAVE"):
        if new_pin_ms is not None or new_pout_ms is not None:
            raise HTTPException(422, "Cannot supply punch_in or punch_out with ABSENT/LEAVE status")
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = ms_to_dt(new_pin_ms) if new_pin_ms is not None else punch_in_dt
        new_punch_out = ms_to_dt(new_pout_ms) if new_pout_ms is not None else punch_out_dt

        # Presence status requires a punch_in
        if new_status in PRESENCE_STATUSES and new_punch_in is None:
            raise HTTPException(422, "Presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        if new_punch_in is not None:
            pin_date = attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
            if pin_date != date:
                raise HTTPException(422, "punch_in must fall on the record's attendance date")

        # punch_out must be after punch_in and within 24 hours
        if new_punch_out is not None and new_punch_in is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_wh = None
        new_ot = 0
        new_hd = False
    else:
        if new_punch_in is not None:
            new_late = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        else:
            new_late = 0

        if new_punch_in is not None and new_punch_out is not None:
            new_wh = compute_work_hours(new_punch_in, new_punch_out)
            new_ot = compute_overtime(new_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_hd = compute_half_day(new_wh)
        else:
            new_wh = None
            new_ot = 0
            new_hd = False

    # Build changes dict (only fields that actually changed)
    changes = {}

    old_status = record["status"]
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}

    if new_punch_in != punch_in_dt:
        changes["punch_in"] = {"from": punch_in_dt, "to": new_punch_in}

    if new_punch_out != punch_out_dt:
        changes["punch_out"] = {"from": punch_out_dt, "to": new_punch_out}

    old_wh = record.get("work_hours")
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}

    old_late = record.get("late_minutes", 0)
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}

    old_ot = record.get("overtime_minutes", 0)
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}

    old_hd = record.get("half_day", False)
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update; compare on record version via _id to avoid concurrent history loss
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "Concurrent edit conflict; please retry")

    return format_record(result)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------
def _month_range(month: str):
    """Return (start_date_str, end_date_str) for a YYYY-MM string."""
    year, mon = map(int, month.split("-"))
    start = f"{year:04d}-{mon:02d}-01"
    # last day of month
    if mon == 12:
        next_year, next_mon = year + 1, 1
    else:
        next_year, next_mon = year, mon + 1
    import calendar
    last_day = calendar.monthrange(year, mon)[1]
    end = f"{year:04d}-{mon:02d}-{last_day:02d}"
    return start, end


def _working_days_in_range(start_str: str, end_str: str, from_date_str: Optional[str] = None) -> int:
    """Count Mon-Fri days between start and end (inclusive), optionally starting from from_date."""
    from datetime import date
    sd = datetime.fromisoformat(start_str).date()
    ed = datetime.fromisoformat(end_str).date()
    if from_date_str:
        fd = datetime.fromisoformat(from_date_str).date()
        if fd > sd:
            sd = fd
    count = 0
    cur = sd
    while cur <= ed:
        if cur.weekday() < 5:  # Mon=0 .. Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Endpoints: Analytics
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    start, end = _month_range(month)
    joined = emp["joined_on"]

    # Working days per R7: from max(start, joined_on) to end, Mon-Fri
    effective_start = max(start, joined)
    working_days = _working_days_in_range(effective_start, end)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                # present_days: Mon-Fri only, half-day = 0.5 (R5-R7)
                # We compute per-record and use $sum:
                # PRESENT/WFH/ON_DUTY records on Mon-Fri = 0.5 if half_day else 1.0
                "records": {"$push": {
                    "status": "$status",
                    "date": "$date",
                    "half_day": "$half_day",
                }},
            }
        },
    ]
    agg = list(db.attendance_logs.aggregate(pipeline))

    if not agg:
        result = agg[0] if agg else None
        return {
            "emp_code": emp_code,
            "month": month,
            "working_days": working_days,
            "present_days": 0.0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
            "attendance_pct": None if working_days == 0 else 0.0,
        }

    r = agg[0]
    # Compute present_days from records (Mon-Fri only)
    present_days = 0.0
    for rec in r["records"]:
        if rec["status"] in PRESENCE_STATUSES:
            d = datetime.fromisoformat(rec["date"]).date()
            if d.weekday() < 5:  # Mon-Fri
                present_days += 0.5 if rec.get("half_day") else 1.0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = half_up_round(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up_round(present_days, 2),
        "leave_days": r["leave_days"],
        "late_count": r["late_count"],
        "total_late_minutes": r["total_late_minutes"],
        "total_overtime_minutes": r["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    start, end = _month_range(month)

    emp_filter = {"joined_on": {"$lte": end}}
    if department:
        emp_filter["department"] = department

    # Aggregate employees into departments for headcount (R9)
    emp_pipeline = [
        {"$match": emp_filter},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"},
            "joined_dates": {"$push": "$joined_on"},
        }},
        {"$sort": {"_id": ASCENDING}},
    ]
    dept_map = {d["_id"]: d for d in db.employees.aggregate(emp_pipeline)}

    if not dept_map:
        return {"month": month, "items": []}

    # Get attendance logs for the month
    log_pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        # Join with employees to get department
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp_info",
        }},
        {"$unwind": "$emp_info"},
        # Filter by department if specified
        *([{"$match": {"emp_info.department": department}}] if department else []),
        {"$group": {
            "_id": "$emp_info.department",
            "present_records": {
                "$push": {
                    "$cond": [
                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                        {"date": "$date", "half_day": "$half_day", "work_hours": "$work_hours"},
                        None,
                    ]
                }
            },
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "wh_values": {
                "$push": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            {"$ne": ["$work_hours", None]},
                        ]},
                        "$work_hours",
                        None,
                    ]
                }
            },
        }},
    ]
    log_results = {r["_id"]: r for r in db.attendance_logs.aggregate(log_pipeline)}

    items = []
    for dept_name in sorted(dept_map.keys()):
        dept_info = dept_map[dept_name]
        logs = log_results.get(dept_name, {})

        present_days = 0.0
        for rec in logs.get("present_records", []):
            if rec is not None:
                d = datetime.fromisoformat(rec["date"]).date()
                if d.weekday() < 5:
                    present_days += 0.5 if rec.get("half_day") else 1.0

        wh_vals = [v for v in logs.get("wh_values", []) if v is not None]
        avg_wh = None
        if wh_vals:
            avg_wh = half_up_round(sum(wh_vals) / len(wh_vals), 2)

        items.append({
            "department": dept_name,
            "headcount": dept_info["headcount"],
            "present_days": half_up_round(present_days, 2),
            "avg_work_hours": avg_wh,
            "late_count": logs.get("late_count", 0),
            "total_late_minutes": logs.get("total_late_minutes", 0),
            "leave_count": logs.get("leave_count", 0),
            "on_duty_count": logs.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = _month_range(month)

    emp_filter: dict = {}
    if department:
        emp_filter["department"] = department

    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        # Join with employees for name, department, filtering
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp_info",
        }},
        {"$unwind": "$emp_info"},
        *([{"$match": {"emp_info.department": department}}] if department else []),
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking
    items = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0 and row["total_late_minutes"] < rows[i - 1]["total_late_minutes"]:
            rank = i + 1
        if rank > limit:
            break
        items.append({
            "rank": rank,
            "emp_code": row["_id"],
            "name": row["emp_info"]["name"],
            "department": row["emp_info"]["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(alias="from"),
    to: str = Query(),
):
    if to < from_:
        raise HTTPException(422, "to must be >= from")

    from datetime import date as date_type
    fd = datetime.fromisoformat(from_).date()
    td = datetime.fromisoformat(to).date()
    days_diff = (td - fd).days
    if days_diff > 91:  # 92 days max means at most 91 days between from and to
        raise HTTPException(422, "Date range must not exceed 92 days")

    # Check department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    # Get all employees in this department (for headcount per day)
    dept_employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))

    # MongoDB aggregation with $densify for gap-filling
    pipeline = [
        {"$match": {
            "date": {"$gte": from_, "$lte": to},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp_info",
        }},
        {"$unwind": "$emp_info"},
        {"$match": {"emp_info.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count_raw": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                        0,
                    ]
                }
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
        }},
        {"$densify": {
            "field": "_id",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_, to],
            },
        }},
        {"$sort": {"_id": ASCENDING}},
    ]

    try:
        agg_results = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}
    except Exception:
        # Fallback if $densify not supported (MongoDB < 6.1)
        agg_results = {}
        raw = list(db.attendance_logs.aggregate(pipeline[:-2]))  # skip densify and sort
        for r in raw:
            agg_results[r["_id"]] = r

    # Build result row by row
    items = []
    non_null_rates = []  # for moving average (up to last 7 non-null rates)

    cur = fd
    while cur <= td:
        date_str = cur.strftime("%Y-%m-%d")
        is_working = cur.weekday() < 5

        # headcount: employees with joined_on <= this day
        headcount = sum(1 for e in dept_employees if e["joined_on"] <= date_str)

        row = agg_results.get(date_str, {})
        present_count = half_up_round(row.get("present_count_raw", 0), 2)
        late_count = row.get("late_count", 0)

        # attendance_rate: only on working days with headcount > 0
        att_rate = None
        if is_working and headcount > 0:
            att_rate = half_up_round(present_count / headcount, 4)
            non_null_rates.append(att_rate)
        else:
            non_null_rates.append(None)  # placeholder

        # moving_avg_7d: mean of non-null rates in current + up to 6 preceding rows in range
        window = [r for r in non_null_rates[-7:] if r is not None]
        moving_avg = half_up_round(sum(window) / len(window), 4) if window else None

        items.append({
            "date": date_str,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": att_rate,
            "moving_avg_7d": moving_avg,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin: Explain
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    VALID_ENDPOINTS = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in VALID_ENDPOINTS:
        raise HTTPException(422, f"endpoint must be one of {VALID_ENDPOINTS}")

    if endpoint == "attendance_list":
        if not date_from and not date_to and not emp_code:
            raise HTTPException(422, "attendance_list requires at least one filter parameter")
        q = {}
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
        pipeline = [
            {"$match": q},
            {"$sort": {"date": -1, "emp_code": 1}},
            {"$skip": (page - 1) * page_size},
            {"$limit": page_size},
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "employee_monthly requires emp_code and month")
        start, end = _month_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
            {"$group": {"_id": None, "total": {"$sum": 1}}},
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "department_summary requires month")
        start, end = _month_range(month)
        emp_filter = {"joined_on": {"$lte": end}}
        if department:
            emp_filter["department"] = department
        explain_doc = db.command(
            "explain",
            {"find": "employees", "filter": emp_filter},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "late_leaderboard requires month")
        start, end = _month_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department_trend requires department, from, and to")
        pipeline = [
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_info",
            }},
            {"$unwind": "$emp_info"},
            {"$match": {"emp_info.department": department}},
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}
