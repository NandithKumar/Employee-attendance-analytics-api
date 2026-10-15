"""HROne Employee Attendance & Analytics API.

Run: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB environment variables.
"""
import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENT, "ABSENT", "LEAVE")
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def from_ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    if type(value) is not int or value < 100_000_000_000 or value > 4_102_444_800_000:
        raise HTTPException(422, "timestamp must be epoch milliseconds")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def api_doc(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    # Seeded records may omit optional legacy fields. The public attendance schema does not.
    if "emp_code" in doc and "date" in doc:
        doc.setdefault("punch_in", None)
        doc.setdefault("punch_out", None)
        doc.setdefault("work_hours", None)
        doc.setdefault("late_minutes", 0)
        doc.setdefault("overtime_minutes", 0)
        doc.setdefault("half_day", False)
        doc.setdefault("history", [])
    for k in ("created_at", "punch_in", "punch_out"):
        if k in doc:
            doc[k] = to_ms(doc[k])
    hist = []
    for entry in doc.get("history", []) or []:
        e = dict(entry)
        e["at"] = to_ms(e.get("at"))
        changes = {}
        for field, vals in (e.get("changes") or {}).items():
            vals = dict(vals)
            if field in ("punch_in", "punch_out"):
                vals = {k: to_ms(v) for k, v in vals.items()}
            changes[field] = vals
        e["changes"] = changes
        hist.append(e)
    if "history" in doc:
        doc["history"] = hist
    return doc


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_datetime(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), IST)


def attendance_day(punch_in: datetime, employee: dict) -> date:
    local = as_utc(punch_in).astimezone(IST)
    start_h, start_m = map(int, employee["shift_start"].split(":"))
    end_h, end_m = map(int, employee["shift_end"].split(":"))
    start_minutes, end_minutes = start_h * 60 + start_m, end_h * 60 + end_m
    if end_minutes <= start_minutes and local.hour * 60 + local.minute < end_minutes:
        return local.date() - timedelta(days=1)
    return local.date()


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str = "18:30") -> int:
    local = as_utc(punch_in).astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    # Only early-morning times in an overnight shift belong to yesterday.
    if (eh * 60 + em) <= (h * 60 + m) and local.hour * 60 + local.minute < eh * 60 + em:
        start -= timedelta(days=1)
    seconds = (local - start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def shift_end_for_record(record_date: str, employee: dict) -> datetime:
    day = date.fromisoformat(record_date)
    start_h, start_m = map(int, employee["shift_start"].split(":"))
    end_h, end_m = map(int, employee["shift_end"].split(":"))
    end_day = day + timedelta(days=1) if (end_h * 60 + end_m) <= (start_h * 60 + start_m) else day
    return shift_datetime(end_day, employee["shift_end"]).astimezone(UTC)


def compute_overtime(punch_out: datetime, employee: dict, record_date: str) -> int:
    delta = (as_utc(punch_out) - shift_end_for_record(record_date, employee)).total_seconds()
    minutes = int(delta // 60)
    return minutes if minutes >= 30 else 0


def derived_fields(record: dict, employee: dict) -> dict:
    status = record.get("status")
    pin, pout = as_utc(record.get("punch_in")), as_utc(record.get("punch_out"))
    if status not in PRESENT:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(pin, employee["shift_start"], employee["shift_end"]) if pin else 0
    if pin and pout:
        seconds = int((pout - pin).total_seconds())
        hours = round_half_up(Decimal(seconds) / Decimal(3600), 2)
        return {"late_minutes": late, "work_hours": hours,
                "overtime_minutes": compute_overtime(pout, employee, record["date"]),
                "half_day": hours < 4.5}
    return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}


def check_page(page: int, page_size: int):
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be between 1 and 100")


def check_month(month: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    return f"{month}-01", f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def ensure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_emp_punch_in")


@app.on_event("startup")
def startup():
    ensure_indexes()


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def reject_explicit_null_timestamp(cls, v):
        if v is None:
            raise ValueError("punched_at must be an integer epoch millisecond value")
        return v

    @field_validator("punched_at")
    @classmethod
    def valid_ms(cls, v):
        if v is not None and (type(v) is not int or not 100_000_000_000 <= v <= 4_102_444_800_000):
            raise ValueError("punched_at must be epoch milliseconds")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def reject_explicit_null_timestamp(cls, v):
        if v is None:
            raise ValueError("punched_at must be an integer epoch millisecond value")
        return v

    @field_validator("punched_at")
    @classmethod
    def valid_ms(cls, v):
        if v is not None and (type(v) is not int or not 100_000_000_000 <= v <= 4_102_444_800_000):
            raise ValueError("punched_at must be epoch milliseconds")
        return v


class RegularizeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ms(cls, v):
        if v is not None and (type(v) is not int or not 100_000_000_000 <= v <= 4_102_444_800_000):
            raise ValueError("timestamps must be epoch milliseconds")
        return v

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def reject_explicit_null_timestamp(cls, v):
        if v is None:
            raise ValueError("timestamp must be an integer epoch millisecond value")
        return v


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return api_doc(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    docs = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [api_doc(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    local_day = attendance_day(ts, emp).isoformat()
    doc = {"emp_code": body.emp_code, "date": local_day, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this attendance date")
    return api_doc(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if status is not None:
        if status not in STATUSES: raise HTTPException(422, "invalid status")
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from.isoformat()
        if date_to: q["date"]["$lte"] = date_to.isoformat()
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    result = []
    for d in docs:
        d.setdefault("history", []); d.setdefault("half_day", False); d.setdefault("late_minutes", 0); d.setdefault("overtime_minutes", 0)
        result.append(api_doc(d))
    return {"items": result, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    candidate = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not candidate: raise HTTPException(404, "no punch-in found")
    pin = as_utc(candidate.get("punch_in"))
    if not pin: raise HTTPException(404, "no punch-in found")
    if ts <= pin or ts > pin + timedelta(hours=24): raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    seconds = int((ts - pin).total_seconds())
    hours = round_half_up(Decimal(seconds) / Decimal(3600), 2)
    overtime = compute_overtime(ts, emp, candidate["date"])
    changes = {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.5}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None}, {"$set": changes}, return_document=ReturnDocument.AFTER)
    if updated is None:
        # Distinguish a simultaneous close from a record that disappeared.
        latest = db.attendance_logs.find_one({"_id": candidate["_id"]})
        if latest and latest.get("punch_out") is not None: raise HTTPException(409, "already punched out")
        raise HTTPException(404, "attendance record not found")
    return api_doc(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: date, body: RegularizeIn):
    record_date = date
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date.isoformat()})
    if not old: raise HTTPException(404, "attendance record not found")
    fields = body.model_fields_set - {"reason", "regularized_by"}
    if not fields: raise HTTPException(422, "no changes supplied")
    proposed = dict(old)
    proposed.setdefault("punch_in", None)
    proposed.setdefault("punch_out", None)
    proposed.setdefault("work_hours", None)
    proposed.setdefault("late_minutes", 0)
    proposed.setdefault("overtime_minutes", 0)
    proposed.setdefault("half_day", False)
    proposed.setdefault("history", [])
    for f in fields:
        val = getattr(body, f)
        proposed[f] = from_ms(val) if f in ("punch_in", "punch_out") and val is not None else val
    status = proposed.get("status", old.get("status"))
    if status in ("ABSENT", "LEAVE"):
        if ("punch_in" in fields and body.punch_in is not None) or ("punch_out" in fields and body.punch_out is not None):
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        proposed["punch_in"] = proposed["punch_out"] = None
    else:
        if not proposed.get("punch_in"): raise HTTPException(422, "presence status requires punch_in")
        if attendance_day(proposed["punch_in"], emp).isoformat() != record_date.isoformat():
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if proposed.get("punch_out"):
            if as_utc(proposed["punch_out"]) <= as_utc(proposed["punch_in"]) or as_utc(proposed["punch_out"]) > as_utc(proposed["punch_in"]) + timedelta(hours=24):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    proposed.update(derived_fields(proposed, emp))
    keys = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for k in keys:
        before, after = old.get(k), proposed.get(k)
        if k == "half_day": before = bool(before); after = bool(after)
        if k in ("late_minutes", "overtime_minutes"): before = before or 0; after = after or 0
        if before != after:
            changes[k] = {"from": before, "to": after}
    if not changes: raise HTTPException(422, "request does not change the record")
    now = now_utc()
    entry = {"at": now, "by": body.regularized_by, "reason": body.reason, "changes": changes}
    set_fields = {k: proposed[k] for k in keys if k in proposed}
    # Compare-and-swap protects both the record and append-only history from lost updates.
    old_history = old.get("history", []) or []
    guard = {"_id": old["_id"]}
    guard["history"] = old_history if "history" in old else {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": set_fields, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record changed concurrently; retry regularization")
    return api_doc(updated)


def month_match(month: str, emp_code: Optional[str] = None):
    start, end = check_month(month)
    q = {"date": {"$gte": start, "$lte": end}}
    if emp_code is not None: q["emp_code"] = emp_code
    return q, start, end


def working_days(joined_on: str, start: str, end: str) -> int:
    lo = max(date.fromisoformat(joined_on), date.fromisoformat(start))
    hi = date.fromisoformat(end)
    if lo > hi: return 0
    return sum(1 for n in range((hi-lo).days+1) if (lo+timedelta(days=n)).weekday() < 5)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    q, start, end = month_match(month, emp_code)
    pipeline = [
        {"$match": q},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT)]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}}}, [2, 3, 4, 5, 6]]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]
    agg = list(db.attendance_logs.aggregate(pipeline))
    values = agg[0] if agg else {}
    wd = working_days(emp["joined_on"], start, end)
    present = float(values.get("present_days", 0))
    pct = round_half_up(Decimal(str(present)) / Decimal(wd) * Decimal(100), 4) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": values.get("leave_days", 0), "late_count": values.get("late_count", 0),
            "total_late_minutes": values.get("total_late_minutes", 0), "total_overtime_minutes": values.get("total_overtime_minutes", 0), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    start, end = check_month(month)
    # Employee-first join preserves employees without attendance records for headcount.
    pipeline = [
        {"$match": {"joined_on": {"$lte": end}, **({"department": department} if department is not None else {})}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$ne": [{"$ifNull": ["$logs.date", None]}, None]}, {"$in": ["$logs.status", list(PRESENT)]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", start]}, "format": "%Y-%m-%d", "timezone": "UTC"}}}, [2, 3, 4, 5, 6]]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
            "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$ne": ["$avg_work_hours", None]}, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$avg_work_hours", 100]}, 0.5]}}, 100]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]
    rows = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": [r for r in rows if r["headcount"] > 0]}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    q, _, _ = month_match(month)
    pipeline = [
        {"$match": q},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: date = Query(..., alias="from"), to_date: date = Query(..., alias="to")):
    if to_date < from_date or (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "invalid date range; maximum is 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "department not found")
    start, end = from_date.isoformat(), to_date.isoformat()
    # Convert string calendar dates to UTC date anchors, densify in MongoDB, then use a window stage.
    start_dt = datetime.combine(from_date, time.min, UTC)
    end_exclusive = datetime.combine(to_date + timedelta(days=1), time.min, UTC)
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", list(PRESENT)]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
        }},
        {"$project": {"_id": 0,
            "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "present_count": 1, "late_count": 1}},
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}}, {"$limit": 1},
            {"$project": {"day": {"$literal": start_dt - timedelta(days=1)},
                "present_count": {"$literal": 0}, "late_count": {"$literal": 0}}}
        ]}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_exclusive]}}},
        # The unioned seed row exists only to make $densify generate a series when there are no logs.
        # Remove it (and any out-of-range row) before computing the moving average.
        {"$match": {"day": {"$gte": start_dt, "$lt": end_exclusive}}},
        {"$set": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "date_str": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]}
        }},
        {"$lookup": {"from": "employees", "let": {"d": "$date_str"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
            {"$count": "n"}
        ], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}, None
        ]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}
        }}},
        {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1,
            "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None,
                {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}}, 10000]}]}}},
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$sort": {"date": 1}}
    ]
    rows = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
            department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
            date_from: Optional[date] = None, date_to: Optional[date] = None,
            status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
            from_date: Optional[date] = Query(None, alias="from"), to_date: Optional[date] = Query(None, alias="to")):
    """Return executionStats explain for the same primary operation used by each endpoint."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        q = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if status is not None:
            if status not in STATUSES:
                raise HTTPException(422, "invalid status")
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from.isoformat()
            if date_to: q["date"]["$lte"] = date_to.isoformat()
        command = {"find": "attendance_logs", "filter": q,
                   "sort": {"date": -1, "emp_code": 1},
                   "skip": (page - 1) * page_size, "limit": page_size}
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        q, _, _ = month_match(month, emp_code)
        command = {"aggregate": "attendance_logs", "pipeline": [
            {"$match": q},
            {"$group": {"_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": [
                    {"$in": ["$status", list(PRESENT)]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}}}, [2, 3, 4, 5, 6]]}]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
        ], "cursor": {}}
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = check_month(month)
        command = {"aggregate": "employees", "pipeline": [
            {"$match": {"joined_on": {"$lte": end}, **({"department": department} if department is not None else {})}},
            {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}]}}}], "as": "logs"}},
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": {"$cond": [{"$and": [
                    {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                    {"$in": ["$logs.status", list(PRESENT)]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", start]}, "format": "%Y-%m-%d", "timezone": "UTC"}}}, [2, 3, 4, 5, 6]]}]},
                    {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
                "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", None]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
            {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
                "avg_work_hours": {"$cond": [{"$ne": ["$avg_work_hours", None]}, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$avg_work_hours", 100]}, 0.5]}}, 100]}, None]},
                "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
            {"$sort": {"department": 1}}
        ], "cursor": {}}
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        q, _, _ = month_match(month)
        pipe = [
            {"$match": q},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
        ]
        if department is not None: pipe.append({"$match": {"employee.department": department}})
        pipe += [
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}
        ]
        command = {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}
        collection = "attendance_logs"
    elif endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(422, "department, from and to are required")
        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, "invalid date range; maximum is 92 days")
        if not db.employees.find_one({"department": department}):
            raise HTTPException(404, "department not found")
        start, end = from_date.isoformat(), to_date.isoformat()
        start_dt = datetime.combine(from_date, time.min, UTC)
        end_exclusive = datetime.combine(to_date + timedelta(days=1), time.min, UTC)
        pipe = [
            {"$match": {"date": {"$gte": start, "$lte": end}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"}, {"$match": {"employee.department": department}},
            {"$group": {"_id": "$date", "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
            {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}, "present_count": 1, "late_count": 1}},
            {"$unionWith": {"coll": "employees", "pipeline": [{"$match": {"department": department}}, {"$limit": 1},
                {"$project": {"day": {"$literal": start_dt - timedelta(days=1)}, "present_count": {"$literal": 0}, "late_count": {"$literal": 0}}}]}},
            {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_exclusive]}}},
            {"$match": {"day": {"$gte": start_dt, "$lt": end_exclusive}}},
            {"$set": {"present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]},
                "date_str": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]}}},
            {"$lookup": {"from": "employees", "let": {"d": "$date_str"}, "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}}, {"$count": "n"}], "as": "hc"}},
            {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
            {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}, None]}}},
            {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
            {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1,
                "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}}, 10000]}]}}},
            {"$sort": {"date": 1}}
        ]
        command = {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}
        collection = "attendance_logs"
    else:
        raise HTTPException(422, "unknown endpoint")
    raw = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": raw}
