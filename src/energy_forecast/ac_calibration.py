"""Learn bank-to-AC conversion from simultaneously measured DC banks and an AC meter."""

from datetime import timedelta

import numpy as np

from .composition import dt
from .forecast import canonical
from .store import digest, now, stamp


def signature(config):
    return digest(
        {
            "banks": [(b.id, b.target, b.measurement_boundary) for b in config.banks],
            "mappings": [
                m.model_dump(mode="json")
                for m in config.mappings
                if m.feature in ("pv_generation", "pv_generation_energy")
                or m.feature in {b.target for b in config.banks}
            ],
        }
    )


def calibrate_ac(store, config):
    if not config.banks or any(b.measurement_boundary != "DC" for b in config.banks):
        return {"status": "dc_bank_inputs_required"}
    cutoff = now()

    def targets(feature):
        return {
            dt(r["start"]): r["energy_kwh"]
            for r in canonical(store, config, feature, training_cutoff=cutoff, measured=True)
            if r["energy_kwh"] is not None
            and r["quality"] in ("valid", "suspect")
            and dt(r["end"]) - dt(r["start"]) == timedelta(hours=1)
        }

    ac = targets("pv_generation")
    banks = [targets(b.target) for b in config.banks]
    common = sorted(set(ac).intersection(*[set(b) for b in banks]))
    times = [t for t in common if sum(b[t] for b in banks) > 0.2 and ac[t] >= 0]
    if len(times) < 100:
        result = {"status": "collecting_paired_ac_history", "paired_hours": len(times)}
    else:
        start, end = times[0], times[-1] + timedelta(hours=1)
        span = end - start
        first, second = start + span * 0.6, start + span * 0.8
        train = np.array([t < first for t in times])
        calibration = np.array([first + timedelta(hours=24) <= t < second for t in times])
        test = np.array([t >= second + timedelta(hours=24) for t in times])
        if min(train.sum(), calibration.sum(), test.sum()) < 6:
            result = {"status": "holdout_incomplete", "paired_hours": len(times)}
        else:
            dc = np.array([sum(b[t] for b in banks) for t in times])
            actual = np.array([ac[t] for t in times])
            efficiency = float(np.median(actual[train] / dc[train]))
            prediction = dc * efficiency
            mae = float(np.mean(np.abs(prediction[test] - actual[test])))
            bias = float(np.mean(prediction[test] - actual[test]))
            scale = max(0.1, float(np.mean(actual[test])))
            accepted = (
                0.7 <= efficiency <= 1.0 and mae / scale <= 0.15 and abs(bias) / scale <= 0.05
            )
            from zoneinfo import ZoneInfo

            energy = targets("pv_generation_energy")
            days = {}
            for i, at in enumerate(times):
                if test[i] and at in energy:
                    day = at.astimezone(ZoneInfo(config.timezone)).date()
                    values = days.setdefault(day, [0.0, 0.0, 0])
                    values[0] += prediction[i]
                    values[1] += energy[at]
                    values[2] += 1
            covered = [v for v in days.values() if v[2] >= 6]
            energy_check = {
                "status": "insufficient_covered_days",
                "covered_days": len(covered),
                "basis": "Matched covered daylight hours; counter timing is assessed separately",
            }
            if len(covered) >= 3:
                predicted_energy = np.array([v[0] for v in covered])
                observed_energy = np.array([v[1] for v in covered])
                energy_mae = float(np.mean(np.abs(predicted_energy - observed_energy)))
                energy_check.update(
                    status="evaluated",
                    test_mae_kwh=energy_mae,
                    test_relative_mae=energy_mae / max(0.1, float(np.mean(observed_energy))),
                )
            result = {
                "energy_total_check": energy_check,
                "status": "calibrated" if accepted else "rejected_holdout",
                "signature": signature(config),
                "efficiency": efficiency,
                "test_mae_kw": mae,
                "test_bias_kw": bias,
                "test_relative_mae": mae / scale,
                "holdout_accepted": accepted,
                "split_counts": {
                    "train": int(train.sum()),
                    "calibration": int(calibration.sum()),
                    "test": int(test.sum()),
                },
                "training_range": [start.isoformat(), first.isoformat()],
                "calibration_range": [
                    (first + timedelta(hours=24)).isoformat(),
                    second.isoformat(),
                ],
                "test_range": [(second + timedelta(hours=24)).isoformat(), end.isoformat()],
                "basis": "simultaneous_measured_dc_banks_and_ac_meter",
                "operating_confidence_validated": False,
                "updated_at": stamp(),
            }
    previous = store.meta("pv_ac_calibration") or {}
    if result.get("holdout_accepted"):
        if previous.get("holdout_accepted"):
            store.meta("previous_pv_ac_calibration", previous)
    elif previous.get("holdout_accepted") and previous.get("signature") == signature(config):
        result = {**previous, "retained_previous": True, "candidate": result}
    store.meta("pv_ac_calibration", result)
    return result
