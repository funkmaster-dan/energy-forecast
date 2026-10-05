"""Matched calendar/household ablation on the same chronological load holdouts."""

import uuid

import numpy as np

from .calendars import features
from .store import digest, stamp


def future_features(config, times, issue):
    from .learning import time_features

    extra = []
    for at in times:
        value = features(config, at, issue)
        extra.append(
            [
                bool(value["is_public_holiday"]),
                bool(value["is_school_holiday"]),
                any(v["kind"] == "household" for v in value["labels"]),
                value["is_public_holiday"] is not None,
                value["is_school_holiday"] is not None,
            ]
        )
    return np.concatenate(
        [time_features(times, config.timezone), np.array(extra, dtype=np.float32)], axis=1
    )


def train_candidate(store, config, job, samples, control):
    if not config.calendars.enabled:
        return {"status": "skipped", "reason": "calendar_disabled"}
    from datetime import timedelta

    import torch

    from .learning import HORIZON, mae, network, profile_prediction

    torch.manual_seed(43)
    model = network(11)
    scale = control["scale"]
    fitting = [s for s in samples if s["group"] == "train"]

    def future(subset):
        return np.array(
            [
                future_features(
                    config, [s["issue"] + timedelta(hours=i) for i in range(HORIZON)], s["issue"]
                )
                for s in subset
            ]
        )

    h = torch.tensor(np.array([s["history"] for s in fitting]) / scale)
    f = torch.tensor(future(fitting))
    y = torch.tensor(np.array([s["target"] for s in fitting]) / scale)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    path = store.root / "models" / f"schedule-checkpoint-{job['id']}.pt"
    identity = digest(
        {"data": control["input_data_hash"], "calendar": config.calendars.model_dump(mode="json")}
    )
    first = 0
    if path.exists():
        checkpoint = torch.load(path, weights_only=True)
        if checkpoint["identity"] == identity:
            model.load_state_dict(checkpoint["model"])
            optimizer.load_state_dict(checkpoint["optimizer"])
            first = checkpoint["epoch"] + 1
    for epoch in range(first, 40):
        model.train()
        for start in range(0, len(h), 32):
            optimizer.zero_grad()
            p = model(h[start : start + 32], f[start : start + 32])
            error = y[start : start + 32, :, None] - p
            q = torch.tensor([0.05, 0.5, 0.95])
            loss = torch.maximum(q * error, (q - 1) * error).mean()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
            optimizer.step()
        temporary = path.with_suffix(".tmp")
        torch.save(
            {
                "identity": identity,
                "epoch": epoch,
                "model": model.state_dict(),
                "optimizer": optimizer.state_dict(),
            },
            temporary,
        )
        temporary.replace(path)
    model.eval()
    outputs = {}
    with torch.no_grad():
        for group in ("calibration", "test"):
            subset = [s for s in samples if s["group"] == group]
            prediction = (
                model(
                    torch.tensor(np.array([s["history"] for s in subset]) / scale),
                    torch.tensor(future(subset)),
                ).numpy()
                * scale
            )
            outputs[group] = (subset, prediction)
    calibration, pcal = outputs["calibration"]
    actual_cal = np.array([s["target"] for s in calibration])
    lo, hi = (
        np.quantile(actual_cal - pcal[:, :, 0], 0.05, axis=0),
        np.quantile(actual_cal - pcal[:, :, 2], 0.95, axis=0),
    )
    test, ptest = outputs["test"]
    actual = np.array([s["target"] for s in test])
    profile = np.array([profile_prediction(s["history"]) for s in test])
    score = mae(actual, ptest[:, :, 1])
    coverage = float(
        np.mean(
            (actual >= np.minimum(ptest[:, :, 1], np.maximum(0, ptest[:, :, 0] + lo)))
            & (actual <= np.maximum(ptest[:, :, 1], ptest[:, :, 2] + hi))
        )
    )
    gates = {
        "mae_improved": score < mae(actual, profile),
        "interval_coverage_not_degraded": coverage
        >= control["metrics"]["baseline_interval_coverage"],
        "peak_error_not_degraded": mae(actual.max(axis=1), ptest[:, :, 1].max(axis=1))
        <= mae(actual.max(axis=1), profile.max(axis=1)),
        "calendar_ablation_improved": score < control["metrics"]["test_mae_kwh"]
        and coverage >= control["metrics"]["test_interval_coverage"],
    }
    identifier = uuid.uuid4().hex
    torch.save(model.state_dict(), store.root / "models" / f"{identifier}.pt")
    bundle = {
        **control,
        "id": identifier,
        "created_at": stamp(),
        "kind": "tcn-calendar-household-quantile-v3",
        "future_dimensions": 11,
        "calendar_signature": digest(config.calendars.model_dump(mode="json")),
        "metrics": {
            "test_mae_kwh": score,
            "baseline_mae_kwh": mae(actual, profile),
            "test_interval_coverage": coverage,
            "baseline_interval_coverage": control["metrics"]["baseline_interval_coverage"],
            "cyclic_control_test_mae_kwh": control["metrics"]["test_mae_kwh"],
        },
        "gates": gates,
        "promotion_eligible": all(gates.values()),
        "q05_residual": lo.tolist(),
        "q95_residual": hi.tolist(),
        "limitations": [
            "Matched calendar/household comparison; unavailable calendar knowledge is masked",
            "Hourly coverage does not validate whole-path export confidence",
        ],
    }
    store.insert_document("models", identifier, bundle)
    path.unlink(missing_ok=True)
    if bundle["promotion_eligible"]:
        from .learning import activate_model

        activate_model(store, identifier)
    return {"status": "trained", "model": bundle}
