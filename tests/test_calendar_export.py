"""Offline coverage of publication failures, private fields and subscription identity."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import ClientConnectionError
from ics import Calendar

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import export_ics  # noqa: E402
from calendar_processing import build_calendar  # noqa: E402
from lessons_to_events import raw_lesson_to_event  # noqa: E402
from main_api import _get_date_range_params  # noqa: E402
from pages_export import validate_calendar_token, write_pages  # noqa: E402

TOKEN = "aB0_-" * 8
LESSON = {
    "date": "2026-11-09",
    "time_start": "10:00",
    "time_end": "11:30",
    "subject": "Algorithms",
    "type": "Lecture",
    "room": "301",
    "building": "Main building",
    "group": "PRIVATE-GROUP",
    "teacher_name": "PRIVATE-TEACHER",
    "zoom_url": "https://example.com/PRIVATE-MEETING",
    "zoom_password": "PRIVATE-ZOOM-PASSWORD",
    "zoom_info": "PRIVATE-ZOOM-INFO",
    "note": "PRIVATE-NOTE",
}


class CalendarExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.access_token = patch.object(export_ics, "get_access_token", AsyncMock(return_value="PRIVATE-ACCESS-TOKEN"))
        self.lessons = patch.object(export_ics, "get_raw_lessons", AsyncMock(return_value=[LESSON]))
        self.access_token.start()
        self.lessons.start()
        self.addCleanup(self.access_token.stop)
        self.addCleanup(self.lessons.stop)

    async def test_pages_export_round_trips_and_removes_private_fields(self):
        output = self.root / "site"
        count = await export_ics.export_pages("PRIVATE-LOGIN", "PRIVATE-PASSWORD", output, TOKEN)
        text = (output / TOKEN / "schedule.ics").read_text(encoding="utf-8")
        parsed = Calendar(text)
        self.assertEqual(count, 1)
        event = next(iter(parsed.events))
        self.assertEqual(event.begin.hour, 7)  # 10:00 Moscow, serialized in UTC.
        self.assertEqual(event.end.hour, 8)
        self.assertEqual(event.end.minute, 30)
        self.assertEqual(event.location, "301, Main building")
        self.assertNotIn("PRIVATE-", text)
        self.assertIn("X-WR-TIMEZONE:Europe/Moscow", text)
        self.assertIn("REFRESH-INTERVAL;VALUE=DURATION:PT6H", text)
        self.assertNotIn(TOKEN, (output / "index.html").read_text(encoding="utf-8"))
        self.assertIn("Disallow: /", (output / "robots.txt").read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(str(path.relative_to(output)).replace("\\", "/") for path in output.rglob("*") if path.is_file()),
            [f"{TOKEN}/index.html", f"{TOKEN}/schedule.ics", "index.html", "robots.txt"],
        )

    async def test_one_off_export_keeps_private_details_and_creates_parent(self):
        output = self.root / "nested" / "schedule.ics"
        self.assertEqual(await export_ics.export_schedule("user", "password", output), 1)
        text = output.read_text(encoding="utf-8")
        self.assertIn("PRIVATE-ZOOM-PASSWORD", text)
        self.assertEqual(len(Calendar(text).events), 1)

    async def test_failed_fetch_preserves_previous_file(self):
        output = self.root / "schedule.ics"
        output.write_text("previous calendar", encoding="utf-8")
        with patch.object(export_ics, "get_raw_lessons", AsyncMock(side_effect=ClientConnectionError())):
            with self.assertRaises(ClientConnectionError):
                await export_ics.export_schedule("user", "password", output)
        self.assertEqual(output.read_text(encoding="utf-8"), "previous calendar")

    async def test_empty_schedule_is_never_published(self):
        output = self.root / "site"
        with patch.object(export_ics, "get_raw_lessons", AsyncMock(return_value=[])):
            with self.assertRaises(ValueError):
                await export_ics.export_pages("user", "password", output, TOKEN)
        self.assertFalse(output.exists())

    async def test_malformed_schedule_is_never_published(self):
        output = self.root / "site"
        with patch.object(export_ics, "get_raw_lessons", AsyncMock(return_value=[{"date": "broken"}])):
            with self.assertRaises(KeyError):
                await export_ics.export_pages("user", "password", output, TOKEN)
        self.assertFalse(output.exists())

    async def test_changed_room_keeps_uid_for_subscription_updates(self):
        before = await export_ics.fetch_calendar("user", "password", public=True)
        with patch.object(export_ics, "get_raw_lessons", AsyncMock(return_value=[{**LESSON, "room": "405"}])):
            after = await export_ics.fetch_calendar("user", "password", public=True)
        self.assertEqual(next(iter(before.events)).uid, next(iter(after.events)).uid)
        self.assertIn("405", next(iter(after.events)).location)


class PublicationSafetyTests(unittest.TestCase):
    def test_unsafe_tokens_are_rejected_without_network_requests(self):
        for token in ("", "short", "../" + "x" * 40, "a/" + "x" * 40, "x" * 129, "x" * 32 + "\n"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                validate_calendar_token(token)
        validate_calendar_token(TOKEN)

    def test_existing_directory_is_not_reused_after_token_rotation(self):
        calendar = build_calendar([raw_lesson_to_event(LESSON)])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "site"
            write_pages(calendar, output, TOKEN)
            before = (output / TOKEN / "schedule.ics").read_bytes()
            with self.assertRaises(ValueError):
                write_pages(calendar, output, "z" * 40)
            self.assertEqual((output / TOKEN / "schedule.ics").read_bytes(), before)
            self.assertFalse((output / ("z" * 40)).exists())

    def test_missing_secrets_fail_without_prompting(self):
        with patch.dict(os.environ, {}, clear=True), patch("builtins.input") as prompt:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                export_ics._read_credentials(argparse.ArgumentParser(), interactive=False)
            self.assertEqual(error.exception.code, 2)
            prompt.assert_not_called()

    def test_cli_error_does_not_print_server_response_or_credentials(self):
        stderr = io.StringIO()
        stdout = io.StringIO()
        environment = {"ITMO_ICAL_ISU_USERNAME": "PRIVATE-USER", "ITMO_ICAL_ISU_PASSWORD": "PRIVATE-PASSWORD"}
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(sys, "argv", ["export_ics.py", "--non-interactive"]),
            patch.object(export_ics, "export_schedule", AsyncMock(side_effect=ValueError("PRIVATE-SERVER-BODY"))),
            contextlib.redirect_stderr(stderr),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as error,
        ):
            export_ics.main()
        self.assertEqual(error.exception.code, 1)
        self.assertNotIn("PRIVATE-", stderr.getvalue() + stdout.getvalue())

    def test_academic_year_boundaries(self):
        self.assertEqual(
            _get_date_range_params(date(2026, 7, 31)), {"date_start": "2025-08-01", "date_end": "2026-07-31"}
        )
        self.assertEqual(
            _get_date_range_params(date(2026, 8, 1)), {"date_start": "2026-08-01", "date_end": "2027-07-31"}
        )
        self.assertEqual(
            _get_date_range_params(date(2027, 1, 1)), {"date_start": "2026-08-01", "date_end": "2027-07-31"}
        )


if __name__ == "__main__":
    unittest.main()
