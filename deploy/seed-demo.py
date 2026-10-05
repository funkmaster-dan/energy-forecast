#!/usr/bin/env python3
"""Synthetic data only. Load into a dedicated demo volume, never a household dataset."""

import argparse
import math
from datetime import datetime, timedelta, timezone

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--password", required=True)
    parser.add_argument("--setup-key", default="")
    parser.add_argument("--days", type=int, default=120)
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=120) as client:
        if args.setup_key:
            response = client.post(
                "/auth/setup", json={"password": args.password, "setup_key": args.setup_key}
            )
            response.raise_for_status()
        login = client.post("/auth/login", json={"password": args.password})
        login.raise_for_status()
        client.headers["X-CSRF-Token"] = login.json()["csrf"]
        config = {
            "name": "Synthetic demonstration",
            "revision": 0,
            "latitude": -35,
            "longitude": 139,
            "timezone": "Australia/Adelaide",
            "banks": [{"id": "north", "dc_kwp": 5, "tilt": 25, "azimuth": 0}],
            "battery": {
                "capacity_kwh": 15,
                "upper_kwh": 15,
                "reserve_kwh": 2,
                "charge_kw": 5,
                "discharge_kw": 5,
            },
            "terminal_reserve_kwh": 3,
            "mappings": [
                {"feature": feature, "sources": [{"source": source}]}
                for feature, source in [
                    ("household_load", "sensor.demo_load"),
                    ("pv_generation", "sensor.demo_pv"),
                    ("battery_soc", "sensor.demo_soc"),
                ]
            ],
            "tariff": [
                {"name": "Default", "import_rate": 0.30, "export_rate": 0.05},
                {
                    "name": "Evening",
                    "start": "16:00",
                    "end": "21:00",
                    "priority": 1,
                    "import_rate": 0.40,
                    "export_rate": 0.25,
                },
                {
                    "name": "Free midday",
                    "start": "11:00",
                    "end": "14:00",
                    "priority": 1,
                    "import_rate": 0,
                    "export_rate": 0.05,
                    "grid_charge_allowed": True,
                },
            ],
        }
        existing = client.get("/v1/sites/home/configuration").json()
        if existing:
            raise SystemExit(
                "Refusing to seed an already configured instance. Use a separate empty demo volume."
            )
        response = client.put("/v1/sites/home/configuration", json=config)
        response.raise_for_status()
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        records = []
        for i in range(args.days * 24):
            start = end - timedelta(hours=args.days * 24 - i)
            local_hour = (start.hour + 10.5) % 24
            load = (
                0.35
                + 0.8 * math.exp(-(((local_hour - 19) / 2) ** 2))
                + 0.25 * math.exp(-(((local_hour - 7) / 1.5) ** 2))
            )
            pv = (
                max(0, 4 * math.sin((local_hour - 6) / 12 * math.pi))
                if 6 <= local_hour <= 18
                else 0
            )
            for feature, source, value in [
                ("household_load", "sensor.demo_load", load),
                ("pv_generation", "sensor.demo_pv", pv),
            ]:
                records.append(
                    {
                        "feature": feature,
                        "source": source,
                        "epoch": "synthetic-v1",
                        "start": start.isoformat(),
                        "end": (start + timedelta(hours=1)).isoformat(),
                        "value": value,
                        "unit": "kW",
                        "kind": "mean_power",
                        "boundary": "AC",
                    }
                )
        for i in range(0, len(records), 2000):
            response = client.post(
                "/v1/sites/home/observations",
                json={
                    "batch_id": f"synthetic-{end.isoformat()}-{i}",
                    "observations": records[i : i + 2000],
                },
            )
            response.raise_for_status()
        current = datetime.now(timezone.utc)
        response = client.post(
            "/v1/sites/home/observations",
            json={
                "batch_id": "demo-soc-" + current.isoformat(),
                "observations": [
                    {
                        "feature": "battery_soc",
                        "source": "sensor.demo_soc",
                        "epoch": "synthetic-v1",
                        "start": (current - timedelta(seconds=1)).isoformat(),
                        "end": current.isoformat(),
                        "value": 85,
                        "unit": "%",
                        "kind": "state",
                        "boundary": "stored",
                    }
                ],
            },
        )
        response.raise_for_status()
        for kind in ("weather", "forecast"):
            response = client.post("/v1/sites/home/jobs", json={"kind": kind})
            response.raise_for_status()
        print(
            "Synthetic history loaded; weather and forecast jobs queued. This instance is demonstration only."
        )


if __name__ == "__main__":
    main()
