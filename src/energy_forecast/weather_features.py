"""Issue-time weather features for the load model.

Only a forecast received by the issue time, or an archive with documented
availability by then, can enter chronological evaluation. Reanalysis is never
used to validate an operational weather-aware candidate.
"""

from datetime import timedelta

import numpy as np

from .composition import dt
from .store import digest


def source_epoch(config):
    return digest({"lat": config.latitude, "lon": config.longitude, "model": config.weather_model})


def features(rows):
    """Fixed physical scaling avoids fitting transforms on future targets."""
    if any(
        row.get("temperature_2m") is None or row.get("shortwave_radiation") is None for row in rows
    ):
        return None
    values = np.array(
        [[row["temperature_2m"], row["shortwave_radiation"]] for row in rows],
        dtype=np.float32,
    )
    if not np.all(np.isfinite(values)) or np.any(values[:, 1] < 0):
        return None
    return np.stack(
        [np.clip((values[:, 0] - 20) / 15, -4, 4), np.clip(values[:, 1] / 1000, 0, 2)],
        axis=1,
    )


def for_issue(store, config, issue, hours=49):
    """Return one full hourly vintage known at issue time, or None."""
    with store.connect() as db:
        found = db.execute(
            """
            SELECT payload FROM weather
            WHERE json_extract(payload,'$.source_epoch')=?
              AND ((json_extract(payload,'$.kind')='live_forecast' AND json_extract(payload,'$.received_at')<=?)
                OR (json_extract(payload,'$.kind')='single_run_archive'
                    AND json_extract(payload,'$.provider_available_at') IS NOT NULL
                    AND json_extract(payload,'$.provider_available_at')<=?))
            ORDER BY COALESCE(json_extract(payload,'$.provider_available_at'),json_extract(payload,'$.received_at'),received) DESC
            LIMIT 12
            """,
            (source_epoch(config), issue.isoformat(), issue.isoformat()),
        ).fetchall()
    import json

    for record in found:
        vintage = json.loads(record[0])
        available = dt(vintage.get("provider_available_at") or vintage["received_at"])
        max_age = (
            config.weather_freshness_seconds if vintage["kind"] == "live_forecast" else 8 * 3600
        )
        if not timedelta(0) <= issue - available <= timedelta(seconds=max_age):
            continue
        by_start = {dt(row["start"]): row for row in vintage["intervals"]}
        times = [issue + timedelta(hours=i) for i in range(hours)]
        if all(moment in by_start for moment in times):
            rows = [by_start[moment] for moment in times]
            if features(rows) is not None:
                return vintage, rows
    return None
