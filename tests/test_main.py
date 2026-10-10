import unittest
from datetime import date, datetime, timezone

from app.main import (
    IST,
    attendance_date,
    build_department_summary,
    late_minutes_for,
    shift_end_at,
    work_hours_for,
)


class AttendanceRuleTests(unittest.TestCase):
    def test_grace_period_is_strictly_more_than_ten_minutes(self):
        employee = {"shift_start": "09:30", "shift_end": "18:30"}
        day = date(2026, 10, 12)

        exact_grace = datetime(2026, 10, 12, 4, 10, tzinfo=timezone.utc)
        beyond_grace = datetime(2026, 10, 12, 4, 10, 1, tzinfo=timezone.utc)

        self.assertEqual(late_minutes_for(exact_grace, day, employee), 0)
        self.assertEqual(late_minutes_for(beyond_grace, day, employee), 10)

    def test_overnight_punch_after_midnight_belongs_to_previous_shift_day(self):
        employee = {"shift_start": "22:00", "shift_end": "06:00"}
        punch_in = datetime(2026, 10, 11, 21, 0, tzinfo=timezone.utc)

        self.assertEqual(punch_in.astimezone(IST).date(), date(2026, 10, 12))
        self.assertEqual(attendance_date(punch_in, employee), date(2026, 10, 11))
        self.assertEqual(
            shift_end_at(date(2026, 10, 11), employee),
            datetime(2026, 10, 12, 6, 0, tzinfo=IST),
        )

    def test_work_hours_round_half_up_to_two_places(self):
        start = datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 12, 4, 0, 54, tzinfo=timezone.utc)

        self.assertEqual(work_hours_for(start, end), 0.02)


class DepartmentSummaryPipelineTests(unittest.TestCase):
    def test_lookup_results_are_unwound_before_department_accumulation(self):
        pipeline = build_department_summary("2026-10", None).pipeline
        lookup_index = next(i for i, stage in enumerate(pipeline) if "$lookup" in stage)
        unwind_index = next(i for i, stage in enumerate(pipeline) if "$unwind" in stage)
        group_index = next(
            i for i, stage in enumerate(pipeline[unwind_index + 1 :], unwind_index + 1)
            if "$group" in stage
        )

        self.assertLess(lookup_index, unwind_index)
        self.assertLess(unwind_index, group_index)
        self.assertTrue(pipeline[unwind_index]["$unwind"]["preserveNullAndEmptyArrays"])
        self.assertEqual(
            pipeline[group_index]["$group"]["present_units"]["$sum"],
            "$s.present_units",
        )


if __name__ == "__main__":
    unittest.main()
