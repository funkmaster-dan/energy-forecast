from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from energy_forecast.api import create_app
from energy_forecast.forecast import leased
from energy_forecast.learning import paired_scenarios, partition_samples, zero_failure_upper_bound
from energy_forecast.store import digest, stamp


def client_with_admin(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    client = TestClient(app)
    key = (tmp_path / "setup-key").read_text()
    assert (
        client.post(
            "/auth/setup", json={"password": "synthetic-test-password", "setup_key": key}
        ).status_code
        == 200
    )
    csrf = client.post("/auth/login", json={"password": "synthetic-test-password"}).json()["csrf"]
    client.headers["x-csrf-token"] = csrf
    return client, app.state.store


def test_auth_csrf_scopes_and_version_conflicts(tmp_path, config):
    client, store = client_with_admin(tmp_path)
    assert client.get("/health/ready").status_code == 200
    path = "/v1/sites/home/configuration"
    assert (
        client.put(
            path, json=config.model_dump(mode="json"), headers={"x-csrf-token": "bad"}
        ).status_code
        == 403
    )
    assert client.put(path, json=config.model_dump(mode="json")).status_code == 200
    assert client.put(path, json=config.model_dump(mode="json")).status_code == 409
    token = client.post("/v1/sites/home/tokens", json={"scope": "integration"}).json()["token"]
    client.cookies.clear()
    client.headers["Authorization"] = "Bearer " + token
    assert client.get("/v1/sites/home/forecast/latest").status_code == 200
    assert client.get(path).status_code == 403
    assert client.get("/v1/sites/other/forecast/latest").status_code == 404
    assert (
        client.post(
            "/auth/login",
            json={"password": "synthetic-test-password"},
            headers={"origin": "https://evil.test"},
        ).status_code
        == 403
    )
    assert store.meta("admin")


def test_setup_claim_key_and_backup_restore(tmp_path, config):
    client, store = client_with_admin(tmp_path)
    client.put("/v1/sites/home/configuration", json=config.model_dump(mode="json"))
    assert not (tmp_path / "setup-key").exists()
    identifier = client.post("/v1/sites/home/backup").json()["id"]
    import tarfile

    from energy_forecast.store import Store

    restored = tmp_path / "restored"
    with tarfile.open(tmp_path / "backups" / f"{identifier}.tar.gz") as archive:
        archive.extractall(restored, filter="data")
    assert Store(restored).configuration() == store.configuration()


def test_lease_expiry_and_configuration_invalidation(store, config):
    store.save_configuration(config)
    payload = {
        "forecast_id": "x",
        "status": "ready",
        "issued_at": stamp(),
        "valid_until": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
        "configuration_hash": digest(store.configuration()),
        "export_plan": {
            "safe_battery_export_remaining_kwh": 5,
            "safe_battery_export_now_kwh": 1,
            "advisory_export_power_kw": 2,
            "reason_codes": [],
        },
    }
    assert leased(store, payload)["status"] == "ready"
    later = datetime.now(timezone.utc) + timedelta(minutes=3)
    assert leased(store, payload, later)["export_plan"]["safe_battery_export_remaining_kwh"] == 0
    store.meta("invalidated_at", stamp())
    assert leased(store, payload)["status"] == "stale"
    assert payload["export_plan"]["safe_battery_export_remaining_kwh"] == 5


def test_chronological_partitions_with_horizon_embargo():
    first = datetime(2025, 1, 1, tzinfo=timezone.utc)
    targets = {first + timedelta(hours=i): 1.0 for i in range(24 * 150)}
    samples, ranges, reason = partition_samples(targets, "Australia/Adelaide")
    assert reason is None
    for sample in samples:
        a, b = map(datetime.fromisoformat, ranges[sample["group"]])
        assert a <= sample["issue"] and sample["issue"] + timedelta(hours=49) <= b
    assert datetime.fromisoformat(ranges["calibration"][0]) - datetime.fromisoformat(
        ranges["train"][1]
    ) >= timedelta(hours=49)
    targets.pop(first + timedelta(hours=200))
    changed, _, _ = partition_samples(targets, "Australia/Adelaide")
    assert len(changed) < len(samples)


def test_paired_blocks_preserve_dependence_and_tail_sample_count():
    blocks = np.array([[[1, -1], [1, -1]], [[-1, 1], [-1, 1]]])
    pv, load = paired_scenarios([2, 2], [2, 2], blocks, 100)
    assert np.all(pv[:, 0] == pv[:, 1])
    assert np.all(pv + load == 4)
    assert zero_failure_upper_bound(299) < 0.01
    assert zero_failure_upper_bound(20) > 0.01
    with pytest.raises(ValueError):
        paired_scenarios([1, 1], [1, 1], [[1, 2]])


def test_cnn_quantiles_are_ordered_nonnegative_and_direct():
    torch = pytest.importorskip("torch")
    from energy_forecast.learning import network

    model = network()
    with torch.no_grad():
        output = model(torch.ones(2, 168), torch.zeros(2, 49, 6))
    assert output.shape == (2, 49, 3)
    assert torch.all(output >= 0)
    assert torch.all(output[:, :, 0] <= output[:, :, 1])
    assert torch.all(output[:, :, 1] <= output[:, :, 2])


def test_passive_calibration_rejects_confounding_and_simultaneous_flows():
    from energy_forecast.passive import estimate_efficiency

    assert estimate_efficiency([], False)["status"] == "underdetermined"
    charge = {
        "quality": "valid",
        "coverage": 1,
        "stored_start_kwh": 2,
        "stored_end_kwh": 2.9,
        "charge_ac_kwh": 1,
        "discharge_ac_kwh": 0,
    }
    discharge = {
        "quality": "valid",
        "coverage": 1,
        "stored_start_kwh": 3,
        "stored_end_kwh": 2,
        "charge_ac_kwh": 0,
        "discharge_ac_kwh": 0.8,
    }
    report = estimate_efficiency(
        [charge] * 25 + [discharge] * 25 + [dict(charge, discharge_ac_kwh=1)], True
    )
    assert report["status"] == "candidate"
    assert report["directions"]["charge"]["estimate"] == pytest.approx(0.9)
    assert report["directions"]["discharge"]["estimate"] == pytest.approx(0.8)
    assert len(report["rejected"]) == 1
    assert report["auto_applied"] is False


def test_archived_overlay_only_shows_completed_actuals(tmp_path, config):
    from conftest import ingest, observation

    client, store = client_with_admin(tmp_path)
    client.put("/v1/sites/home/configuration", json=config.model_dump(mode="json"))
    past = observation()
    ingest(store, past)
    future = past.model_copy(
        update={
            "start": datetime.now(timezone.utc) + timedelta(days=1),
            "end": datetime.now(timezone.utc) + timedelta(days=1, hours=1),
        }
    )
    ingest(store, future, batch_id="future-observation")
    value = {
        "schema_version": "1.0",
        "forecast_id": "replay",
        "status": "shadow",
        "issued_at": stamp(),
        "valid_until": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
        "configuration_hash": digest(store.configuration()),
        "configuration_revision": 1,
        "series": [
            {"start": r.start.isoformat(), "end": r.end.isoformat(), "load_kw": 1, "pv_kw": 0}
            for r in (past, future)
        ],
        "export_plan": {"safe_battery_export_remaining_kwh": 0, "reason_codes": []},
    }
    store.insert_document("forecasts", "replay", value, value["configuration_hash"])
    response = client.get("/v1/sites/home/forecasts/replay")
    assert response.status_code == 200, response.text
    assert response.json()["observed_series"][0]["load_kw"] == 1
    assert response.json()["observed_series"][1]["load_kw"] is None


def test_invalid_timezone_is_a_validation_error(tmp_path, config):
    client, _ = client_with_admin(tmp_path)
    payload = config.model_dump(mode="json")
    payload["timezone"] = "Australia/NotAPlace"
    assert client.put("/v1/sites/home/configuration", json=payload).status_code == 422


def test_recent_soc_statistics_cannot_freshen_a_live_state(store, config):
    from conftest import ingest, observation

    from energy_forecast.forecast import state
    from energy_forecast.schemas import Mapping

    config.mappings.append(Mapping(feature="battery_soc", sources=[{"source": "sensor.soc"}]))
    issue = datetime.now(timezone.utc)
    live = observation(80, "sensor.soc").model_copy(
        update={
            "feature": "battery_soc",
            "kind": "state",
            "unit": "%",
            "boundary": "stored",
            "start": issue - timedelta(minutes=15, seconds=1),
            "end": issue - timedelta(minutes=15),
            "provenance": "live",
        }
    )
    statistics = live.model_copy(
        update={
            "start": issue - timedelta(hours=1),
            "end": issue - timedelta(seconds=10),
            "value": 90,
            "provenance": "statistics",
        }
    )
    ingest(store, live, statistics)
    result = state(store, config, "battery_soc", issue)
    assert result["value"] == 80
    assert (
        issue - datetime.fromisoformat(result["end"])
    ).total_seconds() > config.soc_freshness_seconds


def test_recent_adapter_updates_existing_correction_instead_of_replacing_it(
    store, config, monkeypatch
):
    from conftest import ingest, observation

    from energy_forecast.learning import adapt_recent

    store.save_configuration(config)
    store.meta("active_model", "test-model")
    store.meta("recent_bias", {"model_id": "test-model", "load_kw": 0.1, "profile": {}})
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    records = []
    snapshots = []
    for i in range(9 * 24):
        start = end - timedelta(hours=9 * 24 - i)
        record = observation(1200).model_copy(
            update={"start": start, "end": start + timedelta(hours=1)}
        )
        records.append(record)
        snapshots.append(
            {
                "model_versions": {"load": "test-model"},
                "series": [
                    {"start": start.isoformat(), "end": record.end.isoformat(), "load_kw": 1.1}
                ],
            }
        )
    ingest(store, *records)
    monkeypatch.setattr(store, "origins", lambda *args, **kwargs: iter(snapshots))
    result = adapt_recent(store, config)
    assert result["status"] == "shadow"
    assert result["load_kw"] > 0.1
    assert result["validation"]["new_mae_kw"] < result["validation"]["old_mae_kw"]


def test_backfilled_weather_origins_use_run_availability_not_import_time(store):
    for identifier, day in [("first", "2026-01-01"), ("second", "2026-01-02")]:
        store.insert_document(
            "weather",
            identifier,
            {"id": identifier, "provider_available_at": day + "T05:00:00+00:00"},
            "synthetic",
        )
    assert [r["id"] for r in store.origins("weather", step_hours=12)] == ["first", "second"]
