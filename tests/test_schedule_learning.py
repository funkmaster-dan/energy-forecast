from datetime import datetime, timezone

from energy_forecast.schedule_learning import future_features
from energy_forecast.schemas import Configuration
from energy_forecast.school_calendar import events


def test_future_calendar_knowledge_is_masked_out_of_historical_features():
    config = Configuration(
        latitude=-35,
        longitude=139,
        calendars={"enabled": True, "covered_years": [2025, 2026], "events": events()},
        tariff=[{"name": "default", "import_rate": 0.3}],
    )
    before = datetime(2025, 10, 1, tzinfo=timezone.utc)
    frame = future_features(config, [before], before)
    assert frame.shape == (1, 11)
    assert frame[0, 7] == 0  # No school-holiday assertion before the source was known.
    assert frame[0, 10] == 0  # Missing school calendar knowledge is explicit.
    issue = datetime(2026, 10, 1, tzinfo=timezone.utc)
    frame = future_features(config, [issue], issue)
    assert frame[0, 7] == 1
    assert frame[0, 10] == 1
