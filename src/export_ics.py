# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "aiohttp>=3.8.5,<4",
#     "async-cache==1.1.1",
#     "ics==0.7.2",
#     "tatsu==5.8.3",
# ]
# ///
# uv metadata, Russian prompts, and terminal output are intentional in this CLI.
# ruff: noqa: ERA001, RUF001, T201
"""Export the personal ITMO schedule once, without running a web server."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from getpass import getpass
from pathlib import Path
from tempfile import NamedTemporaryFile

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout
from ics import Calendar

from auth import get_access_token
from calendar_processing import build_calendar
from lessons_to_events import raw_lesson_to_event
from main_api import get_raw_lessons
from pages_export import validate_calendar_token, write_pages
from schedule_overrides import apply_schedule_overrides


def _public_lesson(lesson: dict) -> dict:
    """Only publish the fields needed to display when and where a class meets."""
    fields = ("date", "time_start", "time_end", "subject", "type", "room", "building", "additional")
    return {**{key: lesson[key] for key in fields if key in lesson}, "zoom_url": None}


async def fetch_calendar(username: str, password: str, *, public: bool = False) -> Calendar:
    async with ClientSession(timeout=ClientTimeout(total=120)) as session:
        token = await get_access_token(session, username, password)
        lessons = await get_raw_lessons(session, token)
        lessons = apply_schedule_overrides(lessons)
        if public:
            lessons = map(_public_lesson, lessons)
        calendar = build_calendar(map(raw_lesson_to_event, lessons))
    return calendar


async def export_schedule(username: str, password: str, output: Path) -> int:
    calendar = await fetch_calendar(username, password)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(calendar.serialize().encode("utf-8"))
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(calendar.events)


async def export_pages(username: str, password: str, output: Path, token: str) -> int:
    validate_calendar_token(token)
    calendar = await fetch_calendar(username, password, public=True)
    write_pages(calendar, output, token)
    return len(calendar.events)


def _read_credentials(parser: argparse.ArgumentParser, *, interactive: bool = True) -> tuple[str, str]:
    username = os.environ.get("ITMO_ICAL_ISU_USERNAME", "").strip()
    password = os.environ.get("ITMO_ICAL_ISU_PASSWORD", "")
    if interactive:
        username = username or input("Логин ИСУ: ").strip()
        password = password or getpass("Пароль ИСУ (ввод скрыт): ")
    if not username or not password:
        parser.error("Задайте ITMO_ICAL_ISU_USERNAME и ITMO_ICAL_ISU_PASSWORD в GitHub Secrets или окружении.")
    return username, password


def _run_export(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    interactive = not (args.non_interactive or args.pages_dir or os.environ.get("CI")) and sys.stdin.isatty()
    username, password = _read_credentials(parser, interactive=interactive)
    print("Получаю расписание из my.itmo.ru…")
    if args.pages_dir:
        calendar_token = os.environ.get("ITMO_ICAL_CALENDAR_TOKEN", "")
        try:
            validate_calendar_token(calendar_token)
        except ValueError:
            parser.error("ITMO_ICAL_CALENDAR_TOKEN: нужны 32–128 символов A–Z, a–z, 0–9, _ или -.")
        count = asyncio.run(export_pages(username, password, args.pages_dir, calendar_token))
        print(f"Подготовлено занятий для GitHub Pages: {count}.")
    else:
        output = args.output or Path("schedule.ics")
        count = asyncio.run(export_schedule(username, password, output))
        print(f"Сохранено занятий: {count}. Файл: {output.resolve()}")
    if count == 0:
        print("ИТМО вернул пустое расписание за текущий учебный год.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Выгрузка личного расписания ИТМО в .ics или на GitHub Pages.",
    )
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Файл результата (по умолчанию: schedule.ics).",
    )
    destination.add_argument(
        "--pages-dir",
        type=Path,
        help="Создать каталог для GitHub Pages; нужен секрет ITMO_ICAL_CALENDAR_TOKEN.",
    )
    parser.add_argument("--non-interactive", action="store_true", help="Читать данные входа только из окружения.")
    args = parser.parse_args()

    try:
        return _run_export(parser, args)
    except ClientResponseError as error:
        parser.exit(1, f"Ошибка ИТМО: HTTP {error.status}. Проверьте доступность сайта и данные входа.\n")
    except (ClientError, TimeoutError):
        parser.exit(1, "Не удалось подключиться к ИТМО. Проверьте интернет и повторите попытку.\n")
    except (AssertionError, KeyError, TypeError, ValueError):
        parser.exit(
            1,
            "Публикация не выполнена: расписание пустое, ответ ИТМО изменился или каталог результата уже существует. "
            "Проверьте данные входа и настройки.\n",
        )
    except OSError as error:
        parser.exit(1, f"Ошибка доступа к файлу: {error.strerror}.\n")
    except (KeyboardInterrupt, EOFError):
        parser.exit(130, "\nВыгрузка отменена.\n")


if __name__ == "__main__":
    raise SystemExit(main())
