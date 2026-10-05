from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from energy_forecast.schemas import Bank, Batch, Configuration, Observation
from energy_forecast.solar_calibration import calibrate_banks, geometry_signature
from energy_forecast.weather import physical_pv


def test_bank_geometry_needs_no_capacity_or_inverter_limit():
    config = Configuration(
        latitude=-35,
        longitude=139,
        banks=[{"id": "north", "tilt": 25, "azimuth": 0}],
        tariff=[{"name": "default", "import_rate": 0.3}],
    )
    assert config.banks[0].dc_kwp is None
    assert config.inverter_limits_kw == {}
    rows = [
        {
            "start": "2026-09-01T01:00:00+00:00",
            "end": "2026-09-01T02:00:00+00:00",
            "temperature_2m": 20,
            "shortwave_radiation": 700,
            "direct_normal_irradiance": 600,
            "diffuse_radiation": 150,
        }
    ]
    power, _, poa = physical_pv(config, rows)
    assert np.isnan(power[0]) and poa["north"][0] > 0


def test_per_bank_scale_is_learned_from_measured_generation(store, monkeypatch):
    config = Configuration(
        latitude=-35,
        longitude=139,
        banks=[{"id": "north", "tilt": 25, "azimuth": 0, "target": "pv_bank_north"}],
        mappings=[{"feature": "pv_bank_north", "sources": [{"source": "sensor.bank"}]}],
        tariff=[{"name": "default", "import_rate": 0.3}],
    )
    first = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(40 * 24):
        start = first + timedelta(hours=i)
        local_hour = (start.hour + 9.5) % 24
        ghi = max(0, np.sin((local_hour - 6) / 12 * np.pi)) * 700 if 6 <= local_hour <= 18 else 0
        rows.append(
            {
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
                "temperature_2m": 20,
                "shortwave_radiation": float(ghi),
                "direct_normal_irradiance": float(ghi * 0.8),
                "diffuse_radiation": float(ghi * 0.2),
            }
        )
    reference = config.model_copy(
        update={"banks": [Bank(id="north", tilt=25, azimuth=0, dc_kwp=1, conversion_efficiency=1)]}
    )
    power, _, _ = physical_pv(reference, rows)
    observations = [
        Observation(
            feature="pv_bank_north",
            source="sensor.bank",
            epoch="synthetic",
            start=row["start"],
            end=row["end"],
            value=float(value) * 3.5,
            unit="kW",
            kind="mean_power",
            boundary="DC",
        )
        for row, value in zip(rows, power)
    ]
    store.ingest(Batch(batch_id="synthetic-bank", observations=observations))
    store.insert_document(
        "weather",
        "history",
        {
            "id": "history",
            "kind": "reanalysis_pretraining_only",
            "intervals": rows,
            "raw": {"latitude": -35, "longitude": 139},
        },
        "synthetic",
    )

    def no_fetch(*args, **kwargs):
        raise AssertionError("Historical weather was already cached")

    monkeypatch.setattr("energy_forecast.solar_calibration.fetch", no_fetch)
    result = calibrate_banks(store, config)
    assert result["banks"]["north"]["status"] == "calibrated"
    assert result["scales"]["north"] == pytest.approx(3.5)
    predicted, _, _ = physical_pv(config, rows, learned_scales=result["scales"])
    assert predicted == pytest.approx(power * 3.5)
    assert result["banks"]["north"]["operational_confidence_validated"] is False
    assert result["banks"]["north"]["test_mae_kw"] < 1e-6
    changed = config.banks[0].model_copy(update={"tilt": 40})
    assert geometry_signature(config, changed) != result["banks"]["north"]["geometry_signature"]
