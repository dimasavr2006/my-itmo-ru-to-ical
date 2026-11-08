# ruff: noqa: RUF001
"""Build a static calendar subscription without publishing credentials or an address index."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from ics import Calendar
from ics.grammar.parse import ContentLine

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")

_PAGE_START = """<!doctype html>
<html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive">
<meta name="referrer" content="no-referrer">
<title>Расписание ИТМО</title>
<style>
body{font:17px/1.6 system-ui,sans-serif;background:#f4f6fa;color:#15253d;margin:0;padding:8vh 24px}
main{max-width:640px;margin:auto;background:white;padding:32px;border-radius:16px}
h1{line-height:1.2}a{color:#155bd7}li{margin:12px 0}small{color:#56657b}
</style><main>
"""

_ROOT_PAGE = (
    _PAGE_START
    + """<h1>Расписание ИТМО</h1>
<p>Откройте личную ссылку на календарь из инструкции по настройке.</p>
</main></html>
"""
)


def validate_calendar_token(token: str) -> None:
    if not _TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Calendar token must contain 32–128 URL-safe characters")


def _calendar_page(event_count: int) -> str:
    updated = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M МСК")
    return (
        _PAGE_START
        + f"""<h1>Моё расписание ИТМО</h1>
<p>Занятий: {event_count}.<br><small>Последнее успешное обновление: {updated}.</small></p>
<ul>
<li><a id="subscribe" href="schedule.ics">Подписаться в Apple Calendar / Outlook</a></li>
<li><a id="google" href="https://calendar.google.com/">Добавить в Google Календарь</a></li>
<li><a href="schedule.ics" download>Скачать .ics для разового импорта</a></li>
</ul>
<p>Для автоматического обновления добавьте календарь по URL или оформите подписку.
Импорт файла сохранит только текущую копию расписания.</p>
<p><small>Частоту получения изменений определяет ваше календарное приложение.
Календарь доступен всем, у кого есть ссылка.</small></p>
<script>
const calendar = new URL("schedule.ics", window.location.href);
document.getElementById("subscribe").href = calendar.href.replace(/^https?:/, "webcal:");
document.getElementById("google").href =
  "https://calendar.google.com/calendar/render?cid=" + encodeURIComponent(calendar.href);
</script></main></html>
"""
    )


def write_pages(calendar: Calendar, output: Path, token: str) -> None:
    """Publish a fresh site atomically, failing before replacing a known-good calendar."""
    validate_calendar_token(token)
    if not calendar.events:
        raise ValueError("Refusing to publish an empty schedule")
    if output.exists():
        raise ValueError("Pages output must be a new directory")

    calendar.extra.extend(
        [
            ContentLine(name="X-WR-CALNAME", value="Расписание ИТМО"),
            ContentLine(name="X-WR-TIMEZONE", value="Europe/Moscow"),
            ContentLine(name="REFRESH-INTERVAL", params={"VALUE": ["DURATION"]}, value="PT6H"),
            ContentLine(name="X-PUBLISHED-TTL", value="PT6H"),
        ],
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as temporary:
        site = Path(temporary) / "site"
        calendar_dir = site / token
        calendar_dir.mkdir(parents=True)
        (calendar_dir / "schedule.ics").write_bytes(calendar.serialize().encode("utf-8"))
        (calendar_dir / "index.html").write_text(_calendar_page(len(calendar.events)), encoding="utf-8")
        (site / "index.html").write_text(_ROOT_PAGE, encoding="utf-8")
        (site / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
        site.replace(output)
