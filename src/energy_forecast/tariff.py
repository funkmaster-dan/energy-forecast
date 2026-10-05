"""Resolve wall-clock tariffs using actual UTC realizations, including folds."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def anchor_day(rule, local):
    minute = local.hour * 60 + local.minute + local.second / 60
    start = rule.start.hour * 60 + rule.start.minute
    end = rule.end.hour * 60 + rule.end.minute
    day = local.date()
    if start == end:
        active = True
    elif start < end:
        active = start <= minute < end
    else:
        active = minute >= start or minute < end
        if minute < end:
            day -= timedelta(days=1)
    return day if active else None


def resolve(config, instant):
    local = instant.astimezone(ZoneInfo(config.timezone))
    matches = []
    for rule in config.tariff:
        day = anchor_day(rule, local)
        if (
            day
            and day.weekday() in rule.weekdays
            and day.month in rule.months
            and (not rule.dates or day in rule.dates)
        ):
            matches.append((rule, day))
    if not matches:
        raise ValueError(f"Uncovered tariff at {local.isoformat()}")
    priority = max(r.priority for r, _ in matches)
    winners = [(r, day) for r, day in matches if r.priority == priority]
    if len(winners) != 1:
        raise ValueError(f"Ambiguous tariff at {local.isoformat()}")
    rule, day = winners[0]
    return {**rule.model_dump(mode="json"), "window_id": f"{day.isoformat()}:{rule.name}"}


def boundaries(config, start, end):
    """Exact rule transitions. Round-trip filters nonexistent spring wall times."""
    zone = ZoneInfo(config.timezone)
    points = {start, end}
    # A UTC offset jump can change membership even without a configured wall boundary.
    cursor = start.replace(second=0, microsecond=0)
    previous_offset = cursor.astimezone(zone).utcoffset()
    while cursor < end:
        cursor += timedelta(minutes=1)
        offset = cursor.astimezone(zone).utcoffset()
        if offset != previous_offset and start < cursor < end:
            points.add(cursor)
        previous_offset = offset
    day = start.astimezone(zone).date() - timedelta(days=1)
    last = end.astimezone(zone).date() + timedelta(days=1)
    while day <= last:
        for rule in config.tariff:
            for wall in (rule.start, rule.end):
                for fold in (0, 1):
                    local = datetime.combine(day, wall, zone).replace(fold=fold)
                    utc = local.astimezone(start.tzinfo)
                    if (
                        utc.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None)
                        and start < utc < end
                    ):
                        points.add(utc)
        day += timedelta(days=1)
    return sorted(points)


def validate(config):
    # All weekday/month combinations, plus explicit exceptional dates and preceding days.
    days = set()
    for year in (2025, 2026, 2027, 2028):
        for month in range(1, 13):
            day = datetime(year, month, 10).date()
            days.update(day + timedelta(days=i) for i in range(7))
    for rule in config.tariff:
        if rule.start.second or rule.end.second or rule.start.microsecond or rule.end.microsecond:
            raise ValueError("Tariff times must have minute precision")
        for day in rule.dates:
            days.update((day - timedelta(days=1), day, day + timedelta(days=1)))
    zone = ZoneInfo(config.timezone)
    times = {0}
    for r in config.tariff:
        times.update((r.start.hour * 60 + r.start.minute, r.end.hour * 60 + r.end.minute))
    for day in days:
        for minute in sorted(times):
            local = datetime.combine(day, datetime.min.time(), zone) + timedelta(minutes=minute)
            resolve(config, local)
