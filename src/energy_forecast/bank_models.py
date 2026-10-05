"""Small per-bank boosted candidates; chronological holdout controls activation."""

from functools import lru_cache

import lightgbm as lgb
import numpy as np
import pandas as pd


def inputs(rows, reference, poa):
    hours = np.array([pd.Timestamp(r["start"]).hour for r in rows])
    return pd.DataFrame(
        {
            "geometry_kw": reference,
            "poa": poa,
            "ghi": [r["shortwave_radiation"] for r in rows],
            "dni": [r["direct_normal_irradiance"] for r in rows],
            "dhi": [r["diffuse_radiation"] for r in rows],
            "temperature": [r["temperature_2m"] for r in rows],
            "hour_sin": np.sin(hours * 2 * np.pi / 24),
            "hour_cos": np.cos(hours * 2 * np.pi / 24),
        }
    )


def fit_candidate(frame, actual, train, calibration):
    model = lgb.LGBMRegressor(
        n_estimators=120,
        num_leaves=15,
        min_child_samples=20,
        learning_rate=0.04,
        n_jobs=2,
        verbosity=-1,
        random_state=42,
    )
    model.fit(
        frame[train],
        actual[train],
        eval_set=[(frame[calibration], actual[calibration])],
        callbacks=[lgb.early_stopping(15, verbose=False)],
    )
    return model, np.maximum(0, model.predict(frame))


@lru_cache(maxsize=16)
def load_model(path):
    return lgb.Booster(model_file=path)


def predict_banks(store, config, rows, banks, poa, report):
    from .solar_calibration import geometry_signature

    for bank in config.banks:
        result = report.get("banks", {}).get(bank.id, {})
        model_id = result.get("model_id")
        if not model_id or result.get("geometry_signature") != geometry_signature(config, bank):
            continue
        path = store.root / "models" / f"pv-bank-{model_id}.txt"
        if not path.exists():
            continue
        frame = inputs(rows, banks[bank.id], poa[bank.id])
        predicted = np.maximum(0, load_model(str(path)).predict(frame))
        banks[bank.id] = np.where(
            np.isfinite(banks[bank.id]), np.where(poa[bank.id] <= 0, 0, predicted), np.nan
        )
    groups = {}
    for bank in config.banks:
        groups.setdefault(bank.inverter_group, []).append(bank.id)
    for group, members in groups.items():
        if group in config.inverter_limits_kw:
            total = sum(banks[key] for key in members)
            factor = np.minimum(1, config.inverter_limits_kw[group] / np.maximum(total, 1e-9))
            for key in members:
                banks[key] *= factor
    return sum(banks.values(), np.zeros(len(rows))), banks
