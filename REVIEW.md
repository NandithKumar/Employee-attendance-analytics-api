# REVIEW.md

The starter's happy path concealed defects across the helpers and existing routes. I grouped related observations by the code path where they originate and included concrete triggers so each fix is independently checkable.

| # | Where | What is wrong | How to notice | Fix applied |
|---|---|---|---|---|
| 1 | MongoClient setup | The client returns naive datetimes by default, making API timestamp conversion ambiguous; it also uses the driver's default server-selection timeout. | Read a BSON date back and inspect `tzinfo`; stop MongoDB and observe request delay. | Use UTC-aware BSON dates and a bounded server-selection timeout. Environment variables are read at process startup. |
| 2 | `health()` | It always returns 200 without checking the database. | Stop MongoDB and call `GET /health`. | Ping MongoDB and return 503 on a driver error. |
| 3 | `EmployeeIn` | Employee codes, email, dates, shift times and field lengths are not validated; identical shift boundaries are accepted. | Try `EMP12`, an invalid date, `25:00`, an invalid email, or equal shift times. | Add contract-shaped Pydantic validation and require different start/end strings. |
| 4 | `create_employee()` | A read-before-insert duplicate check is not safe under parallel requests, and there is no unique business-key index. | Send two creates for the same `emp_code` concurrently. | Create a unique index and translate `DuplicateKeyError` to 409. |
| 5 | `create_employee()` | `created_at` is naive and its instant is returned as a datetime-like JSON value rather than epoch milliseconds. | Inspect the stored value and HTTP response. | Store UTC-aware time and convert API instants to integer milliseconds. |
| 6 | `list_employees()` | `skip = page * page_size` skips the first page. | Request page 1 with `page_size=2`. | Use `(page - 1) * page_size`. |
| 7 | `list_employees()` | `total` counts every employee, even when a department filter is applied. | Compare a department-filtered page with the unfiltered headcount. | Count using the same filter as the result query. |
| 8 | `list_employees()` | Results have no required stable `emp_code` ordering. | Repeat a paginated request or insert employees out of order. | Sort by `emp_code` ascending. |
| 9 | `list_employees()` | Invalid page values and oversized pages are not rejected. | Try page 0 or page size 101. | Validate pagination and cap page size at 100. |
| 10 | `compute_late_minutes()` | It replaces the punch timestamp's clock fields without first converting to IST and cannot choose the previous shift day for overnight shifts. | Use a UTC input near midnight with a 22:00–06:00 shift. | Normalize to UTC, convert to IST, and resolve the actual shift start date. |
| 11 | `compute_late_minutes()` | It truncates the difference to whole minutes before checking the grace period, so 09:40:01 on a 09:30 shift is incorrectly treated as not late. | Compare 09:40:00 with 09:40:01. | Apply the strict `> 600 seconds` test first, then floor elapsed minutes. |
| 12 | `compute_work_hours()` | Built-in `round()` uses ties-to-even rather than decimal half-up rounding. | Test a duration at a half-centesimal hour boundary, such as 7 minutes 30 seconds. | Calculate from integer elapsed seconds and use `Decimal` with `ROUND_HALF_UP`. |
| 13 | `compute_overtime()` | It anchors shift end to the record's same calendar date, which is wrong for overnight shifts; the aware shift-end time can also conflict with naive punch timestamps. | Punch out after midnight from a 22:00–06:00 shift. | Resolve next-day shift end where applicable and normalize instants to UTC. |
| 14 | `compute_overtime()` | Any positive overtime is counted, though the contract requires at least 30 whole minutes. | Punch out 10 minutes after shift end. | Floor elapsed minutes and return zero unless the result is at least 30. |
| 15 | `/attendance/punch-in` | Unknown employees are not handled before `emp["shift_start"]` is accessed, producing a server error instead of 404. | Punch in with an unknown employee code. | Check employee existence and return 404. |
| 16 | `/attendance/punch-in` | `datetime.fromtimestamp()` and `.date()` use the machine's local zone, not the contract's IST calendar. Input milliseconds are also not truncated to whole seconds. | Run in a different host timezone or submit milliseconds with a fractional second. | Normalize through UTC, derive the IST attendance day, and truncate at the API boundary. |
| 17 | `PunchInIn` / `/attendance/punch-in` | Timestamps are not strictly typed/range checked, explicit `0` is treated as missing, and arbitrary status strings are accepted. | Send a seconds-looking value, a float/string timestamp, or an unsupported status. | Require strict epoch milliseconds, validate the range, and restrict status to `PRESENT`, `WFH` or `ON_DUTY`. |
| 18 | `/attendance/punch-in` | Check-then-insert allows concurrent requests to race and create duplicate employee-day rows. | Run simultaneous punch-ins for the same attendance date. | Use the unique `(emp_code, date)` index as the concurrency arbiter. |
| 19 | `/attendance/punch-in` | The response exposes a synthetic `id` from MongoDB, although the contract identifies a record by `(emp_code, date)` and never exposes `_id`. | Inspect the response fields. | Remove the database identity from API responses. |
| 20 | `/attendance` | It materializes every matching record in Python, which is too expensive at 100,000 records. | Run with a large collection and inspect memory/latency. | Count, sort, skip and limit in MongoDB. |
| 21 | `/attendance` | Equal-date rows lack the required `emp_code` ascending tie-breaker. | Seed several employees on the same date. | Sort by date descending and employee code ascending. |
| 22 | `/attendance` | Date strings, status values, inverted ranges and pagination bounds are not validated consistently. | Submit malformed dates, an inverted range, invalid status, page 0 or page size 101. | Use date parsing, explicit status/range checks and bounded query parameters. |
| 23 | `/attendance` | Responses expose MongoDB IDs and BSON datetime fields rather than the specified natural key and epoch-millisecond instants. | Inspect response types and fields. | Serialize API instants to epoch milliseconds and remove internal IDs. |
| 24 | `/attendance` | Legacy seeded rows may lack `history`, `half_day` or derived counters, and the starter returns incomplete records. | Query records with optional fields omitted. | Normalize the documented default values in the response. |
| 25 | Contract coverage | Punch-out, regularization, the four analytics endpoints and explain were absent. | Call each route marked “You” in the assignment. | Implement the missing routes, aggregation pipelines, audit behavior and explain output. |

## Examined and retained

- `emp_code` is the public identity; the API never exposes MongoDB `_id`.
- Calendar dates stay as `YYYY-MM-DD` strings in MongoDB; instants are UTC BSON dates.
- Analytics operate through MongoDB aggregation pipelines rather than fetching the attendance collection into Python.
- A unique employee-day index is the correct primitive for making punch-in race-safe; an append-only history guarded by compare-and-swap prevents concurrent regularizations from silently overwriting each other's audit entries.
