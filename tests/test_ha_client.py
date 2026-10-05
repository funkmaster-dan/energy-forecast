from energy_forecast.ha_client import ingest_records, tariff_from_states


def test_direct_ingestion_keeps_immutable_revisions_and_skips_repeats(store):
    record = {
        "source": "sensor.battery",
        "feature": "battery_soc",
        "epoch": "api-v1",
        "start": "2026-10-05T10:00:00+00:00",
        "end": "2026-10-05T10:00:01+00:00",
        "value": 80,
        "unit": "%",
        "kind": "state",
        "boundary": "stored",
    }
    assert ingest_records(store, [record]) == 1
    assert ingest_records(store, [record]) == 0
    assert ingest_records(store, [{**record, "quality": "invalid", "reasons": ["stale"]}]) == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


def test_tariff_helper_schedules_combine_and_apply_gst_once():
    def sensor(direction, windows, multiplier):
        return {
            "entity_id": "sensor.electricity_" + direction + "_rate",
            "attributes": {
                "unit_of_measurement": "AUD/kWh",
                "gst_multiplier": multiplier,
                "windows": windows,
            },
        }

    result = tariff_from_states(
        [
            sensor(
                "import",
                [
                    {"start": "00:00", "end": "11:00", "rate": 0.2},
                    {"start": "11:00", "end": "14:00", "rate": 0},
                    {"start": "14:00", "end": "00:00", "rate": 0.3},
                ],
                1.1,
            ),
            sensor(
                "export",
                [
                    {"start": "00:00", "end": "18:00", "rate": 0.05},
                    {"start": "18:00", "end": "00:00", "rate": 0.15},
                ],
                1,
            ),
        ]
    )
    rules = result["rules"]
    assert len(rules) == 4
    assert abs(rules[0]["import_rate"] - 0.22) < 1e-9
    assert rules[1]["grid_charge_allowed"] is True
    assert rules[-1]["export_rate"] == 0.15


def test_api_reports_bypass_cached_timestamp_and_counter_intervals_stay_contiguous(
    store, monkeypatch
):
    from datetime import timedelta

    from energy_forecast import ha_client
    from energy_forecast.composition import dt, normalize
    from energy_forecast.schemas import Configuration
    from energy_forecast.store import now

    clock = [now() - timedelta(minutes=10)]
    cached = clock[0].isoformat()
    reading = [10.0]
    config = Configuration(
        latitude=-35,
        longitude=139,
        mappings=[
            {"feature": "household_load", "sources": [{"source": "sensor.house_counter"}]},
            {"feature": "energy_debug", "sources": [{"source": "sensor.house_counter"}]},
        ],
        tariff=[{"name": "default", "import_rate": 0.3}],
    )
    store.save_configuration(config)
    store.meta(
        "ha_bridge",
        {"sources": [{"source": "sensor.house_counter", "unit": "kWh", "kind": "counter"}]},
    )
    store.meta("ha_ingestion", {"catalog_at": cached})

    class Client:
        def get(self, path):
            return [
                {
                    "entity_id": "sensor.house_counter",
                    "state": reading[0],
                    "attributes": {"unit_of_measurement": "kWh"},
                    "last_reported": cached,
                    "last_updated": cached,
                }
            ]

        def reports(self, entities):
            return {"sensor.house_counter": clock[0].isoformat()}

        def commands(self, commands):
            return [{}]

    monkeypatch.setattr(ha_client, "client_for", lambda store: Client())
    monkeypatch.setattr(ha_client, "now", lambda: clock[0])
    poller = ha_client.HAPoller(store)
    poller.poll()
    clock[0] += timedelta(minutes=1)
    reading[0] = 10.02
    poller.poll()
    rows = sorted(store.observations("household_load"), key=lambda r: r["end"])
    assert len(store.observations("energy_debug")) == 2
    assert len(rows) == 2
    assert dt(rows[1]["start"]) == dt(rows[0]["end"])
    result = normalize(rows, config.mappings[0])
    assert result[1]["quality"] == "valid"
    assert abs(result[1]["energy_kwh"] - 0.02) < 1e-9
