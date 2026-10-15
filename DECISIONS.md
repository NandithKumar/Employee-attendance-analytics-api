# DECISIONS.md

1. **Indexes.** A unique `employees.emp_code` index enforces public identity. Department/code and department/join-date indexes support lists and headcount. Attendance uses unique `(emp_code, date)` for one row per employee-day, `(date, emp_code)` for sorted listing and month scans, `(emp_code, date, status)` for employee analytics, and `(emp_code, punch_in)` for punch-out lookup. Explain plans should justify each index because indexes cost write time and storage.

2. **Punch-in race.** Requests may both see no row, but only one insert can satisfy the unique employee-day index. MongoDB accepts one; the duplicate-key error becomes 409.

3. **Leaderboard ties.** `$rank` ranks by total late minutes, so ties share a rank and the next rank is skipped. Filtering `rank <= limit` after ranking includes everyone tied at the cutoff, even if that exceeds `limit`. Display order is late minutes descending, then employee code ascending.

4. **Headcount.** The summary starts from employees who joined by month-end and looks up attendance. Employees with no logs remain in the aggregation; later joiners are excluded.

5. **100x scale.** I would measure latency and query plans first. If analytics became expensive, I would assess index selectivity, pre-aggregated daily metrics and read scaling, guided by measurements rather than adding indexes or caches blindly.
