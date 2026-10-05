"""Bounded chronological candidates. Predictive fit never unlocks export safety."""

import json
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

import numpy as np

from .composition import dt
from .forecast import canonical
from .jobs import update
from .store import digest, now, stamp
from .weather_features import features as weather_features
from .weather_features import source_epoch as weather_source_epoch

CONTEXT = 168
HORIZON = 49  # fractional first + last bins cover a rolling 48-hour request


def time_features(times, zone):
    local = [t.astimezone(ZoneInfo(zone)) for t in times]
    return np.array(
        [
            [
                np.sin(2 * np.pi * t.hour / 24),
                np.cos(2 * np.pi * t.hour / 24),
                np.sin(2 * np.pi * t.weekday() / 7),
                np.cos(2 * np.pi * t.weekday() / 7),
                np.sin(2 * np.pi * t.timetuple().tm_yday / 365.25),
                np.cos(2 * np.pi * t.timetuple().tm_yday / 365.25),
            ]
            for t in local
        ],
        dtype=np.float32,
    )


def hourly_targets(store, config, feature, cutoff=None):
    cutoff = cutoff or now()
    result = {}
    for row in canonical(store, config, feature, training_cutoff=cutoff):
        start, end = dt(row["start"]), dt(row["end"])
        if (
            start.minute == 0
            and start.second == 0
            and end - start == timedelta(hours=1)
            and end <= cutoff
            and row["energy_kwh"] is not None
            and row["quality"] in ("valid", "suspect")
        ):
            result[start] = row["energy_kwh"]
    return result


def partition_samples(targets, zone):
    if not targets:
        return [], {}, "No valid hourly targets"
    first, last = min(targets), max(targets) + timedelta(hours=1)
    length = (last - first).total_seconds() / 3600
    if length < 24 * 90:
        return [], {}, "At least 90 covered days are needed for disjoint candidate ranges"
    train_end = first + timedelta(hours=int(length * 0.6))
    calibration_end = first + timedelta(hours=int(length * 0.8))
    ranges = {
        "train": (first, train_end),
        "calibration": (train_end + timedelta(hours=HORIZON), calibration_end),
        "test": (calibration_end + timedelta(hours=HORIZON), last),
    }
    samples = []
    issue = first + timedelta(hours=CONTEXT)
    while issue + timedelta(hours=HORIZON) <= last:
        keys = [issue + timedelta(hours=i) for i in range(-CONTEXT, HORIZON)]
        if all(t in targets for t in keys):
            group = next(
                (
                    name
                    for name, (a, b) in ranges.items()
                    if issue >= a and issue + timedelta(hours=HORIZON) <= b
                ),
                None,
            )
            if group:
                samples.append(
                    {
                        "issue": issue,
                        "group": group,
                        "history": np.array([targets[t] for t in keys[:CONTEXT]], dtype=np.float32),
                        "target": np.array([targets[t] for t in keys[CONTEXT:]], dtype=np.float32),
                        "future": time_features(keys[CONTEXT:], zone),
                    }
                )
        issue += timedelta(hours=24)
    return samples, {k: [a.isoformat(), b.isoformat()] for k, (a, b) in ranges.items()}, None


def network(future_dimensions=6):
    import torch
    from torch import nn

    class CausalBlock(nn.Module):
        def __init__(self, dilation):
            super().__init__()
            self.padding = 2 * dilation
            self.conv = nn.Conv1d(32, 32, 3, dilation=dilation, padding=self.padding)

        def forward(self, x):
            return torch.relu(x + self.conv(x)[:, :, : -self.padding])

    class TCN(nn.Module):
        def __init__(self):
            super().__init__()
            self.input = nn.Conv1d(1, 32, 1)
            self.blocks = nn.Sequential(*(CausalBlock(d) for d in (1, 2, 4, 8, 16, 32, 64)))
            self.head = nn.Sequential(
                nn.Linear(32 + future_dimensions, 32), nn.ReLU(), nn.Linear(32, 3)
            )

        def forward(self, history, future):
            context = self.blocks(torch.relu(self.input(history[:, None, :])))[:, :, -1]
            repeated = context[:, None, :].expand(-1, future.shape[1], -1)
            raw = self.head(torch.cat([repeated, future], dim=-1))
            central = torch.nn.functional.softplus(raw[:, :, 1])
            lower = central * torch.sigmoid(raw[:, :, 0])
            upper = central + torch.nn.functional.softplus(raw[:, :, 2])
            return torch.stack([lower, central, upper], dim=-1)

    return TCN()


def profile_prediction(history):
    # Same hour in the previous week, available at issue time.
    return np.array([history[-168 + (i % 168)] for i in range(HORIZON)])


def mae(actual, predicted):
    return float(np.mean(np.abs(actual - predicted)))


def train(store, config, job):
    if config.learning_paused:
        return {"status": "skipped", "reason": "learning_paused"}
    cutoff = dt(job["created_at"])
    targets = hourly_targets(store, config, "household_load", cutoff)
    samples, ranges, reason = partition_samples(targets, config.timezone)
    counts = {
        group: sum(s["group"] == group for s in samples)
        for group in ("train", "calibration", "test")
    }
    if reason or min(counts.values(), default=0) < 8:
        return {
            "status": "skipped",
            "reason": reason or "Insufficient contiguous targets in each partition",
            "counts": counts,
            "pv": train_pv(store, config, cutoff),
        }
    if any(
        "source_transition" in r["reasons"]
        for r in canonical(store, config, "household_load")[-24 * config.recent_days :]
    ):
        return {"status": "skipped", "reason": "source_transition_review_required"}
    import torch

    torch.set_num_threads(2)
    torch.manual_seed(42)
    np.random.seed(42)
    model = network()
    fitting = [s for s in samples if s["group"] == "train"]
    # Training-only scale; never fit transforms to calibration/test targets.
    scale = max(0.1, float(np.mean(np.concatenate([s["history"] for s in fitting]))))
    history = torch.tensor(np.array([s["history"] for s in fitting]) / scale)
    future = torch.tensor(np.array([s["future"] for s in fitting]))
    target = torch.tensor(np.array([s["target"] for s in fitting]) / scale)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    directory = store.root / "models"
    directory.mkdir(exist_ok=True)
    checkpoint = directory / f"checkpoint-{job['id']}.pt"
    identity = digest(
        {
            "ranges": ranges,
            "timezone": config.timezone,
            "targets": {t.isoformat(): v for t, v in targets.items()},
        }
    )
    first_epoch = 0
    if checkpoint.exists():
        saved = torch.load(checkpoint, weights_only=True)
        if saved["identity"] == identity:
            model.load_state_dict(saved["model"])
            optimizer.load_state_dict(saved["optimizer"])
            first_epoch = saved["epoch"] + 1
    for epoch in range(first_epoch, 40):
        model.train()
        # Fixed bounded sample set keeps seasonal data and high demand; no rare-tail deletion.
        for start in range(0, len(history), 32):
            optimizer.zero_grad()
            prediction = model(history[start : start + 32], future[start : start + 32])
            error = target[start : start + 32, :, None] - prediction
            quantiles = torch.tensor([0.05, 0.5, 0.95])
            loss = torch.maximum(quantiles * error, (quantiles - 1) * error).mean()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
            optimizer.step()
        temporary = checkpoint.with_suffix(".tmp")
        torch.save(
            {
                "model": model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "epoch": epoch,
                "identity": identity,
            },
            temporary,
        )
        temporary.replace(checkpoint)
        update(store, job["id"], progress=0.1 + 0.7 * (epoch + 1) / 40, checkpoint_epoch=epoch)
    model.eval()
    predictions = {}
    with torch.no_grad():
        for group in ("calibration", "test"):
            subset = [s for s in samples if s["group"] == group]
            h = torch.tensor(np.array([s["history"] for s in subset]) / scale)
            f = torch.tensor(np.array([s["future"] for s in subset]))
            predictions[group] = (subset, model(h, f).numpy() * scale)
    calibration, cal_predictions = predictions["calibration"]
    cal_actual = np.array([s["target"] for s in calibration])
    q05 = np.quantile(cal_actual - cal_predictions[:, :, 0], 0.05, axis=0)
    q95 = np.quantile(cal_actual - cal_predictions[:, :, 2], 0.95, axis=0)
    test, test_quantiles = predictions["test"]
    test_predictions = test_quantiles[:, :, 1]
    actual = np.array([s["target"] for s in test])
    baseline = np.array([profile_prediction(s["history"]) for s in test])
    model_mae, baseline_mae = mae(actual, test_predictions), mae(actual, baseline)
    coverage = float(
        np.mean(
            (actual >= np.minimum(test_predictions, np.maximum(0, test_quantiles[:, :, 0] + q05)))
            & (actual <= np.maximum(test_predictions, test_quantiles[:, :, 2] + q95))
        )
    )
    # Compare profile tails on the SAME calibration/test partitions.
    base_cal = np.array([profile_prediction(s["history"]) for s in calibration])
    base_residuals = np.array([s["target"] for s in calibration]) - base_cal
    blo, bhi = np.quantile(base_residuals, [0.05, 0.95], axis=0)
    base_coverage = float(
        np.mean((actual >= np.maximum(0, baseline + blo)) & (actual <= baseline + bhi))
    )
    gates = {
        "mae_improved": model_mae < baseline_mae,
        "interval_coverage_not_degraded": coverage >= base_coverage,
        "peak_error_not_degraded": mae(actual.max(axis=1), test_predictions.max(axis=1))
        <= mae(actual.max(axis=1), baseline.max(axis=1)),
    }
    identifier = uuid.uuid4().hex
    path = directory / f"{identifier}.pt"
    torch.save(model.state_dict(), path)
    bundle = {
        "id": identifier,
        "created_at": stamp(),
        "kind": "tcn-calendar-quantile-v2",
        "future_dimensions": 6,
        "input_watermark": job["created_at"],
        "input_data_hash": identity,
        "timezone": config.timezone,
        "scale": scale,
        "configuration_hash": digest(config.model_dump(mode="json")),
        "ranges": ranges,
        "counts": counts,
        "metrics": {
            "test_mae_kwh": model_mae,
            "baseline_mae_kwh": baseline_mae,
            "test_interval_coverage": coverage,
            "baseline_interval_coverage": base_coverage,
        },
        "gates": gates,
        "promotion_eligible": all(gates.values()),
        "q05_residual": q05.tolist(),
        "q95_residual": q95.tolist(),
        "limitations": [
            "Calendar cycles only; weather-feature ablation awaits matching vintages",
            "Hourly interval coverage does not validate whole-path export confidence",
        ],
        "source_signature": digest([m.model_dump(mode="json") for m in config.mappings]),
        "export_validated": False,
    }
    store.insert_document("models", identifier, bundle)
    checkpoint.unlink(missing_ok=True)
    if bundle["promotion_eligible"] and not (store.configuration() or {}).get(
        "learning_paused", True
    ):
        activate_model(store, identifier)
    from .schedule_learning import train_candidate as train_schedule
    from .weather_learning import train_candidate

    return {
        "status": "trained",
        "model": bundle,
        "weather_candidate": train_candidate(store, config, job, targets),
        "schedule_candidate": train_schedule(store, config, job, samples, bundle),
        "pv": train_pv(store, config, cutoff),
    }


def activate_model(store, identifier):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT payload FROM models WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("Unknown candidate")
        bundle = json.loads(row[0])
        if not bundle.get("promotion_eligible"):
            raise ValueError("Candidate failed predictive validation gates")
        config = store.configuration()
        if bundle["source_signature"] != digest(config["mappings"]):
            raise ValueError("Source mapping changed; retrain before promotion")
        if bundle.get("timezone", config["timezone"]) != config["timezone"]:
            raise ValueError("Site timezone changed; retrain before promotion")
        if bundle.get("weather_epoch"):
            from .schemas import Configuration

            if bundle["weather_epoch"] != weather_source_epoch(
                Configuration.model_validate(config)
            ):
                raise ValueError("Weather source changed; retrain before promotion")
        active = store.meta("active_model")
        if active == identifier:
            return {"active_model": identifier, "unchanged": True}
        # One transaction activates the complete immutable bundle and invalidates leases.
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('previous_model',?)", (json.dumps(active),)
        )
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('active_model',?)", (json.dumps(identifier),)
        )
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('invalidated_at',?)", (json.dumps(stamp()),)
        )
        db.execute("UPDATE models SET status='active' WHERE id=?", (identifier,))
        db.execute(
            "INSERT INTO audit(created,action,payload) VALUES (?,?,?)",
            (stamp(), "model_activated", json.dumps({"id": identifier})),
        )
    return {"active_model": identifier, "export_ready": False}


def predict_load(store, config, times, baseline, issue, weather=None, weather_rows=None):
    identifier = store.meta("active_model")
    if not identifier:
        return baseline, None, None, None
    bundle = next((b for b in store.documents("models") if b["id"] == identifier), None)
    if not bundle or bundle["source_signature"] != digest(
        [m.model_dump(mode="json") for m in config.mappings]
    ):
        return baseline, None, None, None
    if bundle.get("timezone", config.timezone) != config.timezone:
        return baseline, None, None, None
    dimensions = bundle.get("future_dimensions", 6)
    future = time_features(times, config.timezone)
    if dimensions == 11:
        from .schedule_learning import future_features

        if bundle.get("calendar_signature") != digest(config.calendars.model_dump(mode="json")):
            return baseline, None, None, None
        future = future_features(config, times, issue)
    if dimensions == 8:
        if (
            not weather
            or not weather_rows
            or bundle.get("weather_epoch") != weather_source_epoch(config)
            or weather.get("source_epoch") != bundle.get("weather_epoch")
            or not 0
            <= (issue - dt(weather["received_at"])).total_seconds()
            <= config.weather_freshness_seconds
        ):
            return baseline, None, None, None
        environmental = weather_features(weather_rows)
        if environmental is None or len(environmental) != len(times):
            return baseline, None, None, None
        future = np.concatenate((future, environmental), axis=1)
    targets = hourly_targets(store, config, "household_load", issue)
    anchor = times[0].replace(minute=0, second=0, microsecond=0)
    history_times = [anchor - timedelta(hours=CONTEXT - i) for i in range(CONTEXT)]
    if not all(t in targets for t in history_times):
        return baseline, None, None, None
    import torch

    model = network(dimensions)
    model.load_state_dict(torch.load(store.root / "models" / f"{identifier}.pt", weights_only=True))
    model.eval()
    h = np.array([[targets[t] for t in history_times]], dtype=np.float32) / bundle["scale"]
    with torch.no_grad():
        predicted_quantiles = (
            model(torch.tensor(h), torch.tensor(future[None, :, :])).numpy()[0] * bundle["scale"]
        )
    values = predicted_quantiles[:, 1]
    # Bounded recent residual profile; only completed observations and previous predictions.
    residual = store.meta("recent_bias") or {}
    if residual.get("model_id") == identifier:
        profile = residual.get("profile", {})
        corrections = []
        for instant in times:
            local = instant.astimezone(ZoneInfo(config.timezone))
            corrections.append(
                profile.get(f"{local.weekday()}:{local.hour}", residual.get("load_kw", 0))
            )
        correction = np.clip(corrections, -0.2 * np.mean(values), 0.2 * np.mean(values))
    else:
        correction = 0
    values = np.maximum(0, values + correction)
    low = np.minimum(
        values,
        np.maximum(
            0,
            predicted_quantiles[:, 0]
            + correction
            + np.array(bundle["q05_residual"][: len(values)]),
        ),
    )
    high = np.maximum(
        values,
        predicted_quantiles[:, 2] + correction + np.array(bundle["q95_residual"][: len(values)]),
    )
    return values.tolist(), low.tolist(), high.tolist(), identifier


def train_pv(store, config, cutoff):
    from lightgbm import LGBMRegressor

    from .weather import physical_pv

    targets = hourly_targets(store, config, "pv_generation", cutoff)
    rows = {}
    for vintage in store.origins("weather", step_hours=12, limit=20000):
        for interval in vintage["intervals"]:
            start = dt(interval["start"])
            if start in targets and (
                (vintage["kind"] == "live_forecast" and dt(vintage["received_at"]) <= start)
                or (
                    vintage["kind"] == "single_run_archive"
                    and vintage.get("provider_available_at")
                    and dt(vintage["provider_available_at"]) <= start
                )
            ):
                rows.setdefault(start, interval)
    times = sorted(rows)
    if len(times) < 24 * 60:
        return {
            "status": "skipped",
            "reason": "Need 60 days of matching issue-time weather and PV targets",
        }
    selected = [rows[t] for t in times]
    physical, _, _ = physical_pv(config, selected)
    x = np.array(
        [
            [
                physical[i],
                selected[i]["temperature_2m"],
                selected[i]["shortwave_radiation"],
                selected[i]["cloud_cover"],
            ]
            for i in range(len(times))
        ]
    )
    y = np.array([targets[t] for t in times])
    train_end, cal_end = int(len(times) * 0.6), int(len(times) * 0.8)
    # Time embargo protects overlapping origins even though trees predict individual hours.
    calibration_mask = [
        i
        for i in range(train_end, cal_end)
        if times[i] >= times[train_end - 1] + timedelta(hours=48)
    ]
    test_mask = [
        i
        for i in range(cal_end, len(times))
        if times[i] >= times[cal_end - 1] + timedelta(hours=48)
    ]
    if len(calibration_mask) < 100 or len(test_mask) < 100 or not np.all(np.isfinite(x)):
        return {"status": "skipped", "reason": "PV holdouts/weather incomplete"}
    models = {}
    directory = store.root / "models"
    identifier = uuid.uuid4().hex
    for quantile in (0.05, 0.5, 0.95):
        model = LGBMRegressor(
            objective="quantile",
            alpha=quantile,
            n_estimators=100,
            num_leaves=15,
            n_jobs=2,
            verbosity=-1,
            random_state=42,
        )
        model.fit(x[:train_end], y[:train_end])
        models[str(quantile)] = model
    predicted = models["0.5"].predict(x[test_mask])
    metrics = {
        "test_mae_kwh": mae(y[test_mask], predicted),
        "physical_baseline_mae_kwh": mae(y[test_mask], physical[test_mask]),
    }
    eligible = metrics["test_mae_kwh"] < metrics["physical_baseline_mae_kwh"]
    # Keep PV candidate separate until bank/topology/coverage validation has been commissioned.
    for quantile, model in models.items():
        model.booster_.save_model(str(directory / f"pv-{identifier}-{quantile}.txt"))
    report = {
        "id": identifier,
        "kind": "aggregate-pv-lightgbm",
        "metrics": metrics,
        "predictive_improvement": eligible,
        "promotion_eligible": False,
        "export_validated": False,
        "limitations": [
            "Aggregate PV allocation is not identifiable per bank",
            "PV candidate requires topology review",
        ],
        "ranges": {
            "train": [times[0].isoformat(), times[train_end - 1].isoformat()],
            "calibration": [
                times[calibration_mask[0]].isoformat(),
                times[calibration_mask[-1]].isoformat(),
            ],
            "test": [times[test_mask[0]].isoformat(), times[test_mask[-1]].isoformat()],
        },
    }
    store.insert_document("models", identifier, report)
    return report


def paired_scenarios(pv, load, residual_blocks, count=2000, seed=42):
    blocks = np.asarray(residual_blocks, dtype=float)
    if blocks.ndim != 3 or blocks.shape[1:] != (len(pv), 2):
        raise ValueError("Use paired complete-horizon PV/load residual blocks")
    indices = np.random.default_rng(seed).integers(0, len(blocks), count)
    selected = blocks[indices]
    return np.maximum(0, np.asarray(pv) + selected[:, :, 0]), np.maximum(
        0, np.asarray(load) + selected[:, :, 1]
    )


def calibrate(store, config):
    # Only completed, nonoverlapping horizons count. No future outcomes/revised weather.
    load = hourly_targets(store, config, "household_load")
    pv = hourly_targets(store, config, "pv_generation")
    latest = store.latest("forecasts")
    signature = (
        (latest["model_versions"].get("validation_cohort") or digest(latest["model_versions"]))
        if latest
        else None
    )
    blocks, selected, last_end = [], [], None
    for forecast in store.origins(
        step_hours=48, config_hash=digest(config.model_dump(mode="json"))
    ):
        if (
            forecast["model_versions"].get("validation_cohort")
            or digest(forecast["model_versions"])
        ) != signature or forecast["configuration_hash"] != digest(config.model_dump(mode="json")):
            continue
        if dt(forecast["issued_at"]) + timedelta(hours=48) > now():
            continue
        rows = forecast["series"]
        if len(rows) != HORIZON or (last_end and dt(rows[0]["start"]) < last_end):
            continue
        # Hourly actual means cover fractional endpoint bins; this cannot validate peaks.
        keys = [dt(r["start"]).replace(minute=0, second=0, microsecond=0) for r in rows]
        if any(
            t not in load or t not in pv or r["load_kw"] is None or r["pv_kw"] is None
            for t, r in zip(keys, rows)
        ):
            continue
        block = [[pv[t] - r["pv_kw"], load[t] - r["load_kw"]] for t, r in zip(keys, rows)]
        blocks.append(block)
        selected.append(forecast["forecast_id"])
        last_end = keys[-1] + timedelta(hours=1)
    report = {
        "status": "insufficient_data" if len(blocks) < 30 else "shadow",
        "complete_nonoverlapping_paths": len(blocks),
        "selected_forecasts": selected,
        "whole_path_confidence_validated": False,
        "model_signature": signature,
        "configuration_hash": digest(config.model_dump(mode="json")),
        "updated_at": stamp(),
        "reason": "Real operating envelope and independent planner outcomes remain unvalidated",
    }
    if blocks:
        directory = store.root / "models"
        directory.mkdir(exist_ok=True)
        np.savez_compressed(directory / "paired-residuals.npz", blocks=np.array(blocks))
    from .passive import calibrate_battery

    store.meta("battery_calibration", calibrate_battery(store, config))
    store.meta("calibration", report)
    return report


def zero_failure_upper_bound(independent_trials, confidence=0.95):
    return 1 - (1 - confidence) ** (1 / independent_trials) if independent_trials > 0 else 1.0


def adapt_recent(store, config, daily=False):
    """Bounded residual profile retaining the long-term branch and archived features."""
    from collections import defaultdict

    identifier = store.meta("active_model")
    if config.learning_paused or not identifier:
        return {"status": "skipped", "reason": "learning_paused_or_no_validated_model"}
    cutoff = now()
    targets = hourly_targets(store, config, "household_load", cutoff)
    recent = cutoff - timedelta(days=config.recent_days)
    rows = canonical(store, config, "household_load")
    if any(
        row["quality"] == "invalid" or "source_transition" in row["reasons"]
        for row in rows
        if dt(row["end"]) >= recent
    ):
        return {"status": "paused", "reason": "source_quality_review_required"}
    residuals, actual_values = {}, {}
    for snapshot in store.origins(
        step_hours=1, limit=20000, config_hash=digest(config.model_dump(mode="json"))
    ):
        if snapshot["model_versions"].get("load") != identifier:
            continue
        for row in snapshot["series"]:
            valid = dt(row["start"])
            end = dt(row["end"])
            # Only exact measured-hour targets. Do not treat partial bins as ground truth.
            if (
                end > cutoff
                or valid < recent
                or end - valid != timedelta(hours=1)
                or valid not in targets
                or row["load_kw"] is None
            ):
                continue
            # One residual per valid hour, against the earliest matching archived issuance.
            residuals.setdefault(valid, targets[valid] - row["load_kw"])
            actual_values[valid] = targets[valid]
    days = {t.date() for t in residuals}
    if len(days) < 7 or len(residuals) < 7 * 18:
        return {
            "status": "skipped",
            "reason": "Need seven days of covered completed model outcomes",
            "hours": len(residuals),
        }
    groups = defaultdict(list)
    for timestamp, value in residuals.items():
        local = timestamp.astimezone(ZoneInfo(config.timezone))
        groups[f"{local.weekday()}:{local.hour}"].append(value)
    mean_load = float(np.mean(list(actual_values.values())))
    cap = 0.2 * mean_load
    global_bias = float(
        np.clip(np.mean(list(residuals.values())) * len(days) / (len(days) + 14), -cap, cap)
    )
    profile = (
        {
            key: float(np.clip(np.mean(values) * len(values) / (len(values) + 14), -cap, cap))
            for key, values in groups.items()
        }
        if daily
        else (store.meta("recent_bias") or {}).get("profile", {})
    )
    report = {
        "id": uuid.uuid4().hex,
        "model_id": identifier,
        "updated_at": stamp(),
        "window_days": config.recent_days,
        "load_kw": global_bias,
        "profile": profile,
        "completed_hours": len(residuals),
        "correction_cap_kw": cap,
        "status": "shadow",
        "daily_profile": daily,
    }
    ordered = sorted(residuals)
    split = int(len(ordered) * 0.7)
    fitting, validation = ordered[:split], ordered[split:]
    if len(validation) < 24:
        return {"status": "skipped", "reason": "Recent validation range is too small"}
    fit_bias = float(
        np.clip(
            np.mean([residuals[t] for t in fitting]) * len(fitting) / (len(fitting) + 24 * 14),
            -cap,
            cap,
        )
    )
    fit_groups = defaultdict(list)
    for timestamp in fitting:
        local = timestamp.astimezone(ZoneInfo(config.timezone))
        fit_groups[f"{local.weekday()}:{local.hour}"].append(residuals[timestamp])
    fit_profile = (
        {
            key: float(np.clip(np.mean(values) * len(values) / (len(values) + 14), -cap, cap))
            for key, values in fit_groups.items()
        }
        if daily
        else {}
    )
    previous = store.meta("recent_bias") or {}
    if previous.get("model_id") != identifier:
        previous = {}
    previous_global = previous.get("load_kw", 0)
    previous_profile = previous.get("profile", {})
    candidate_global = float(np.clip(previous_global + fit_bias, -cap, cap))
    candidate_profile = dict(previous_profile)
    if daily:
        for key, delta in fit_profile.items():
            candidate_profile[key] = float(
                np.clip(previous_profile.get(key, previous_global) + delta, -cap, cap)
            )

    def effective_delta(timestamp):
        local = timestamp.astimezone(ZoneInfo(config.timezone))
        key = f"{local.weekday()}:{local.hour}"
        return candidate_profile.get(key, candidate_global) - previous_profile.get(
            key, previous_global
        )

    old_error = float(np.mean([abs(residuals[t]) for t in validation]))
    new_error = float(np.mean([abs(residuals[t] - effective_delta(t)) for t in validation]))
    report.update(
        load_kw=candidate_global,
        profile=candidate_profile,
        validation={
            "start": validation[0].isoformat(),
            "end": validation[-1].isoformat(),
            "old_mae_kw": old_error,
            "new_mae_kw": new_error,
        },
    )
    if new_error >= old_error:
        return {
            **report,
            "status": "not_promoted",
            "reason": "Recent held-out error did not improve",
        }
    if (store.configuration() or {}).get("learning_paused", True):
        return {**report, "status": "not_promoted", "reason": "Learning paused during evaluation"}
    store.meta("recent_bias", report)
    store.meta("invalidated_at", stamp())
    store.audit(
        "bounded_recent_update",
        {"id": report["id"], "model_id": identifier, "completed_hours": len(residuals)},
    )
    # Persist drift/cooldown; one unusual hour cannot repeatedly trigger full training.
    drift = store.meta("drift") or {"days": [], "last_candidate_at": None}
    if abs(float(np.mean(list(residuals.values())))) > max(0.1, 0.2 * mean_load):
        today = cutoff.date().isoformat()
        drift["days"] = sorted(set(drift["days"] + [today]))[-3:]
        if len(drift["days"]) >= 3 and (
            not drift["last_candidate_at"]
            or cutoff - dt(drift["last_candidate_at"]) >= timedelta(days=7)
        ):
            from .jobs import enqueue

            enqueue(store, "train", scheduled=True)
            drift["last_candidate_at"] = stamp()
    else:
        drift["days"] = []
    store.meta("drift", drift)
    return report
