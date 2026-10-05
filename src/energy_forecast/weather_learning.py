"""Weather-aware load candidate with matched chronological ablation."""

import uuid
from datetime import timedelta

import numpy as np

from .store import digest, stamp
from .weather_features import features as weather_features
from .weather_features import for_issue, source_epoch

CONTEXT = 168
HORIZON = 49


def matched_samples(store, config, targets):
    """Use only complete targets and weather actually available at each issue."""
    if not targets:
        return []
    from .learning import time_features

    last = max(targets) + timedelta(hours=1)
    issue = min(targets) + timedelta(hours=CONTEXT)
    result = []
    while issue + timedelta(hours=HORIZON) <= last:
        hours = [issue + timedelta(hours=i) for i in range(-CONTEXT, HORIZON)]
        if all(hour in targets for hour in hours):
            vintage = for_issue(store, config, issue, HORIZON)
            if vintage:
                document, rows = vintage
                weather = weather_features(rows)
                result.append(
                    {
                        "issue": issue,
                        "history": np.array(
                            [targets[t] for t in hours[:CONTEXT]], dtype=np.float32
                        ),
                        "target": np.array([targets[t] for t in hours[CONTEXT:]], dtype=np.float32),
                        "calendar_future": time_features(hours[CONTEXT:], config.timezone),
                        "weather_future": weather,
                        "weather_vintage": document["id"],
                    }
                )
        issue += timedelta(hours=24)
    return result


def partitions(samples):
    """Disjoint completed targets, including a 49-hour embargo at each split."""
    if not samples:
        return [], {}, "No issue-time weather with complete targets"
    first, last = samples[0]["issue"], samples[-1]["issue"] + timedelta(hours=HORIZON)
    if last - first < timedelta(days=90):
        return [], {}, "Need 90 days of matching weather and completed load targets"
    length = (last - first).total_seconds()
    train_end = first + timedelta(seconds=int(length * 0.6))
    calibration_end = first + timedelta(seconds=int(length * 0.8))
    ranges = {
        "train": (first, train_end),
        "calibration": (train_end + timedelta(hours=HORIZON), calibration_end),
        "test": (calibration_end + timedelta(hours=HORIZON), last),
    }
    selected = []
    for sample in samples:
        group = next(
            (
                name
                for name, (start, end) in ranges.items()
                if start <= sample["issue"] and sample["issue"] + timedelta(hours=HORIZON) <= end
            ),
            None,
        )
        if group:
            selected.append({**sample, "group": group})
    return (
        selected,
        {name: [start.isoformat(), end.isoformat()] for name, (start, end) in ranges.items()},
        None,
    )


def _fit(samples, scale, dimensions, epochs, job, store, suffix):
    import torch

    from .jobs import update
    from .learning import network

    torch.set_num_threads(2)
    torch.manual_seed(42)
    model = network(dimensions)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    history = torch.tensor(np.array([s["history"] for s in samples]) / scale)
    calendar = np.array([s["calendar_future"] for s in samples])
    future = (
        calendar
        if dimensions == 6
        else np.concatenate((calendar, np.array([s["weather_future"] for s in samples])), axis=2)
    )
    future = torch.tensor(future)
    target = torch.tensor(np.array([s["target"] for s in samples]) / scale)
    checkpoint = store.root / "models" / f"checkpoint-{suffix}-{job['id']}.pt"
    identity = digest(
        {
            "issue": [s["issue"].isoformat() for s in samples],
            "weather": [s["weather_vintage"] for s in samples] if dimensions == 8 else None,
            "target": [s["target"].tolist() for s in samples],
            "dimensions": dimensions,
            "future": future.tolist(),
        }
    )
    first_epoch = 0
    if checkpoint.exists():
        saved = torch.load(checkpoint, weights_only=True)
        if saved["identity"] == identity:
            model.load_state_dict(saved["model"])
            optimizer.load_state_dict(saved["optimizer"])
            first_epoch = saved["epoch"] + 1
    quantiles = torch.tensor([0.05, 0.5, 0.95])
    for epoch in range(first_epoch, epochs):
        model.train()
        for start in range(0, len(history), 32):
            optimizer.zero_grad()
            prediction = model(history[start : start + 32], future[start : start + 32])
            error = target[start : start + 32, :, None] - prediction
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
        update(
            store,
            job["id"],
            progress=(0.8 if dimensions == 6 else 0.875) + 0.075 * ((epoch + 1) / epochs),
            checkpoint_epoch=epoch,
            phase=suffix,
        )
    checkpoint.unlink(missing_ok=True)
    return model


def _evaluate(model, samples, scale, dimensions):
    import torch

    model.eval()
    history = torch.tensor(np.array([s["history"] for s in samples]) / scale)
    calendar = np.array([s["calendar_future"] for s in samples])
    future = (
        calendar
        if dimensions == 6
        else np.concatenate((calendar, np.array([s["weather_future"] for s in samples])), axis=2)
    )
    with torch.no_grad():
        return model(history, torch.tensor(future)).numpy() * scale


def _metrics(calibration, test, cal_prediction, test_prediction):
    from .learning import mae

    cal_actual = np.array([s["target"] for s in calibration])
    test_actual = np.array([s["target"] for s in test])
    low = np.quantile(cal_actual - cal_prediction[:, :, 0], 0.05, axis=0)
    high = np.quantile(cal_actual - cal_prediction[:, :, 2], 0.95, axis=0)
    point = test_prediction[:, :, 1]
    coverage = float(
        np.mean(
            (test_actual >= np.minimum(point, np.maximum(0, test_prediction[:, :, 0] + low)))
            & (test_actual <= np.maximum(point, test_prediction[:, :, 2] + high))
        )
    )
    return (
        {
            "mae_kwh": mae(test_actual, point),
            "peak_mae_kwh": mae(test_actual.max(axis=1), point.max(axis=1)),
            "interval_coverage": coverage,
        },
        low,
        high,
    )


def train_candidate(store, config, job, targets):
    from .learning import mae, profile_prediction

    eligible = matched_samples(store, config, targets)
    samples, ranges, reason = partitions(eligible)
    counts = {
        group: sum(s["group"] == group for s in samples)
        for group in ("train", "calibration", "test")
    }
    if reason or min(counts.values(), default=0) < 8:
        return {
            "status": "skipped",
            "reason": reason or "Insufficient matched origins in every partition",
            "counts": counts,
        }
    (store.root / "models").mkdir(exist_ok=True)
    fitting = [s for s in samples if s["group"] == "train"]
    calibration = [s for s in samples if s["group"] == "calibration"]
    test = [s for s in samples if s["group"] == "test"]
    scale = max(0.1, float(np.mean(np.concatenate([s["history"] for s in fitting]))))
    calendar_model = _fit(fitting, scale, 6, 25, job, store, "calendar-ablation")
    weather_model = _fit(fitting, scale, 8, 25, job, store, "weather")
    calendar_metrics, _, _ = _metrics(
        calibration,
        test,
        _evaluate(calendar_model, calibration, scale, 6),
        _evaluate(calendar_model, test, scale, 6),
    )
    weather_metrics, low, high = _metrics(
        calibration,
        test,
        _evaluate(weather_model, calibration, scale, 8),
        _evaluate(weather_model, test, scale, 8),
    )
    actual = np.array([s["target"] for s in test])
    profile = np.array([profile_prediction(s["history"]) for s in test])
    profile_mae = mae(actual, profile)
    profile_peak_mae = mae(actual.max(axis=1), profile.max(axis=1))
    span_days = (samples[-1]["issue"] - samples[0]["issue"]).days
    months = len({s["issue"].strftime("%Y-%m") for s in samples})
    gates = {
        "complete_annual_cycle": span_days >= 365 and months >= 10,
        "better_than_calendar_ablation": weather_metrics["mae_kwh"] < calendar_metrics["mae_kwh"],
        "better_than_weekday_profile": weather_metrics["mae_kwh"] < profile_mae,
        "interval_coverage_not_degraded": weather_metrics["interval_coverage"]
        >= calendar_metrics["interval_coverage"],
        "peak_error_not_degraded": weather_metrics["peak_mae_kwh"]
        <= min(calendar_metrics["peak_mae_kwh"], profile_peak_mae),
    }
    identifier = uuid.uuid4().hex
    directory = store.root / "models"
    directory.mkdir(exist_ok=True)
    import torch

    torch.save(weather_model.state_dict(), directory / f"{identifier}.pt")
    bundle = {
        "id": identifier,
        "created_at": stamp(),
        "kind": "tcn-calendar-weather-quantile-v1",
        "future_dimensions": 8,
        "scale": scale,
        "configuration_hash": digest(config.model_dump(mode="json")),
        "source_signature": digest([m.model_dump(mode="json") for m in config.mappings]),
        "weather_epoch": source_epoch(config),
        "timezone": config.timezone,
        "ranges": ranges,
        "counts": counts,
        "matching_weather_vintages": len({s["weather_vintage"] for s in samples}),
        "weather_feature_version": "temperature-radiation-v1",
        "input_watermark": job["created_at"],
        "weather_origins": [
            {
                "issue": s["issue"].isoformat(),
                "vintage": s["weather_vintage"],
                "partition": s["group"],
            }
            for s in samples
        ],
        "input_data_hash": digest(
            {
                "history": [s["history"].tolist() for s in samples],
                "targets": [s["target"].tolist() for s in samples],
            }
        ),
        "metrics": {
            "weather": weather_metrics,
            "calendar_only": calendar_metrics,
            "weekday_profile_mae_kwh": profile_mae,
        },
        "gates": gates,
        "promotion_eligible": all(gates.values()),
        "q05_residual": low.tolist(),
        "q95_residual": high.tolist(),
        "export_validated": False,
        "limitations": [
            "Only issue-time weather is eligible; reanalysis is excluded from evaluation",
            "Interval coverage does not establish whole-path export confidence",
        ],
    }
    store.insert_document("models", identifier, bundle)
    if bundle["promotion_eligible"] and not (store.configuration() or {}).get(
        "learning_paused", True
    ):
        from .learning import activate_model

        activate_model(store, identifier)
    return {"status": "trained", "model": bundle}
