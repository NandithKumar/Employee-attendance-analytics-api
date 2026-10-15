"""Pure rule/serialization tests; these do not require a running MongoDB server."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from app.main import (
    PunchInIn,
    RegularizeIn,
    api_doc,
    attendance_day,
    compute_late_minutes,
    compute_overtime,
    derived_fields,
    round_half_up,
    shift_end_for_record,
)

UTC = timezone.utc


class AttendanceRuleTests(unittest.TestCase):
    def test_strict_ten_minute_grace_boundary(self):
        # 09:30 IST is 04:00 UTC.
        self.assertEqual(compute_late_minutes(datetime(2026, 7, 6, 4, 10, 0, tzinfo=UTC), "09:30"), 0)
        self.assertEqual(compute_late_minutes(datetime(2026, 7, 6, 4, 10, 1, tzinfo=UTC), "09:30"), 10)
        self.assertEqual(compute_late_minutes(datetime(2026, 7, 6, 4, 45, 59, tzinfo=UTC), "09:30"), 45)

    def test_half_up_work_hour_rounding(self):
        employee = {"shift_start": "09:30", "shift_end": "18:30"}
        punch_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
        result = derived_fields({
            "status": "PRESENT", "punch_in": punch_in,
            "punch_out": punch_in + timedelta(seconds=450), "date": "2026-07-06",
        }, employee)
        self.assertEqual(result["work_hours"], 0.13)  # 0.125 rounds half-up
        self.assertTrue(result["half_day"])

    def test_overnight_attendance_date_boundary(self):
        employee = {"shift_start": "22:00", "shift_end": "06:00"}
        before_end = datetime(2026, 7, 7, 0, 29, 0, tzinfo=UTC)  # 05:59 IST
        at_end = datetime(2026, 7, 7, 0, 30, 0, tzinfo=UTC)     # 06:00 IST
        self.assertEqual(attendance_day(before_end, employee).isoformat(), "2026-07-06")
        self.assertEqual(attendance_day(at_end, employee).isoformat(), "2026-07-07")

    def test_overnight_overtime_uses_next_day_shift_end(self):
        employee = {"shift_start": "22:00", "shift_end": "06:00"}
        end = shift_end_for_record("2026-07-06", employee)
        self.assertEqual(end, datetime(2026, 7, 7, 0, 30, tzinfo=UTC))
        self.assertEqual(compute_overtime(end + timedelta(minutes=29, seconds=59), employee, "2026-07-06"), 0)
        self.assertEqual(compute_overtime(end + timedelta(minutes=30), employee, "2026-07-06"), 30)

    def test_timestamp_input_is_strict_and_extras_are_ignored(self):
        valid = PunchInIn(emp_code="EMP0001", punched_at=1783312500000, work_hours=999)
        self.assertEqual(valid.punched_at, 1783312500000)
        for invalid in ("1783312500000", 1783312500000.0, None, 1783312500):
            with self.subTest(value=invalid), self.assertRaises(Exception):
                PunchInIn(emp_code="EMP0001", punched_at=invalid)

    def test_regularization_rejects_explicit_null_time(self):
        with self.assertRaises(Exception):
            RegularizeIn(reason="Fix a missed punch", regularized_by="manager", punch_in=None)

    def test_legacy_attendance_defaults_and_no_internal_id(self):
        result = api_doc({
            "_id": "internal-only", "emp_code": "EMP0001", "date": "2026-07-06", "status": "ABSENT",
        })
        self.assertNotIn("_id", result)
        self.assertEqual(result["history"], [])
        self.assertFalse(result["half_day"])
        self.assertEqual(result["late_minutes"], 0)
        self.assertIsNone(result["punch_in"])
        self.assertIsNone(result["punch_out"])

    def test_decimal_half_up_helper(self):
        self.assertEqual(round_half_up(Decimal("1.225"), 2), 1.23)


if __name__ == "__main__":
    unittest.main()
