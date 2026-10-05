"""Calendar imports keep explicit coverage. Missing school years stay unknown."""

import csv
from datetime import datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

import holidays
from icalendar import Calendar

from .schemas import CalendarEvent


def features(config, instant, issue_time=None):
    day = instant.astimezone(ZoneInfo(config.timezone)).date()
    result = {
        "day_of_week": day.weekday(),
        "is_weekend": day.weekday() >= 5,
        "is_public_holiday": None,
        "is_school_holiday": None,
        "is_school_day": None,
        "labels": [],
        "coverage": "disabled",
    }
    calendar = config.calendars
    if not calendar.enabled:
        return result
    if day.year not in calendar.covered_years:
        result["coverage"] = "year_missing"
        return result
    public = holidays.Australia(subdiv=calendar.public_jurisdiction, years=[day.year])
    result["is_public_holiday"] = day in public
    if day in public:
        result["labels"].append(
            {
                "label": public[day],
                "source": f"python-holidays {holidays.__version__}",
                "kind": "public_holiday",
            }
        )
    partial = holidays.Australia(
        subdiv=calendar.public_jurisdiction, years=[day.year], categories=holidays.HALF_DAY
    )
    if day in partial:
        result["labels"].append(
            {
                "label": partial[day],
                "source": f"python-holidays {holidays.__version__}",
                "kind": "partial_public_holiday",
            }
        )
        # SA/NT published part-day Christmas/New Year's Eve holidays start at 19:00.
        if (
            calendar.public_jurisdiction in ("SA", "NT")
            and instant.astimezone(ZoneInfo(config.timezone)).hour >= 19
        ):
            result["is_public_holiday"] = True
    school = {p: False for p in calendar.school_profiles}
    for event in calendar.events:
        if issue_time and event.known_at and event.known_at > issue_time:
            continue
        event_start = datetime.combine(event.start, event.start_time, ZoneInfo(config.timezone))
        event_end = datetime.combine(event.end, event.end_time, ZoneInfo(config.timezone))
        if event_start <= instant.astimezone(ZoneInfo(config.timezone)) < event_end:
            result["labels"].append(
                {
                    "label": event.label,
                    "source": event.source,
                    "version": event.version,
                    "kind": event.kind,
                }
            )
            if event.kind == "public_holiday":
                result["is_public_holiday"] = True
            if event.kind in ("school_holiday", "pupil_free") and event.profile in school:
                school[event.profile] = True
    unavailable = [
        event
        for event in calendar.events
        if event.kind == "school_holiday"
        and event.profile in school
        and event.start.year == day.year
        and issue_time
        and event.known_at
        and event.known_at > issue_time
    ]
    if unavailable and all(
        event.known_at and event.known_at > issue_time
        for event in calendar.events
        if event.kind == "school_holiday"
        and event.start.year == day.year
        and event.profile in school
    ):
        result["coverage"] = "school_schedule_not_known_at_issue"
        return result
    values = list(school.values())
    result["is_school_holiday"] = (
        (any(values) if calendar.school_combination == "any" else all(values)) if values else None
    )
    result["is_school_day"] = (
        not result["is_weekend"]
        and not result["is_public_holiday"]
        and not result["is_school_holiday"]
    )
    result["coverage"] = "configured"
    return result


def import_events(content, format, source, version, kind, zone="Australia/Adelaide"):
    events = []
    if format == "csv":
        for row in csv.DictReader(StringIO(content)):
            events.append(
                CalendarEvent(
                    start=row["start"],
                    end=row["end"],
                    label=row["label"],
                    kind=kind,
                    source=source,
                    version=version,
                    start_time=row.get("start_time", "00:00"),
                    end_time=row.get("end_time", "00:00"),
                )
            )
    elif format == "ics":
        for component in Calendar.from_ical(content).walk("VEVENT"):
            start = component.decoded("dtstart")
            end = component.decoded("dtend") if "dtend" in component else start + timedelta(days=1)

            def local_date(value):
                if isinstance(value, datetime):
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=ZoneInfo(zone))
                    return value.astimezone(ZoneInfo(zone)).date()
                return value

            first, last = local_date(start), local_date(end)
            start_time = (
                start.astimezone(ZoneInfo(zone)).time()
                if isinstance(start, datetime) and start.tzinfo
                else start.time()
                if isinstance(start, datetime)
                else datetime.min.time()
            )
            end_time = (
                end.astimezone(ZoneInfo(zone)).time()
                if isinstance(end, datetime) and end.tzinfo
                else end.time()
                if isinstance(end, datetime)
                else datetime.min.time()
            )
            events.append(
                CalendarEvent(
                    start=first,
                    end=last,
                    label=str(component.get("summary", "Imported event")),
                    kind=kind,
                    source=source,
                    version=version,
                    start_time=start_time,
                    end_time=end_time,
                )
            )
    elif format == "json":
        import json

        events = [
            CalendarEvent.model_validate({**v, "source": source, "version": version, "kind": kind})
            for v in json.loads(content)
        ]
    else:
        raise ValueError("Use json, csv or ics")
    from .store import now

    events = [v if v.known_at else v.model_copy(update={"known_at": now()}) for v in events]
    return [v.model_dump(mode="json") for v in events]
