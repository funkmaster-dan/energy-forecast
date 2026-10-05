from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from energy_forecast.weather_features import features, for_issue, source_epoch
from energy_forecast.weather_learning import partitions


def vintage(
    config, issue, identifier, received, temperature=20, kind="live_forecast", available=None
):
    return {
        "id": identifier,
        "received_at": received.isoformat(),
        "provider_available_at": available.isoformat() if available else None,
        "kind": kind,
        "source_epoch": source_epoch(config),
        "intervals": [
            {
                "start": (issue + timedelta(hours=i)).isoformat(),
                "end": (issue + timedelta(hours=i + 1)).isoformat(),
                "temperature_2m": temperature,
                "shortwave_radiation": 100,
            }
            for i in range(49)
        ],
    }


def test_weather_join_excludes_future_revisions_and_reanalysis(store, config):
    issue = datetime(2026, 1, 1, tzinfo=timezone.utc)
    earlier = vintage(config, issue, "earlier", issue - timedelta(minutes=10), temperature=15)
    future = vintage(config, issue, "future", issue + timedelta(minutes=10), temperature=40)
    reanalysis = vintage(
        config,
        issue,
        "actual-weather",
        issue - timedelta(minutes=1),
        temperature=45,
        kind="reanalysis_pretraining_only",
    )
    for document in [earlier, future, reanalysis]:
        store.insert_document("weather", document["id"], document, "synthetic")
    selected, rows = for_issue(store, config, issue)
    assert selected["id"] == "earlier"
    assert rows[0]["temperature_2m"] == 15


def test_archive_initialization_does_not_prove_availability(store, config):
    issue = datetime(2026, 1, 1, tzinfo=timezone.utc)
    unknown = vintage(
        config, issue, "unknown", issue + timedelta(days=90), kind="single_run_archive"
    )
    unknown["provider_run_at"] = (issue - timedelta(hours=6)).isoformat()
    store.insert_document("weather", "unknown", unknown, "synthetic")
    assert for_issue(store, config, issue) is None
    confirmed = vintage(
        config,
        issue,
        "confirmed",
        issue + timedelta(days=90),
        kind="single_run_archive",
        available=issue - timedelta(hours=2),
    )
    store.insert_document("weather", "confirmed", confirmed, "synthetic")
    assert for_issue(store, config, issue)[0]["id"] == "confirmed"


def test_missing_or_foreign_weather_does_not_become_zero(store, config):
    issue = datetime(2026, 1, 1, tzinfo=timezone.utc)
    missing = vintage(config, issue, "missing", issue - timedelta(minutes=1))
    missing["intervals"][10]["temperature_2m"] = None
    store.insert_document("weather", "missing", missing, "synthetic")
    assert for_issue(store, config, issue) is None
    foreign = vintage(config, issue, "foreign", issue - timedelta(minutes=1))
    foreign["source_epoch"] = "another-location"
    store.insert_document("weather", "foreign", foreign, "synthetic")
    assert for_issue(store, config, issue) is None
    assert features([{"temperature_2m": None, "shortwave_radiation": 0}]) is None
    assert features([{"temperature_2m": 20, "shortwave_radiation": float("nan")}]) is None


def test_weather_partitions_keep_target_periods_disjoint():
    first = datetime(2025, 1, 1, tzinfo=timezone.utc)
    samples = [{"issue": first + timedelta(days=i)} for i in range(180)]
    selected, ranges, reason = partitions(samples)
    assert reason is None
    for sample in selected:
        start, end = map(datetime.fromisoformat, ranges[sample["group"]])
        assert start <= sample["issue"] and sample["issue"] + timedelta(hours=49) <= end
    for before, after in [("train", "calibration"), ("calibration", "test")]:
        assert datetime.fromisoformat(ranges[after][0]) - datetime.fromisoformat(
            ranges[before][1]
        ) >= timedelta(hours=49)


def test_weather_network_infers_ordered_direct_quantiles():
    torch = pytest.importorskip("torch")
    from energy_forecast.learning import network

    model = network(8)
    with torch.no_grad():
        prediction = model(torch.ones(1, 168), torch.ones(1, 49, 8))
    assert prediction.shape == (1, 49, 3)
    assert torch.all(prediction >= 0)
    assert torch.all(prediction[:, :, 0] <= prediction[:, :, 1])
    assert torch.all(prediction[:, :, 1] <= prediction[:, :, 2])
    scaled = features([{"temperature_2m": 35, "shortwave_radiation": 1000}])
    assert np.allclose(scaled, [[1, 1]])


def test_changed_weather_source_cannot_activate_a_candidate(store, config):
    from energy_forecast.learning import activate_model
    from energy_forecast.store import digest

    store.save_configuration(config)
    config = config.model_copy(update={"revision": 1})
    store.insert_document(
        "models",
        "weather-model",
        {
            "id": "weather-model",
            "promotion_eligible": True,
            "source_signature": digest([m.model_dump(mode="json") for m in config.mappings]),
            "timezone": config.timezone,
            "weather_epoch": "different-provider-or-location",
        },
    )
    with pytest.raises(ValueError, match="Weather source changed"):
        activate_model(store, "weather-model")
    assert store.meta("active_model") is None


def test_weather_model_falls_back_when_live_weather_is_missing(store, config):
    from energy_forecast.learning import predict_load
    from energy_forecast.store import digest

    store.meta("active_model", "weather-model")
    store.insert_document(
        "models",
        "weather-model",
        {
            "id": "weather-model",
            "future_dimensions": 8,
            "source_signature": digest([m.model_dump(mode="json") for m in config.mappings]),
            "weather_epoch": source_epoch(config),
            "timezone": config.timezone,
        },
    )
    issue = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    times = [issue + timedelta(hours=i) for i in range(49)]
    baseline = [1.0] * 49
    assert predict_load(store, config, times, baseline, issue) == (baseline, None, None, None)
