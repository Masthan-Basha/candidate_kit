Employee Attendance & Analytics API
A FastAPI + MongoDB API for employee attendance tracking with punch-in/out, manual corrections with an audit trail, and analytics endpoints.

Stack
Python 3.11+, FastAPI, PyMongo, Pydantic v2
MongoDB 6.0+ (local or Atlas M0)
Local setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Create .env with:
# MONGO_URI=mongodb://localhost:27017
# MONGO_DB=attendance_db

python sample_seed.py          # load sample data
uvicorn app.main:app --port 8000 --reload
Interactive docs: http://localhost:8000/docs

Endpoints
Method	Path	Description
GET	/health	Readiness probe (pings MongoDB)
POST	/employees	Create employee
GET	/employees	List employees (paginated, filterable by department)
POST	/attendance/punch-in	Record punch-in
POST	/attendance/punch-out	Record punch-out
GET	/attendance	List attendance records (paginated, filterable)
PATCH	/attendance/{emp_code}/{date}	Regularize a record (audit trail)
GET	/analytics/employees/{emp_code}/monthly	Monthly summary for one employee
GET	/analytics/departments/summary	Department summary for a month
GET	/analytics/leaderboard/late	Late-comers leaderboard
GET	/analytics/departments/{department}/trend	Daily attendance trend
GET	/admin/explain/{endpoint}	MongoDB explain for an endpoint's main query
Key implementation notes
All timestamps in the API are epoch milliseconds (integer); validated to be ≥ 100 000 000 000.
Attendance dates use IST (UTC+05:30) with the overnight shift rule (R1).
Derived fields (late_minutes, work_hours, overtime_minutes, half_day) are computed at write time and stored; analytics read stored values.
Concurrent punch-in and create-employee conflicts are handled by unique indexes + catching DuplicateKeyError — no check-then-insert race.
Indexes created at startup: unique employees.emp_code, unique compound attendance_logs(emp_code, date), and attendance_logs(date DESC, emp_code ASC) for list queries.
What is complete
All endpoints described in openapi.yaml are implemented, including the /admin/explain/{endpoint} endpoint with executionStats verbosity.

What is missing / known limitations
The department trend endpoint computes moving_avg_7d in Python rather than inside the MongoDB aggregation. The $densify stage is used for gap-filling but the 7-day windowed average is computed in the loop.
No authentication (per spec — "no authentication in this assignment").
