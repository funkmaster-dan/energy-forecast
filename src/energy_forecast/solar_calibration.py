"""Per-bank output learned from measured generation and historical irradiance."""

import uuid
from datetime import timedelta

import numpy as np

from .composition import dt
from .forecast import canonical
from .store import digest, now, stamp
from .weather import fetch, physical_pv


def geometry_signature(config, bank):
    return digest(
        {
            "latitude": config.latitude,
            "longitude": config.longitude,
            "tilt": bank.tilt,
            "azimuth": bank.azimuth,
            "target": bank.target,
            "mapping": next(
                (m.model_dump(mode="json") for m in config.mappings if m.feature == bank.target),
                None,
            ),
        }
    )


def calibrate_banks(store, config):
    cutoff = now()
    previous = store.meta("pv_bank_calibration") or {}
    if not previous.get("scales"):
        previous = store.meta("previous_pv_bank_calibration") or previous
    results = {}
    scales = {}
    for bank in config.banks:
        if not bank.target:
            results[bank.id] = {"status": "sensor_required", "reason": "Select a generation sensor"}
            continue
        targets = {
            dt(row["start"]): row["energy_kwh"]
            for row in canonical(store, config, bank.target, training_cutoff=cutoff, measured=True)
            if row["energy_kwh"] is not None
            and row["quality"] in ("valid", "suspect")
            and dt(row["end"]) - dt(row["start"]) == timedelta(hours=1)
        }
        if len(targets) < 24 * 14:
            results[bank.id] = {
                "status": "collecting_history",
                "hours": len(targets),
                "reason": "Need at least 14 days of covered generation history",
            }
            continue
        weather = {}
        for vintage in store.documents("weather", 10000):
            raw = vintage.get("raw", {})
            if (
                vintage["kind"] == "reanalysis_pretraining_only"
                and abs(raw.get("latitude", config.latitude) - config.latitude) < 0.5
                and abs(raw.get("longitude", config.longitude) - config.longitude) < 0.5
            ):
                weather.update({dt(row["start"]): row for row in vintage["intervals"]})
        start, end = min(targets).date(), max(targets).date()
        cursor = start
        while cursor <= end:
            stop = min(end, cursor + timedelta(days=30))
            if not all(t in weather for t in targets if cursor <= t.date() <= stop):
                vintage = fetch(config, store, (cursor, stop))
                weather.update({dt(row["start"]): row for row in vintage["intervals"]})
            cursor = stop + timedelta(days=1)
        times = sorted(set(targets) & set(weather))
        if not times:
            results[bank.id] = {"status": "weather_missing"}
            continue
        reference = bank.model_copy(update={"dc_kwp": 1, "conversion_efficiency": 1})
        reference_config = config.model_copy(
            update={"banks": [reference], "inverter_limits_kw": {}}
        )
        physical, _, reference_poa = physical_pv(reference_config, [weather[t] for t in times])
        weather_rows = [weather[t] for t in times]
        actual = np.array([targets[t] for t in times])
        usable = np.isfinite(physical) & (physical > 0.05) & np.isfinite(actual) & (actual >= 0)
        times = [t for i, t in enumerate(times) if usable[i]]
        weather_rows = [row for i, row in enumerate(weather_rows) if usable[i]]
        irradiance = reference_poa[bank.id][usable]
        physical, actual = physical[usable], actual[usable]
        if len(times) < 120:
            results[bank.id] = {"status": "insufficient_daylight", "hours": len(times)}
            continue
        first, last = times[0], times[-1] + timedelta(hours=1)
        length = (last - first).total_seconds()
        train_end = first + timedelta(seconds=length * 0.6)
        calibration_end = first + timedelta(seconds=length * 0.8)
        train = np.array([t < train_end for t in times])
        cal = np.array([train_end + timedelta(hours=48) <= t < calibration_end for t in times])
        test = np.array([t >= calibration_end + timedelta(hours=48) for t in times])
        if min(train.sum(), cal.sum(), test.sum()) < 12:
            results[bank.id] = {"status": "holdout_incomplete", "daylight_hours": len(times)}
            continue
        # Median ratios retain genuine clipping/high output in the journal while resisting faults.
        scale = float(np.median(actual[train] / physical[train]))
        if not np.isfinite(scale) or scale <= 0:
            results[bank.id] = {"status": "invalid_scale"}
            continue
        predicted = physical * scale
        geometry_mae = float(np.mean(np.abs(actual[test] - predicted[test])))
        model_id = None
        if int(train.sum()) >= 120:
            from .bank_models import fit_candidate, inputs

            model, candidate = fit_candidate(
                inputs(weather_rows, predicted, irradiance), actual, train, cal
            )
            candidate_mae = float(np.mean(np.abs(actual[test] - candidate[test])))
            if candidate_mae < geometry_mae * 0.95 and abs(
                float(np.mean(candidate[test] - actual[test]))
            ) <= max(0.05, abs(float(np.mean(predicted[test] - actual[test])))):
                model_id = uuid.uuid4().hex
                directory = store.root / "models"
                directory.mkdir(exist_ok=True)
                model.booster_.save_model(str(directory / f"pv-bank-{model_id}.txt"))
                predicted = candidate
        residual = actual[cal] - predicted[cal]
        lower, upper = np.quantile(residual, [0.05, 0.95])
        coverage = float(
            np.mean(
                (actual[test] >= np.maximum(0, predicted[test] + lower))
                & (actual[test] <= predicted[test] + upper)
            )
        )
        baseline = np.array(
            [
                np.median(actual[train & np.array([x.hour == t.hour for x in times])])
                if any(train & np.array([x.hour == t.hour for x in times]))
                else np.median(actual[train])
                for t in times
            ]
        )
        baseline_mae = float(np.mean(np.abs(actual[test] - baseline[test])))
        test_mae = float(np.mean(np.abs(actual[test] - predicted[test])))
        test_bias = float(np.mean(predicted[test] - actual[test]))
        test_scale = max(float(np.mean(actual[test])), 0.1)
        accepted = test_mae <= baseline_mae * 1.1 + 1e-6 and abs(test_bias) / test_scale <= 0.3
        result = {
            "status": "calibrated" if accepted else "rejected_holdout",
            "scale": scale,
            "model_id": model_id,
            "model_kind": "bank-lightgbm" if model_id else "geometry-scaled",
            "geometry_test_mae_kw": geometry_mae,
            "geometry_signature": geometry_signature(config, bank),
            "measurement_boundary": bank.measurement_boundary,
            "training_range": [first.isoformat(), train_end.isoformat()],
            "calibration_range": [
                (train_end + timedelta(hours=48)).isoformat(),
                calibration_end.isoformat(),
            ],
            "test_range": [(calibration_end + timedelta(hours=48)).isoformat(), last.isoformat()],
            "test_mae_kw": test_mae,
            "baseline_test_mae_kw": baseline_mae,
            "holdout_accepted": accepted,
            "split_counts": {
                "train": int(train.sum()),
                "calibration": int(cal.sum()),
                "test": int(test.sum()),
            },
            "test_bias_kw": test_bias,
            "test_interval_coverage": coverage,
            "daylight_hours": len(times),
            "weather_basis": "historical_reanalysis",
            "operational_confidence_validated": False,
            "updated_at": stamp(),
        }
        results[bank.id] = result
        if accepted:
            scales[bank.id] = scale
    for bank in config.banks:
        if (
            bank.id not in scales
            and previous.get("banks", {}).get(bank.id, {}).get("geometry_signature")
            == geometry_signature(config, bank)
            and bank.id in previous.get("scales", {})
        ):
            candidate = results.get(bank.id)
            results[bank.id] = {
                **previous["banks"][bank.id],
                "retained_previous": True,
                "candidate": candidate,
            }
            scales[bank.id] = previous["scales"][bank.id]
    if previous.get("scales"):
        store.meta("previous_pv_bank_calibration", previous)
    report = {"banks": results, "scales": scales, "updated_at": stamp()}
    store.meta("pv_bank_calibration", report)
    store.meta("invalidated_at", stamp())
    return report
