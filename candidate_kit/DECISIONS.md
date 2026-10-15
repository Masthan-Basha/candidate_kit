# DECISIONS.md

## 1. Indexes

I created three indexes:

- **`employees.emp_code` (unique)** — Enforces the uniqueness guarantee required by the spec. Every lookup, punch-in check, and join goes through `emp_code`, so this index is hit constantly. The unique constraint also makes the concurrent create safe without a check-then-insert pattern.

- **`attendance_logs(emp_code, date)` (unique composite)** — The natural key for a log record. Every punch-in, punch-out, regularize, and analytics query filters on one or both of these fields. The uniqueness constraint replaces the read-before-insert race condition with a clean `DuplicateKeyError`. I considered separating them into two single-field indexes, but a compound index also satisfies leading-prefix queries on `emp_code` alone, so it serves double duty.

- **`attendance_logs(date DESC, emp_code ASC)`** — Serves `GET /attendance` which sorts by date descending then emp_code ascending. Without this index the collection scan at 100k records would be slow.

**Index I considered and rejected:** A separate single-field index on `attendance_logs.emp_code`. The compound `(emp_code, date)` index already covers all queries that filter on `emp_code` alone (it satisfies the leading-prefix), so an extra single-field index would be redundant storage and write overhead.

## 2. Punch-in race condition

Two identical punch-in requests arrive simultaneously for the same `(emp_code, date)`. My code goes straight to `insert_one` without a prior `find_one`. Both requests reach MongoDB at roughly the same time and both attempt to insert a document with the same composite key. MongoDB's unique index on `(emp_code, date)` allows exactly one insert to succeed; the other gets a `DuplicateKeyError` which I catch and translate into an HTTP 409. The request that succeeded has already returned 201. There is no window between check and insert where both could succeed, because the uniqueness is enforced atomically inside MongoDB.

## 3. Ties on the late leaderboard

If two employees are both tied at, say, 45 late minutes and the `limit` is 2, and they are the second-highest scorers (rank 2), both receive `rank = 2` and both are included in the response even though that makes three rows total. This is standard competition ranking ("1, 2, 2, 4" not "1, 2, 3, 4"). The spec says: "return every employee whose rank ≤ limit", so ties at the boundary are always included. The sort order among tied employees is `emp_code` ascending (lexicographic) for a stable, deterministic result.

## 4. Headcount with zero logs

`GET /analytics/departments/summary` and the trend endpoint both derive headcount directly from the `employees` collection, not from `attendance_logs`. I aggregate employees filtered by `joined_on <= month_end` (R9) and group by department. A department's headcount is therefore correct even when some or all of its employees have zero attendance records for the month. The attendance aggregation is a separate pipeline that I then join in Python by department name, falling back to zero-values for any department whose employees have no logs.

## 5. One thing I would change for 100× the data

I would move the `present_days` computation out of Python and into the MongoDB aggregation pipeline using `$addFields` with `$dayOfWeek` and a conditional sum. Currently I push all records into an array and iterate them in Python to check `weekday() < 5`. At 100x scale (millions of records) that means transferring large result arrays from MongoDB to the application. Pushing the weekday check into the aggregation (MongoDB's `$dayOfWeek` returns 1=Sun … 7=Sat, so weekdays are 2–6) would let the database aggregate to a single number and transfer only that, keeping network traffic and memory constant regardless of record count.
