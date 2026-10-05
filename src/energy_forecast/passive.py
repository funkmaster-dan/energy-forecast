"""Passive independent energy measurements; never schedules a battery test cycle."""

import numpy as np


def estimate_efficiency(intervals, independent_stored_energy=False, seed=42):
    if not independent_stored_energy:
        return {
            "status": "underdetermined",
            "reason": "Stored-energy changes must be independent of the same SoC/capacity formula",
            "charge_efficiency": None,
            "discharge_efficiency": None,
        }
    accepted = {"charge": [], "discharge": []}
    rejected = []
    for i, row in enumerate(intervals):
        if (
            row.get("quality") != "valid"
            or row.get("coverage", 0) < 0.95
            or row.get("reset")
            or row.get("balancing")
        ):
            rejected.append({"index": i, "reason": "quality_or_balancing"})
            continue
        change = row["stored_end_kwh"] - row["stored_start_kwh"] + row.get("standby_kwh", 0)
        charge, discharge = row["charge_ac_kwh"], row["discharge_ac_kwh"]
        if charge > 0.1 and discharge < 0.01 and change > 0.05:
            efficiency, direction = change / charge, "charge"
        elif discharge > 0.1 and charge < 0.01 and change < -0.05:
            efficiency, direction = discharge / -change, "discharge"
        else:
            rejected.append({"index": i, "reason": "insufficient_excursion_or_simultaneous_flow"})
            continue
        if 0.5 <= efficiency <= 1:
            accepted[direction].append(efficiency)
        else:
            rejected.append({"index": i, "reason": "inconsistent_energy_boundary"})
    rng = np.random.default_rng(seed)
    results = {}
    for direction, values in accepted.items():
        if len(values) < 20:
            results[direction] = {"status": "insufficient_data", "count": len(values)}
            continue
        values = np.array(values)
        bootstrap = np.median(rng.choice(values, (1000, len(values))), axis=1)
        results[direction] = {
            "status": "candidate",
            "estimate": float(np.median(values)),
            "confidence_95": np.quantile(bootstrap, [0.025, 0.975]).tolist(),
            "count": len(values),
            "limitation": "Bootstrap assumes independent fitting intervals; validate sensor errors separately",
        }
    return {
        "status": "candidate"
        if all(v["status"] == "candidate" for v in results.values())
        else "insufficient_data",
        "directions": results,
        "rejected": rejected,
        "auto_applied": False,
    }


def calibrate_battery(store, config):
    from .composition import dt
    from .forecast import canonical

    if not config.battery.independent_stored_energy:
        return estimate_efficiency([], False)
    mapping = next((m for m in config.mappings if m.feature == "battery_stored_energy"), None)
    if not mapping:
        return {"status": "insufficient_data", "reason": "Map independent battery_stored_energy"}
    records = [
        r
        for r in store.observations("battery_stored_energy")
        if r["source"] in {s.source for s in mapping.sources}
        and r["quality"] == "valid"
        and r["boundary"] == "stored"
        and r["unit"] == "kWh"
        and r["kind"] == "state"
        and r["value"] is not None
    ]
    if not records:
        return {"status": "insufficient_data", "reason": "Independent stored energy is unavailable"}
    charge = {(r["start"], r["end"]): r for r in canonical(store, config, "battery_charge")}
    discharge = {(r["start"], r["end"]): r for r in canonical(store, config, "battery_discharge")}
    intervals = []
    for key in sorted(charge.keys() & discharge.keys()):
        c, d = charge[key], discharge[key]
        if c["energy_kwh"] is None or d["energy_kwh"] is None:
            continue
        first, last = map(dt, key)
        a = min(records, key=lambda r: abs((dt(r["end"]) - first).total_seconds()))
        b = min(records, key=lambda r: abs((dt(r["end"]) - last).total_seconds()))
        if (
            abs((dt(a["end"]) - first).total_seconds()) > 60
            or abs((dt(b["end"]) - last).total_seconds()) > 60
            or a["epoch"] != b["epoch"]
        ):
            continue
        intervals.append(
            {
                "quality": "valid" if c["quality"] == d["quality"] == "valid" else "suspect",
                "coverage": 1,
                "stored_start_kwh": a["value"],
                "stored_end_kwh": b["value"],
                "charge_ac_kwh": c["energy_kwh"],
                "discharge_ac_kwh": d["energy_kwh"],
                "standby_kwh": config.battery.standby_kw * (last - first).total_seconds() / 3600,
            }
        )
    return estimate_efficiency(intervals, True)
