"""Apply dated, subject-specific corrections after fetching the original ITMO schedule."""

from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path
from typing import Iterable

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "schedule-overrides.json"
_METADATA_FIELDS = ("room", "building", "teacher_name", "teacher_fio", "group")


def _bell_times(config: dict) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for start, end in config["bell_times"]:
        begin, finish = time.fromisoformat(start), time.fromisoformat(end)
        if begin >= finish:
            raise ValueError("Each period must end after it starts")
        if result and result[-1][1] > start:
            raise ValueError("Bell periods must not overlap")
        result.append((begin.strftime("%H:%M"), finish.strftime("%H:%M")))
    return result


def _same_subject(lesson: dict, rule: dict) -> bool:
    return str(lesson.get("subject") or "").strip().casefold() == rule["subject"].strip().casefold()


def _within_rule(lesson: dict, rule: dict) -> bool:
    return rule["date_start"] <= lesson["date"] <= rule["date_end"]


def _metadata(source: list[dict], rule: dict, replacement: dict) -> dict:
    """Keep a room only when same-day lessons of this type agree about it."""
    candidates = [
        lesson
        for lesson in source
        if _same_subject(lesson, rule)
        and lesson["date"] == replacement["date"]
        and lesson.get("type") == replacement["type"]
    ]
    result = {}
    for field in _METADATA_FIELDS:
        values = {lesson[field] for lesson in candidates if lesson.get(field)}
        if len(values) == 1:
            result[field] = values.pop()
    return result


def _expand_replacements(source: list[dict], rule: dict, periods: list[tuple[str, str]]) -> list[dict]:
    result = []
    seen = set()
    for replacement in rule["lessons"]:
        _validate_replacement(replacement, rule)
        metadata = _metadata(source, rule, replacement)
        for period in replacement["periods"]:
            if not 1 <= period <= len(periods):
                raise ValueError("Unknown bell period")
            key = (replacement["date"], period)
            if key in seen:
                raise ValueError("Duplicate or overlapping replacement lesson")
            seen.add(key)
            start, end = periods[period - 1]
            result.append(
                {
                    **metadata,
                    "subject": rule["subject"],
                    "date": replacement["date"],
                    "type": replacement["type"],
                    "time_start": start,
                    "time_end": end,
                    "zoom_url": None,
                    "additional": replacement.get("additional", False),
                },
            )
    return result


def _validate_replacement(replacement: dict, rule: dict) -> None:
    date.fromisoformat(replacement["date"])
    if not _within_rule(replacement, rule):
        raise ValueError("Replacement date is outside the correction window")
    if replacement["type"] not in rule["replace_types"]:
        raise ValueError("Replacement type is outside the correction rule")
    if not replacement["periods"]:
        raise ValueError("Replacement must contain at least one period")


def _apply_replacement_rules(source: list[dict], rules: list[dict], periods: list[tuple[str, str]]) -> list[dict]:
    result = source
    for rule in rules:
        date.fromisoformat(rule["date_start"])
        date.fromisoformat(rule["date_end"])
        if not any(_same_subject(lesson, rule) and _within_rule(lesson, rule) for lesson in source):
            continue
        replacements = _expand_replacements(source, rule, periods)
        result = [
            lesson
            for lesson in result
            if not (
                _same_subject(lesson, rule)
                and _within_rule(lesson, rule)
                and lesson.get("type") in rule["replace_types"]
            )
        ] + replacements
    return result


def _matches_adjustment(lesson: dict, adjustment: dict) -> bool:
    return (
        _same_subject(lesson, adjustment)
        and _within_rule(lesson, adjustment)
        and lesson.get("type") in adjustment["types"]
        and date.fromisoformat(lesson["date"]).weekday() in adjustment["weekdays"]
    )


def _shift_to_period(lesson: dict, adjustment: dict, periods: list[tuple[str, str]]) -> dict:
    period = adjustment["period"]
    if not 1 <= period <= len(periods):
        raise ValueError("Unknown adjustment bell period")
    start, end = periods[period - 1]
    shifted = {
        **lesson,
        "_original_time_start": lesson.get("_original_time_start", lesson["time_start"]),
        "time_start": start,
        "time_end": end,
    }
    if adjustment.get("clear_location"):
        shifted.pop("room", None)
        shifted.pop("building", None)
    return shifted


def _apply_adjustments(lesson: dict, adjustments: list[dict], periods: list[tuple[str, str]]) -> dict:
    result = lesson
    for adjustment in adjustments:
        if _matches_adjustment(result, adjustment):
            result = _shift_to_period(result, adjustment, periods)
    return result


def apply_schedule_overrides(lessons: Iterable[dict], config: dict | None = None) -> list[dict]:
    source = list(lessons)
    if not source:
        return source
    if config is None:
        config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    if config["version"] != 1:
        raise ValueError("Unsupported schedule corrections version")
    periods = _bell_times(config)
    result = _apply_replacement_rules(source, config["rules"], periods)
    return [_apply_adjustments(lesson, config.get("adjustments", []), periods) for lesson in result]
