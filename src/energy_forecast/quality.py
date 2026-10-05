"""Coverage is a union of measured intervals, never a sum of overlapping aliases."""

from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .composition import dt
from .forecast import canonical
from .store import now


def union_hours(intervals):
    if not intervals:
        return 0
    intervals = sorted(intervals)
    first, last = intervals[0]
    seconds = 0
    for start, end in intervals[1:]:
        if start <= last:
            last = max(last, end)
        else:
            seconds += (last - first).total_seconds()
            first, last = start, end
    return (seconds + (last - first).total_seconds()) / 3600


def report(store, config, days=90):
    zone = ZoneInfo(config.timezone)
    end_day = now().astimezone(zone).date()
    start_day = end_day - timedelta(days=days - 1)
    profiles = []
    for mapping in config.mappings:
        if mapping.feature in ("battery_soc", "export_limit", "outdoor_temperature"):
            continue
        rows = canonical(store, config, mapping.feature)
        counts = Counter(reason for row in rows for reason in row["reasons"])
        coverage = []
        for i in range(days):
            day = start_day + timedelta(days=i)
            start = datetime.combine(day, datetime.min.time(), zone).astimezone(now().tzinfo)
            end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone).astimezone(
                now().tzinfo
            )
            measured = [
                (max(start, dt(r["start"])), min(end, dt(r["end"])))
                for r in rows
                if r["energy_kwh"] is not None and dt(r["start"]) < end and dt(r["end"]) > start
            ]
            hours = union_hours(measured)
            duration = (end - start).total_seconds() / 3600
            coverage.append(
                {
                    "date": day.isoformat(),
                    "covered_hours": hours,
                    "day_hours": duration,
                    "fraction": min(1, hours / duration),
                }
            )
        profiles.append(
            {
                "feature": mapping.feature,
                "mode": mapping.mode,
                "sources": [s.source for s in mapping.sources],
                "coverage": coverage,
                "reasons": dict(counts),
                "intervals": len(rows),
                "invalid_intervals": sum(r["energy_kwh"] is None for r in rows),
                "retained_extremes": [
                    r
                    for r in sorted(
                        [r for r in rows if r["energy_kwh"] is not None],
                        key=lambda r: r["energy_kwh"],
                        reverse=True,
                    )[:5]
                ],
            }
        )
    return {
        "timezone": config.timezone,
        "profiles": profiles,
        "quality_rule_version": "physical-v1+review-journal",
        "raw_preserved": True,
    }
