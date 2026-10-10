"""
Employee Attendance & Analytics API
"""
import os
import math
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone, date
from typing import Optional, List, Dict, Any, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, EmailStr, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

# Idempotent index creation
db.employees.create_index([("emp_code", ASCENDING)], unique=True)
db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
db.attendance_logs.create_index([("date", DESCENDING)])
db.employees.create_index([("department", ASCENDING)])

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def get_ist_dt(dt: datetime) -> datetime:
    return dt.astimezone(IST)

def truncate_to_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)

def dt_to_epoch(dt: Optional[datetime]) -> Optional[int]:
    if not dt: return None
    return int(dt.timestamp() * 1000)

def epoch_to_dt(epoch: int) -> datetime:
    return truncate_to_seconds(datetime.fromtimestamp(epoch / 1000, tz=timezone.utc))

def round_half_up(value: float, decimals: int = 2) -> float:
    return float(Decimal(str(value)).quantize(Decimal(f"0.{'0' * decimals}"), rounding=ROUND_HALF_UP))

def get_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> date:
    p_time = punch_in_ist.time()
    s_end_time = datetime.strptime(shift_end, "%H:%M").time()
    s_start_time = datetime.strptime(shift_start, "%H:%M").time()
    
    if s_end_time <= s_start_time and p_time < s_end_time:
        return punch_in_ist.date() - timedelta(days=1)
    return punch_in_ist.date()

def compute_late_minutes(punch_in_ist: datetime, att_date: date, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start_dt = datetime.combine(att_date, datetime.min.time()).replace(hour=h, minute=m, tzinfo=IST)
    diff = (punch_in_ist - start_dt).total_seconds()
    minutes = math.floor(diff / 60)
    return minutes if minutes > 10 else 0

def compute_overtime(punch_out_ist: datetime, att_date: date, shift_start: str, shift_end: str) -> int:
    h, m = map(int, shift_end.split(":"))
    s_end_time = datetime.strptime(shift_end, "%H:%M").time()
    s_start_time = datetime.strptime(shift_start, "%H:%M").time()
    end_date = att_date + timedelta(days=1) if s_end_time <= s_start_time else att_date
    
    end_dt = datetime.combine(end_date, datetime.min.time()).replace(hour=h, minute=m, tzinfo=IST)
    diff = (punch_out_ist - end_dt).total_seconds()
    minutes = math.floor(diff / 60)
    return minutes if minutes >= 30 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    diff = (punch_out - punch_in).total_seconds()
    return round_half_up(diff / 3600.0, 2)

def get_month_dates(month: str):
    year, m = map(int, month.split("-"))
    start_date = date(year, m, 1)
    next_month = date(year + 1, 1, 1) if m == 12 else date(year, m + 1, 1)
    return start_date, next_month - timedelta(days=1)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r'^EMP\d{4,6}$')
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str = Field("18:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: str
    
class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = "PRESENT"
    @field_validator('status')
    def validate_status(cls, v):
        if v not in ["PRESENT", "WFH", "ON_DUTY"]: raise ValueError("Invalid status")
        return v

class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)

class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command('ping')
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database not available")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
        
    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(timezone.utc))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    
    doc.pop("_id", None)
    doc["created_at"] = dt_to_epoch(doc["created_at"])
    return doc

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if department: q["department"] = department
    
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = []
    for doc in cursor:
        doc["created_at"] = dt_to_epoch(doc["created_at"])
        items.append(doc)
        
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "Employee not found")
        
    ts = epoch_to_dt(body.punched_at) if body.punched_at else truncate_to_seconds(datetime.now(timezone.utc))
    ts_ist = get_ist_dt(ts)
    
    att_date = get_attendance_date(ts_ist, emp["shift_start"], emp["shift_end"])
    
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts_ist, att_date, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
        
    doc.pop("_id", None)
    doc["punch_in"] = dt_to_epoch(doc["punch_in"])
    return doc

@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "Employee not found")
        
    ts_out = epoch_to_dt(body.punched_at) if body.punched_at else truncate_to_seconds(datetime.now(timezone.utc))
    
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts_out}},
        sort=[("punch_in", DESCENDING)]
    )
    if not record: raise HTTPException(404, "No punch-in found")
    if record["punch_out"] is not None: raise HTTPException(409, "Already punched out")
        
    diff_sec = (ts_out - record["punch_in"]).total_seconds()
    if diff_sec <= 0 or diff_sec > 24 * 3600:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
        
    ts_out_ist = get_ist_dt(ts_out)
    att_date = datetime.strptime(record["date"], "%Y-%m-%d").date()
    
    work_hrs = compute_work_hours(record["punch_in"], ts_out)
    overtime_mins = compute_overtime(ts_out_ist, att_date, emp["shift_start"], emp["shift_end"])
    half_day = bool(work_hrs < 4.50)
    
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts_out, "work_hours": work_hrs, "overtime_minutes": overtime_mins, "half_day": half_day}},
        return_document=True
    )
    if not updated: raise HTTPException(409, "Already punched out")
        
    updated.pop("_id", None)
    updated["punch_in"] = dt_to_epoch(updated["punch_in"])
    updated["punch_out"] = dt_to_epoch(updated["punch_out"])
    if updated.get("history"):
        for h in updated["history"]:
            h["at"] = dt_to_epoch(h["at"])
            for k_change in ["punch_in", "punch_out"]:
                if k_change in h["changes"]:
                    h["changes"][k_change]["from"] = dt_to_epoch(h["changes"][k_change]["from"])
                    h["changes"][k_change]["to"] = dt_to_epoch(h["changes"][k_change]["to"])
    
    return updated

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if emp_code: q["emp_code"] = emp_code
    if date_from or date_to:
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from > date_to")
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status: q["status"] = status
        
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    
    items = []
    for d in cursor:
        d["punch_in"] = dt_to_epoch(d["punch_in"])
        d["punch_out"] = dt_to_epoch(d["punch_out"])
        if d.get("history"):
            for h in d["history"]:
                h["at"] = dt_to_epoch(h["at"])
                for k_change in ["punch_in", "punch_out"]:
                    if k_change in h["changes"]:
                        h["changes"][k_change]["from"] = dt_to_epoch(h["changes"][k_change]["from"])
                        h["changes"][k_change]["to"] = dt_to_epoch(h["changes"][k_change]["to"])
        if "history" not in d: d["history"] = []
        if "half_day" not in d: d["half_day"] = False
        items.append(d)
        
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    
    if not emp or not record: raise HTTPException(404, "Employee or record not found")
        
    changes = {}
    new_status = body.status if body.status is not None else record["status"]
    new_in = epoch_to_dt(body.punch_in) if body.punch_in is not None else record["punch_in"]
    new_out = epoch_to_dt(body.punch_out) if body.punch_out is not None else record["punch_out"]
    
    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch times for ABSENT/LEAVE")
        new_in = None
        new_out = None
    else:
        if new_in is None: raise HTTPException(422, "Presence status requires punch_in")
        new_in_ist = get_ist_dt(new_in)
        if get_attendance_date(new_in_ist, emp["shift_start"], emp["shift_end"]).isoformat() != date:
            raise HTTPException(422, "punch_in must stay on the same attendance date")
        if new_out:
            diff_sec = (new_out - new_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24h")
                
    att_date_obj = datetime.strptime(date, "%Y-%m-%d").date()
    if new_status in ["ABSENT", "LEAVE"]:
        new_work, new_late, new_overtime, new_half = None, 0, 0, False
    else:
        new_late = compute_late_minutes(get_ist_dt(new_in), att_date_obj, emp["shift_start"])
        if new_out:
            new_work = compute_work_hours(new_in, new_out)
            new_overtime = compute_overtime(get_ist_dt(new_out), att_date_obj, emp["shift_start"], emp["shift_end"])
            new_half = bool(new_work < 4.50)
        else:
            new_work, new_overtime, new_half = None, 0, False
            
    old_fields = {
        "status": record["status"], "punch_in": record["punch_in"], "punch_out": record["punch_out"],
        "work_hours": record.get("work_hours"), "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0), "half_day": record.get("half_day", False)
    }
    new_fields = {
        "status": new_status, "punch_in": new_in, "punch_out": new_out,
        "work_hours": new_work, "late_minutes": new_late,
        "overtime_minutes": new_overtime, "half_day": new_half
    }
    
    for k in old_fields:
        if old_fields[k] != new_fields[k]:
            changes[k] = {"from": old_fields[k], "to": new_fields[k]}
            
    if not changes: raise HTTPException(422, "No changes")
        
    history_entry = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by, "reason": body.reason, "changes": changes
    }
    
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {"$set": new_fields, "$push": {"history": history_entry}},
        return_document=True
    )
    
    updated.pop("_id", None)
    updated["punch_in"] = dt_to_epoch(updated["punch_in"])
    updated["punch_out"] = dt_to_epoch(updated["punch_out"])
    if updated.get("history"):
        for h in updated["history"]:
            h["at"] = dt_to_epoch(h["at"])
            for k_change in ["punch_in", "punch_out"]:
                if k_change in h["changes"]:
                    h["changes"][k_change]["from"] = dt_to_epoch(h["changes"][k_change]["from"])
                    h["changes"][k_change]["to"] = dt_to_epoch(h["changes"][k_change]["to"])
                    
    return updated


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "Employee not found")
        
    start_dt, end_dt = get_month_dates(month)
    start_str, end_str = start_dt.isoformat(), end_dt.isoformat()
    joined_dt = datetime.strptime(emp["joined_on"], "%Y-%m-%d").date()
    
    working_days = sum(1 for n in range(int((end_dt - max(start_dt, joined_dt)).days) + 1) if (max(start_dt, joined_dt) + timedelta(n)).weekday() < 5)
    if (end_dt - max(start_dt, joined_dt)).days < 0: working_days = 0
        
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_str, "$lte": end_str}}},
        {"$addFields": {
            "is_weekday": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
        }},
        {"$group": {
            "_id": None,
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$is_weekday"]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]}, 0
                    ]
                }
            }
        }}
    ]
    
    res = list(db.attendance_logs.aggregate(pipeline))
    agg = res[0] if res else {"leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0, "present_days": 0.0}
    att_pct = round_half_up((agg["present_days"] / working_days * 100), 2) if working_days > 0 else None
    
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": agg["present_days"], "leave_days": agg["leave_days"],
        "late_count": agg["late_count"], "total_late_minutes": agg["total_late_minutes"],
        "total_overtime_minutes": agg["total_overtime_minutes"], "attendance_pct": att_pct
    }

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'), department: Optional[str] = None):
    start_dt, end_dt = get_month_dates(month)
    start_str, end_str = start_dt.isoformat(), end_dt.isoformat()
    match_stage = {"joined_on": {"$lte": end_str}}
    if department: match_stage["department"] = department
        
    pipeline = [
        {"$match": match_stage},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$emp"]},
                    {"$gte": ["$date", start_str]},
                    {"$lte": ["$date", end_str]}
                ]}}},
                {"$addFields": {"is_weekday": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}}}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": {"dept": "$department", "emp": "$emp_code"}, "emp_logs": {"$push": "$logs"}}},
        {"$group": {"_id": "$_id.dept", "headcount": {"$sum": 1}, "all_logs": {"$push": "$emp_logs"}}},
        {"$project": {
            "department": "$_id", "headcount": 1,
            "flat_logs": {"$reduce": {"input": "$all_logs", "initialValue": [], "in": {"$concatArrays": ["$$value", "$$this"]}}}
        }},
        {"$unwind": {"path": "$flat_logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$first": "$headcount"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [{"$in": ["$flat_logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$eq": ["$flat_logs.is_weekday", True]}]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$flat_logs.half_day", False]}, True]}, 0.5, 1.0]}, 0
                    ]
                }
            },
            "valid_work_hours": {
                "$push": {
                    "$cond": [
                        {"$and": [{"$in": ["$flat_logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$flat_logs.work_hours", None]}]},
                        "$flat_logs.work_hours", "$$REMOVE"
                    ]
                }
            },
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$flat_logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$flat_logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$flat_logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$flat_logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}}
    ]
    
    results = db.employees.aggregate(pipeline)
    items = []
    for r in results:
        vwh = r["valid_work_hours"]
        avg = round_half_up(sum(vwh)/len(vwh), 2) if vwh else None
        items.append({
            "department": r["_id"], "headcount": r["headcount"], "present_days": r["present_days"],
            "avg_work_hours": avg, "late_count": r["late_count"], "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"]
        })
    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start_dt, end_dt = get_month_dates(month)
    start_str, end_str = start_dt.isoformat(), end_dt.isoformat()
    match_emp = {}
    if department: match_emp["department"] = department
    
    pipeline = [
        {"$match": {"date": {"$gte": start_str, "$lte": end_str}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp_info"}},
        {"$unwind": "$emp_info"},
        {"$match": {f"emp_info.{k}": v for k, v in match_emp.items()}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}}
    ]
    
    res = db.attendance_logs.aggregate(pipeline)
    items = []
    for r in res:
        items.append({
            "rank": r["rank"], "emp_code": r["_id"], "name": r["emp_info"]["name"],
            "department": r["emp_info"]["department"], "total_late_minutes": r["total_late_minutes"], "late_count": r["late_count"]
        })
    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    if from_date > to_date: raise HTTPException(422, "from > to")
    f_dt, t_dt = datetime.strptime(from_date, "%Y-%m-%d").date(), datetime.strptime(to_date, "%Y-%m-%d").date()
    if (t_dt - f_dt).days > 92: raise HTTPException(422, "Range exceeds 92 days")
    if db.employees.count_documents({"department": department}) == 0: raise HTTPException(404, "Unknown department")
        
    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0}},
        {"$densify": {
            "field": "date_obj",
            "range": {
                "step": 1, "unit": "day",
                "bounds": [datetime.combine(f_dt, datetime.min.time()), datetime.combine(t_dt + timedelta(days=1), datetime.min.time())]
            }
        }},
        {"$project": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$date_obj"}, [2, 3, 4, 5, 6]]}
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}}
            ],
            "as": "emps"
        }},
        {"$addFields": {"headcount": {"$size": "$emps"}}},
        {"$project": {"emps": 0}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
                {"$unwind": "$emp"},
                {"$match": {"$expr": {"$eq": ["$emp.department", department]}}}
            ],
            "as": "logs"
        }},
        {"$addFields": {
            "present_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs", "as": "l",
                        "in": {
                            "$cond": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]}, 0.5, 1.0]}, 0
                            ]
                        }
                    }
                }
            },
            "late_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs", "as": "l",
                        "in": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]}
                    }
                }
            }
        }},
        {"$project": {"logs": 0}},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
            "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, {"$round": ["$moving_avg_7d", 4]}]}
        }}
    ]
    
    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., enum=["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None,
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100)
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            if date_from and date_to and date_from > date_to: raise HTTPException(422)
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status: q["status"] = status
        
        skip = (page - 1) * page_size
        explain = db.command("explain", {
            "find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1},
            "skip": skip, "limit": page_size
        }, verbosity="executionStats")
        coll = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422)
        start_dt, end_dt = get_month_dates(month)
        start_str, end_str = start_dt.isoformat(), end_dt.isoformat()
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_str, "$lte": end_str}}},
            {"$addFields": {"date_dt": {"$dateFromString": {"dateString": "$date"}}}},
            {"$group": {"_id": None}}
        ]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        coll = "attendance_logs"
        
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422)
        start_dt, end_dt = get_month_dates(month)
        start_str, end_str = start_dt.isoformat(), end_dt.isoformat()
        match_stage = {"joined_on": {"$lte": end_str}}
        if department: match_stage["department"] = department
        pipeline = [{"$match": match_stage}, {"$lookup": {"from": "attendance_logs", "as": "logs"}}]
        explain = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        coll = "employees"
        
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422)
        start_dt, end_dt = get_month_dates(month)
        start_str, end_str = start_dt.isoformat(), end_dt.isoformat()
        pipeline = [{"$match": {"date": {"$gte": start_str, "$lte": end_str}, "late_minutes": {"$gt": 0}}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        coll = "attendance_logs"
        
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date: raise HTTPException(422)
        pipeline = [{"$match": {"department": department}}]
        explain = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        coll = "employees"

    return {"endpoint": endpoint, "collection": coll, "explain": explain}
