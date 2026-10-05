from datetime import datetime, timedelta, timezone

import pytest

from energy_forecast.schemas import Batch, Configuration, Observation
from energy_forecast.store import Store


@pytest.fixture
def config():
    return Configuration(
        latitude=-35,
        longitude=139,
        banks=[{"id": "north", "dc_kwp": 5, "tilt": 25, "azimuth": 0}],
        mappings=[{"feature": "household_load", "sources": [{"source": "sensor.load"}]}],
        terminal_reserve_kwh=2,
        tariff=[{"name": "default", "import_rate": 0.3, "export_rate": 0.1}],
    )


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def observation(value=1000, source="sensor.load", **kwargs):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Observation(
        feature="household_load",
        source=source,
        epoch="original",
        start=start,
        end=start + timedelta(hours=1),
        value=value,
        unit="W",
        kind="mean_power",
        boundary="AC",
        **kwargs,
    )


def ingest(store, *records, batch_id="test"):
    return store.ingest(Batch(batch_id=batch_id, observations=list(records)))
