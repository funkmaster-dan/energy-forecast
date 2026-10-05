"""Export pinned v1 contracts without reading installation data."""

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from energy_forecast.api import create_app
from energy_forecast.schemas import Batch, Configuration, ForecastResponse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integration", type=Path)
    args = parser.parse_args()
    directory = Path(__file__).resolve().parents[1] / "contracts"
    directory.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="energy-contract-") as temporary:
        schemas = {
            "openapi-v1": create_app(temporary, False).openapi(),
            "configuration-v1": Configuration.model_json_schema(),
            "observations-v1": Batch.model_json_schema(),
            "forecast-v1": ForecastResponse.model_json_schema(),
        }
        for name, document in schemas.items():
            (directory / f"{name}.json").write_text(json.dumps(document, indent=2) + "\n")
    fixture = {
        "schema_version": "1.0",
        "forecast_id": "synthetic-contract-1",
        "status": "shadow",
        "issued_at": "2026-10-05T00:00:00Z",
        "valid_until": "2026-10-05T00:01:00Z",
        "configuration_hash": "synthetic-only",
        "configuration_revision": 1,
        "model_versions": {"pv": "physical-v1", "load": "weekday-profile-v1", "uncertainty": None},
        "totals": {"pv_24h_kwh": 10, "pv_48h_kwh": 20, "load_24h_kwh": 8, "load_48h_kwh": 16},
        "series": [],
        "export_plan": {
            "safe_battery_export_remaining_kwh": 0,
            "safe_battery_export_now_kwh": 0,
            "advisory_export_power_kw": 0,
            "target_path_confidence": 0.99,
            "estimated_path_confidence": None,
            "required_reserve_kwh": 2,
            "required_reserve_soc_pct": 20,
            "source_soc_at": "2026-10-04T23:59:50Z",
            "source_soc_pct": 80,
            "replacement_budget": True,
            "reason_codes": ["insufficient_tail_validation"],
            "schedule": [],
        },
    }
    fixture = ForecastResponse.model_validate(fixture).model_dump(mode="json")
    (directory / "synthetic-forecast-v1.json").write_text(json.dumps(fixture, indent=2) + "\n")
    if args.integration:
        args.integration.mkdir(parents=True, exist_ok=True)
        files = ["forecast-v1.json", "observations-v1.json", "synthetic-forecast-v1.json"]
        for file in files:
            shutil.copyfile(directory / file, args.integration / file)
        (args.integration / "source.json").write_text(
            json.dumps(
                {
                    "source": "https://github.com/funkmaster-dan/energy-forecast",
                    "api": "1.0",
                    "sha256": {
                        file: hashlib.sha256((directory / file).read_bytes()).hexdigest()
                        for file in files
                    },
                },
                indent=2,
            )
            + "\n"
        )


if __name__ == "__main__":
    main()
