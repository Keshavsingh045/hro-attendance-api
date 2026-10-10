# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Used `.replace(hour=h, minute=m)` without setting `tzinfo` correctly, leading to naive datetime math. Did not handle dates properly. | Submitting an overnight shift punch-in yields incorrect late minutes. | Rewrote to use IST aware datetimes combined with the correct attendance date. |
| 2 | `compute_work_hours` | Return value was rounded but float representation could cause precision issues. Did not use half-up rounding. | E.g. `4.495` might round to `4.49` instead of `4.50`. | Replaced with `decimal.Decimal` and `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | Did not account for overnight shifts where `shift_end` is on the next calendar day. | An overnight shift end time of `06:00` would be evaluated on the same day as `punch_in`, leading to negative/wrong overtime. | Computed the correct end date for overnight shifts before subtracting. |
| 4 | `create_employee` | Uses `datetime.now()` instead of epoch milliseconds and uses naive datetime. | Returned body has `created_at` as an ISO string instead of integer epoch milliseconds. | Used `datetime.now(timezone.utc)`, truncated to seconds, and converted to epoch ms. |
| 5 | `list_employees` | Pagination calculation `skip = page * page_size` is wrong. Skips the first page when `page=1`. | API call with `page=1` skips the first 20 records. | Changed to `skip = (page - 1) * page_size`. |
| 6 | `punch_in` | Uses check-then-insert which causes race conditions. Returns `_id` as `id` which violates contract. Timezone mapping is naive. | Concurrent requests get duplicated records. Response schema has extra `id` field. | Rely on MongoDB unique index `(emp_code, date)` and catch `DuplicateKeyError`. Removed `id` field. |
| 7 | `list_attendance` | Loads all documents into memory `list(db.attendance_logs.find(q))` before sorting. Crashes on large collections. | OOM crash or slow response on 100,000+ records. | Passed sort, skip, limit directly to the MongoDB cursor. |

**Not a defect:**
* `shift_start` and `shift_end` defaults in `EmployeeIn`. I decided to keep them since Pydantic handles validation, though they were adjusted to enforce regex matching.
