"""The expected sessions below are transcribed independently from the teacher's photo."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ics import Calendar

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

import export_ics  # noqa: E402
from lessons_to_events import raw_lesson_to_event  # noqa: E402
from schedule_overrides import apply_schedule_overrides  # noqa: E402

SUBJECT = "Органическая химия"
LAB = "Лабораторные занятия"
PRACTICE = "Практические занятия"
LECTURE = "Лекции"

# Date, lab periods, practice periods: only cells containing group 2, including "2,3" and "2(доп)".
PHOTO_GROUP_2 = [
    ("2026-09-04", (3, 4), (5, 6)),
    ("2026-09-11", (3, 4), ()),
    ("2026-09-18", (), (3, 4)),
    ("2026-09-25", (1, 2), ()),
    ("2026-10-02", (3, 4), (1, 2)),
    ("2026-10-16", (1, 2), (3, 4)),
    ("2026-10-23", (3, 4), ()),
    ("2026-10-30", (), (3, 4)),
    ("2026-11-13", (3, 4), (1, 2)),
    ("2026-11-27", (1, 2), (3, 4)),
    ("2026-12-11", (), (1, 2)),
]
BELL_TIMES = (
    ("08:10", "09:40"),
    ("09:50", "11:20"),
    ("11:30", "13:00"),
    ("13:30", "15:00"),
    ("15:30", "17:00"),
    ("17:10", "18:40"),
)


def lesson(date="2026-11-13", kind=PRACTICE, start="08:10", end="09:40", **fields):
    return {
        "date": date,
        "type": kind,
        "time_start": start,
        "time_end": end,
        "subject": SUBJECT,
        "zoom_url": None,
        **fields,
    }


class ScheduleOverrideTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((PROJECT / "config" / "schedule-overrides.json").read_text(encoding="utf-8"))
        self.source = [
            lesson("2026-09-08", LECTURE),
            lesson("2026-11-06", LAB, "13:30", "15:00", room="1329"),
            lesson(room="1220", building="Main building"),
            lesson(start="09:50", end="11:20", room="1220", building="Main building"),
            lesson("2026-11-20", LAB, room="1329"),
        ]

    def test_every_group_2_session_matches_photo_and_bell_times(self):
        corrected = apply_schedule_overrides(self.source)
        actual = {
            (row["date"], row["type"], row["time_start"], row["time_end"])
            for row in corrected
            if row["type"] in (LAB, PRACTICE)
        }
        expected = {
            (day, kind, *BELL_TIMES[number - 1])
            for day, labs, practices in PHOTO_GROUP_2
            for kind, numbers in ((LAB, labs), (PRACTICE, practices))
            for number in numbers
        }
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 32)
        self.assertEqual(sum(row["type"] == LAB for row in corrected), 16)
        self.assertEqual(sum(row["type"] == PRACTICE for row in corrected), 16)
        self.assertNotIn("2026-11-20", {row["date"] for row in corrected})

    def test_only_labs_and_practices_in_this_semester_are_replaced(self):
        unchanged = [
            lesson("2026-11-10", LECTURE),
            lesson("2027-01-18", "Экзамен"),
            lesson("2027-01-11", "Консультация к экзамену"),
            lesson("2026-09-03", LAB),
            lesson(subject="Неорганическая химия"),
            lesson("2027-09-10", LAB),
        ]
        source = self.source + unchanged
        snapshot = copy.deepcopy(source)
        corrected = apply_schedule_overrides(source)
        for row in unchanged:
            self.assertIn(row, corrected)
        self.assertEqual(source, snapshot)

    def test_no_old_semester_is_added_to_next_year_or_another_course(self):
        for source in ([], [lesson("2027-09-10", LAB)], [lesson(subject="Algorithms")]):
            with self.subTest(source=source):
                self.assertEqual(apply_schedule_overrides(source), source)

    def test_missing_or_conflicting_room_is_not_invented(self):
        conflicting = [lesson(room="301"), lesson(start="09:50", end="11:20", room="405")]
        corrected = apply_schedule_overrides(self.source + conflicting)
        practice = [row for row in corrected if row["date"] == "2026-11-13" and row["type"] == PRACTICE]
        self.assertTrue(all("room" not in row for row in practice))
        new_labs = [row for row in corrected if row["date"] == "2026-11-13" and row["type"] == LAB]
        self.assertTrue(all("room" not in row for row in new_labs))

    def test_unambiguous_same_day_room_survives_and_additional_lab_is_labelled(self):
        corrected = apply_schedule_overrides(self.source)
        practice = [row for row in corrected if row["date"] == "2026-11-13" and row["type"] == PRACTICE]
        self.assertTrue(all(row["room"] == "1220" for row in practice))
        additional = [row for row in corrected if row.get("additional")]
        self.assertEqual(len(additional), 2)
        self.assertTrue(all(row["date"] == "2026-11-27" and row["type"] == LAB for row in additional))
        self.assertTrue(all(raw_lesson_to_event(row).name.endswith("(доп.)") for row in additional))

    def test_repeated_application_keeps_events_and_uids_without_duplicates(self):
        once = apply_schedule_overrides(self.source)
        twice = apply_schedule_overrides(once)
        self.assertEqual(once, twice)
        uids = [raw_lesson_to_event(row).uid for row in twice]
        self.assertEqual(len(uids), len(set(uids)))

    def test_invalid_period_and_duplicate_are_rejected(self):
        for periods in ([0], [7], [1, 1], []):
            with self.subTest(periods=periods):
                config = copy.deepcopy(self.config)
                config["rules"][0]["lessons"][0]["periods"] = periods
                with self.assertRaises(ValueError):
                    apply_schedule_overrides(self.source, config)

    def test_disabled_rules_preserve_api_schedule(self):
        self.config["rules"] = []
        self.assertEqual(apply_schedule_overrides(self.source, self.config), self.source)


class AutomaticExportCorrectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_pages_applies_corrections_on_every_fetch_including_additional_label(self):
        token = "photo-test-calendar-address-1234567890"
        with (
            patch.object(export_ics, "get_access_token", AsyncMock(return_value="not-a-real-token")),
            patch.object(export_ics, "get_raw_lessons", AsyncMock(return_value=[lesson("2026-09-08", LECTURE)])),
            tempfile.TemporaryDirectory() as directory,
        ):
            root = Path(directory) / "site"
            self.assertEqual(await export_ics.export_pages("user", "password", root, token), 33)
            calendar = Calendar((root / token / "schedule.ics").read_text(encoding="utf-8"))
        additional = [event for event in calendar.events if "(доп.)" in event.name]
        self.assertEqual(len(additional), 2)
        self.assertTrue(all(event.begin.to("+03:00").format("YYYY-MM-DD") == "2026-11-27" for event in additional))
        self.assertTrue(all((event.end - event.begin).total_seconds() == 90 * 60 for event in calendar.events))


if __name__ == "__main__":
    unittest.main()
