from datetime import timedelta

import pytest
from conftest import ingest, observation
from pydantic import ValidationError

from energy_forecast.composition import compose
from energy_forecast.schemas import Batch, Mapping
from energy_forecast.store import Conflict, Store


def test_idempotence_revision_and_restart(store):
    original = observation()
    assert ingest(store, original)["accepted"] == 1
    assert ingest(store, original)["duplicate"]
    with pytest.raises(Conflict):
        ingest(store, original.model_copy(update={"value": 1200}))
    with pytest.raises(Conflict):
        ingest(store, original.model_copy(update={"value": 1200}), batch_id="other")
    ingest(store, original.model_copy(update={"value": 1200, "revision": 1}), batch_id="revision")
    assert Store(store.root).observations()[0]["value"] == 1200
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


def test_atomic_batch_rejects_conflicting_revision(store):
    original = observation()
    ingest(store, original)
    with pytest.raises(Conflict):
        ingest(
            store,
            original.model_copy(update={"source": "sensor.other"}),
            original.model_copy(update={"value": 2}),
            batch_id="atomic",
        )
    assert len(store.observations()) == 1


def test_watts_and_stitch_not_sum(store):
    ingest(store, observation(1000, "sensor.old"), observation(2000, "sensor.new"))
    mapping = Mapping(
        feature="household_load",
        sources=[{"source": "sensor.old", "priority": 0}, {"source": "sensor.new", "priority": 1}],
    )
    rows = compose(store.observations(), mapping)
    assert rows[0]["energy_kwh"] == 2
    assert rows[0]["sources"] == ["sensor.new"]
    assert "source_disagreement" in rows[0]["reasons"]


def test_counter_resets_and_epochs(store):
    base = observation(100).model_copy(update={"unit": "kWh", "kind": "counter"})
    records = [base]
    for i, value in enumerate((102, 1, 3), 1):
        records.append(
            base.model_copy(
                update={
                    "start": base.start + timedelta(hours=i),
                    "end": base.end + timedelta(hours=i),
                    "value": value,
                }
            )
        )
    records.append(
        base.model_copy(
            update={
                "epoch": "replacement",
                "start": base.start + timedelta(hours=4),
                "end": base.end + timedelta(hours=4),
                "value": 200,
            }
        )
    )
    ingest(store, *records)
    rows = compose(
        store.observations(), Mapping(feature="household_load", sources=[{"source": "sensor.load"}])
    )
    assert [r["energy_kwh"] for r in rows] == [None, 2, None, 2, None]


def test_missing_phase_is_not_zero(store):
    ingest(store, observation())
    mapping = Mapping(
        feature="household_load",
        mode="sum",
        sources=[
            {"source": "sensor.load", "component": "phase1"},
            {"source": "sensor.phase2", "component": "phase2"},
        ],
    )
    assert compose(store.observations(), mapping)[0]["energy_kwh"] is None


def test_high_legitimate_demand_retained_and_dc_conversion(store):
    ingest(store, observation(18000).model_copy(update={"boundary": "DC"}))
    rows = compose(
        store.observations(),
        Mapping(
            feature="household_load",
            maximum_kw=20,
            sources=[{"source": "sensor.load", "dc_to_ac_efficiency": 0.9}],
        ),
    )
    assert rows[0]["quality"] == "valid"
    assert rows[0]["energy_kwh"] == pytest.approx(16.2)


def test_derived_balance_requires_all_sources(store):
    ingest(store, observation(5000, "sensor.pv"), observation(2000, "sensor.export"))
    mapping = Mapping(
        feature="household_load",
        mode="derived",
        sources=[
            {"source": "sensor.pv", "coefficient": 1},
            {"source": "sensor.export", "coefficient": -1},
        ],
    )
    assert compose(store.observations(), mapping)[0]["energy_kwh"] == 3


def test_no_naive_time_nan_or_output_feedback():
    with pytest.raises(ValidationError):
        Batch.model_validate(
            {
                "batch_id": "x",
                "observations": [{**observation().model_dump(mode="json"), "value": float("nan")}],
            }
        )
    with pytest.raises(ValidationError):
        observation(source="sensor.energy_forecast_pv")


def test_counter_unit_change_requires_an_epoch(store):
    base = observation(100).model_copy(update={"unit": "kWh", "kind": "counter"})
    changed = base.model_copy(
        update={
            "start": base.end,
            "end": base.end + timedelta(hours=1),
            "unit": "Wh",
            "value": 1000,
        }
    )
    ingest(store, base, changed)
    rows = compose(
        store.observations(), Mapping(feature="household_load", sources=[{"source": "sensor.load"}])
    )
    assert rows[-1]["energy_kwh"] is None


def test_state_percentage_cannot_be_a_load_target(store):
    record = observation(1).model_copy(update={"unit": "%", "kind": "state", "boundary": "stored"})
    ingest(store, record)
    row = compose(
        store.observations(), Mapping(feature="household_load", sources=[{"source": "sensor.load"}])
    )[0]
    assert row["energy_kwh"] is None


def test_align_disjoint_five_minute_phase_with_hourly_phase(store):
    base = observation(1000, "sensor.phase1")
    records = [base]
    for i in range(12):
        records.append(
            base.model_copy(
                update={
                    "source": "sensor.phase2",
                    "start": base.start + timedelta(minutes=i * 5),
                    "end": base.start + timedelta(minutes=(i + 1) * 5),
                    "value": 2000,
                }
            )
        )
    ingest(store, *records)
    mapping = Mapping(
        feature="household_load",
        mode="sum",
        sources=[
            {"source": "sensor.phase1", "component": "p1"},
            {"source": "sensor.phase2", "component": "p2"},
        ],
    )
    rows = compose(store.observations(), mapping)
    assert len(rows) == 1 and rows[0]["energy_kwh"] == pytest.approx(3)
    assert len(rows[0]["observation_ids"]) == 13


def test_hourly_statistic_does_not_double_count_overlapping_raw_power(store):
    base = observation(1000)
    records = [
        base,
        base.model_copy(
            update={
                "start": base.start + timedelta(minutes=5),
                "end": base.start + timedelta(minutes=10),
                "value": 5000,
            }
        ),
    ]
    ingest(store, *records)
    row = compose(
        store.observations(), Mapping(feature="household_load", sources=[{"source": "sensor.load"}])
    )[0]
    assert row["energy_kwh"] == 1


def test_hourly_energy_cannot_be_ground_truth_for_fifteen_minute_peaks(store):
    ingest(store, observation())
    rows = compose(
        store.observations(),
        Mapping(
            feature="household_load", resolution_minutes=15, sources=[{"source": "sensor.load"}]
        ),
    )
    assert all(row["energy_kwh"] is None for row in rows)
