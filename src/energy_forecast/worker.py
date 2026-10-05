import json
import sys
from datetime import date

from . import weather
from .forecast import refresh
from .jobs import enqueue, update
from .schemas import Configuration
from .store import Store


def run(directory, identifier):
    store = Store(directory)
    with store.connect() as db:
        row = db.execute("SELECT payload FROM jobs WHERE id=?", (identifier,)).fetchone()
    job = json.loads(row[0])
    config = Configuration.model_validate(store.configuration())
    try:
        kind = job["kind"]
        if kind == "weather":
            result = {"weather_vintage": weather.fetch(config, store)["id"]}
            enqueue(store, "forecast")
        elif kind == "forecast":
            result = {"forecast_id": refresh(store)["forecast_id"]}
        elif kind == "weather_history":
            start, end = (
                date.fromisoformat(job["options"]["start"]),
                date.fromisoformat(job["options"]["end"]),
            )
            result = {
                "weather_vintage": weather.fetch(config, store, (start, end))["id"],
                "evaluation_eligible": False,
            }
        elif kind == "weather_run":
            from .composition import dt

            run = dt(job["options"]["run"])
            available = (
                dt(job["options"]["available_at"]) if job["options"].get("available_at") else None
            )
            result = {
                "weather_vintage": weather.fetch(config, store, run=run, available_at=available)[
                    "id"
                ],
                "evaluation_eligible": available is not None,
            }
        elif kind == "pv_calibrate":
            from .solar_calibration import calibrate_banks

            result = calibrate_banks(store, config)
            enqueue(store, "forecast")
        elif kind == "train":
            from .learning import train

            result = train(store, config, job)
        elif kind in ("bias", "adapt"):
            from .learning import adapt_recent

            result = adapt_recent(store, config, daily=kind == "adapt")
        elif kind == "calibrate":
            from .learning import calibrate

            result = calibrate(store, config)
        elif kind == "export_history":
            import pandas as pd

            from .forecast import canonical

            frames = []
            for mapping in config.mappings:
                if mapping.feature not in ("battery_soc", "export_limit"):
                    frame = pd.DataFrame(canonical(store, config, mapping.feature))
                    frame["feature"] = mapping.feature
                    frames.append(frame)
            if frames:
                directory = store.root / "history"
                directory.mkdir(exist_ok=True)
                pd.concat(frames).to_parquet(directory / "canonical.parquet", index=False)
            result = {"profiles": len(frames)}
        update(store, identifier, status="completed", progress=1, result=result)
    except Exception as error:
        # Exceptions may contain private coordinates/request URLs. Keep them local only.
        update(store, identifier, status="failed", reason=type(error).__name__)
        raise


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
