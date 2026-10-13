# DECISIONS.md

1. **Indexes.** `employees.emp_code` (unique) and `attendance_logs (emp_code, date)` (unique) give identity, make punch-in race-safe and serve per-employee reads. `(date desc, emp_code)` serves the list sort and every month-range scan; `(status, date, emp_code)` serves status filters; `(emp_code, punch_in desc)` finds the record to punch out; `(department, ...)` on employees serves list, summary and trend. Rejected: an index on `late_minutes`; the date range already narrows the scan, so it was not worth the write cost.
2. **Punch-in race.** Both requests call `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed; the other gets `DuplicateKeyError` and I return 409. There is no read-then-write.
3. **Ties.** Ranking uses `$rank` over the totals before `limit` is applied, then keeps `rank <= limit`. A tie across the cutoff is therefore returned in full, so there can be more than `limit` rows.
4. **Headcount.** The summary pipeline starts from `employees` (joined on or before month end) and `$lookup`s logs with `preserveNullAndEmptyArrays`; every employee adds 1, logs or not.
5. **100x data.** Keep daily and monthly rollups, updated on write, and read those instead of scanning raw logs. The trend pipeline gathers `joined_on` values into one document, which would not scale. Shard by `emp_code`.
