from datetime import datetime, timezone

import numpy as np

from energy_forecast.calendars import features, import_events
from energy_forecast.weather import physical_pv


def test_calendar_unknown_year_and_ics_exclusive_end(config):
    config.calendars.enabled = True
    instant = datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert features(config, instant)["is_school_holiday"] is None
    content = "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nDTSTART;VALUE=DATE:20261005\nDTEND;VALUE=DATE:20261007\nSUMMARY:Break\nEND:VEVENT\nEND:VCALENDAR"
    imported = import_events(content, "ics", "official", "v1", "school_holiday")
    assert imported[0]["end"] == "2026-10-07"
    from energy_forecast.schemas import CalendarEvent

    config.calendars.events = [CalendarEvent.model_validate(imported[0])]
    config.calendars.covered_years = [2026]
    assert features(config, instant)["is_school_holiday"]
    assert not features(config, datetime(2026, 10, 7, tzinfo=timezone.utc))["is_school_holiday"]


def test_compass_geometry_shared_clipping_and_missing_weather(config):
    rows = [
        {
            "start": "2026-01-01T01:00:00+00:00",
            "end": "2026-01-01T02:00:00+00:00",
            "shortwave_radiation": 1000,
            "direct_normal_irradiance": 900,
            "diffuse_radiation": 150,
            "temperature_2m": 25,
        }
    ]
    config.inverter_limits_kw["main"] = 1
    total, banks, poa = physical_pv(config, rows)
    assert total[0] <= 1 and banks["north"][0] >= 0 and poa["north"][0] > 0
    rows[0]["shortwave_radiation"] = None
    assert np.isnan(physical_pv(config, rows)[0][0])


def test_partial_public_holidays_and_timed_ics(config):
    from zoneinfo import ZoneInfo

    config.calendars.enabled = True
    config.calendars.covered_years = [2026]
    early = datetime(2026, 12, 24, 18, 0, tzinfo=ZoneInfo("Australia/Adelaide"))
    late = early.replace(hour=20)
    assert not features(config, early)["is_public_holiday"]
    assert features(config, late)["is_public_holiday"]
    content = "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nDTSTART:20261005T020000Z\nDTEND:20261005T040000Z\nSUMMARY:Visitors\nEND:VEVENT\nEND:VCALENDAR"
    event = import_events(content, "ics", "household", "v1", "household")[0]
    assert event["start"] == event["end"] == "2026-10-05"
    assert event["start_time"] == "12:30:00" and event["end_time"] == "14:30:00"
