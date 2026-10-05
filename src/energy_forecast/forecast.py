import copy
import uuid
from collections import defaultdict
from datetime import timedelta
from statistics import median
from zoneinfo import ZoneInfo

import numpy as np

from .battery import policy_search, simulate
from .calendars import features
from .composition import compose, dt
from .schemas import Configuration
from .store import digest, now
from .tariff import boundaries, resolve
from .weather import physical_pv


def canonical(store, config, feature):
    mapping = next((m for m in config.mappings if m.feature == feature), None)
    if mapping is None:
        return []
    # Derived profiles may consume several logical features, but only named sources.
    records = store.observations(None if mapping.mode == "derived" else feature)
    return compose(records, mapping)


def load_profile(store, config, intervals, issue):
    records = canonical(store, config, "household_load")
    groups, broad = defaultdict(list), defaultdict(list)
    zone = ZoneInfo(config.timezone)
    for record in records:
        start, end = dt(record["start"]), dt(record["end"])
        if (
            end > issue
            or record["energy_kwh"] is None
            or record["quality"] not in ("valid", "suspect")
        ):
            continue
        if (issue - end).days > 730:
            continue
        hours = (end - start).total_seconds() / 3600
        if not 0.2 <= hours <= 1.1:
            continue
        local = start.astimezone(zone)
        kw = record["energy_kwh"] / hours
        groups[(local.weekday(), local.hour)].append(kw)
        broad[local.hour].append(kw)
    values = []
    for interval in intervals:
        local = dt(interval["start"]).astimezone(zone)
        samples = groups[(local.weekday(), local.hour)]
        if len(samples) < 2:
            samples = broad[local.hour]
        values.append(median(samples) if len(samples) >= 2 else None)
    return values, len(records)


def state(store, config, feature, issue):
    mapping = next((m for m in config.mappings if m.feature == feature), None)
    if not mapping:
        return None
    sources = {s.source: s for s in mapping.sources}
    valid = []
    for row in store.observations(feature, 1000):
        s = sources.get(row["source"])
        timestamp = dt(row["end"])
        if (
            s
            and row.get("provenance", "live") == "live"
            and row["quality"] == "valid"
            and row["value"] is not None
            and timestamp <= issue
            and (s.valid_from is None or dt(row["start"]) >= s.valid_from)
            and (s.valid_to is None or timestamp <= s.valid_to)
        ):
            valid.append(row)
    return (
        max(valid, key=lambda r: (dt(r["end"]), sources[r["source"]].priority)) if valid else None
    )


def number(value):
    return float(value) if value is not None and np.isfinite(value) else None


def refresh(store):
    payload = store.configuration()
    if not payload:
        raise ValueError("Configure a site first")
    config = Configuration.model_validate(payload)
    issue = now()
    weather = next(
        (
            w
            for w in store.documents("weather", 100)
            if w["kind"] == "live_forecast"
            and w["source_epoch"]
            == digest(
                {"lat": config.latitude, "lon": config.longitude, "model": config.weather_model}
            )
        ),
        None,
    )
    reason = []
    if (
        not weather
        or (issue - dt(weather["received_at"])).total_seconds() > config.weather_freshness_seconds
    ):
        reason.append("weather_missing")
    # Start now, retaining the fractional first/last hour; 48 hours of coverage is required.
    end = issue + timedelta(hours=48)
    weather_rows = (
        [r for r in weather["intervals"] if dt(r["end"]) > issue and dt(r["start"]) < end]
        if weather
        else []
    )
    if weather_rows and (dt(weather_rows[0]["start"]) > issue or dt(weather_rows[-1]["end"]) < end):
        reason.append("weather_horizon_incomplete")
    total_pv, banks, poa = (
        physical_pv(config, weather_rows) if weather_rows else (np.array([]), {}, {})
    )
    load, count = load_profile(store, config, weather_rows, issue)
    load_low, load_high, model_id = None, None, None
    if weather_rows and len(weather_rows) <= 49 and all(v is not None for v in load):
        from .learning import predict_load

        load, load_low, load_high, model_id = predict_load(
            store, config, [dt(r["start"]) for r in weather_rows], load, issue
        )
    if not weather_rows or any(v is None for v in load):
        reason.append("load_history_missing")
    if any(not np.isfinite(v) for v in total_pv):
        reason.append("weather_fields_missing")
    if not config.banks:
        reason.append("pv_geometry_missing")
    soc = state(store, config, "battery_soc", issue)
    if (
        soc is None
        or soc["unit"] != "%"
        or not 0 <= soc["value"] <= 100
        or (issue - dt(soc["end"])).total_seconds() > config.soc_freshness_seconds
    ):
        reason.append("soc_stale")
    if config.mandatory_phase_limits:
        reason.append("phase_limits_unverified")
    dynamic_limit = (
        state(store, config, "export_limit", issue) if config.required_dynamic_limit else None
    )
    if config.required_dynamic_limit:
        if (
            not dynamic_limit
            or dynamic_limit["unit"] not in ("W", "kW")
            or dynamic_limit["value"] < 0
            or (issue - dt(dynamic_limit["end"])).total_seconds() > config.soc_freshness_seconds
        ):
            reason.append("export_limit_missing")
        else:
            limit = dynamic_limit["value"] / (1000 if dynamic_limit["unit"] == "W" else 1)
            config = config.model_copy(
                update={"export_limit_kw": min(config.export_limit_kw, limit)}
            )
    if config.terminal_reserve_kwh is None:
        reason.append("terminal_obligation_unbounded")
    rows, sim_intervals, sim_pv, sim_load = [], [], [], []
    origin_indices = []
    for i, weather_row in enumerate(weather_rows):
        start, stop = max(issue, dt(weather_row["start"])), min(end, dt(weather_row["end"]))
        points = set(boundaries(config, start, stop))
        tick = start.replace(minute=(start.minute // 15) * 15, second=0, microsecond=0) + timedelta(
            minutes=15
        )
        while tick < stop:
            points.add(tick)
            tick += timedelta(minutes=15)
        points = sorted(points)
        hours = (stop - start).total_seconds() / 3600
        rows.append(
            {
                "start": start.isoformat(),
                "end": stop.isoformat(),
                "pv_kw": number(total_pv[i]),
                "load_kw": load[i],
                "pv_kwh": number(total_pv[i] * hours),
                "load_kwh": number(load[i] * hours) if load[i] is not None else None,
                "banks_kw": {key: number(value[i]) for key, value in banks.items()},
                "poa_w_m2": {key: number(value[i]) for key, value in poa.items()},
                "ghi_w_m2": weather_row["shortwave_radiation"],
                "dni_w_m2": weather_row["direct_normal_irradiance"],
                "dhi_w_m2": weather_row["diffuse_radiation"],
                "temperature_c": weather_row["temperature_2m"],
                "calendar": features(config, start, issue),
                "pv_p05_kw": None,
                "pv_p95_kw": None,
                "load_p05_kw": load_low[i] if load_low else None,
                "load_p95_kw": load_high[i] if load_high else None,
            }
        )
        for first, last in zip(points, points[1:]):
            sim_intervals.append(
                {
                    "start": first.isoformat(),
                    "end": last.isoformat(),
                    "hours": (last - first).total_seconds() / 3600,
                    "tariff": resolve(config, first),
                }
            )
            origin_indices.append(i)
            sim_pv.append(number(total_pv[i]))
            sim_load.append(load[i])
    status = "warming_up" if reason else "shadow"
    plan = {
        "safe_battery_export_remaining_kwh": 0.0,
        "safe_battery_export_now_kwh": 0.0,
        "advisory_export_power_kw": 0.0,
        "target_path_confidence": config.target_confidence,
        "estimated_path_confidence": None,
        "required_reserve_kwh": config.battery.reserve_kwh,
        "required_reserve_soc_pct": config.battery.reserve_kwh / config.battery.capacity_kwh * 100,
        "source_soc_at": soc["end"] if soc else None,
        "source_soc_pct": soc["value"] if soc else None,
        "replacement_budget": True,
        "conditional_on_free_charge": False,
        "reason_codes": reason + ["insufficient_tail_validation"],
        "schedule": [],
        "shadow_candidate": None,
    }
    simulation_blockers = {
        "weather_missing",
        "weather_horizon_incomplete",
        "load_history_missing",
        "weather_fields_missing",
        "soc_stale",
        "pv_geometry_missing",
        "export_limit_missing",
    }
    if not simulation_blockers.intersection(reason):
        initial = soc["value"] / 100 * config.battery.capacity_kwh
        if not config.battery.reserve_kwh <= initial <= config.battery.upper_kwh:
            reason.append("soc_outside_bounds")
            plan["reason_codes"].append("soc_outside_bounds")
        else:
            result, power, planner_status = policy_search(
                config, sim_intervals, sim_pv, sim_load, initial
            )
            plan["schedule"] = result["series"]
            plan["shadow_candidate"] = {
                "battery_export_kwh": float(result["battery_export_kwh"][0]),
                "policy_power_kw": power,
                "natural_pv_export_kwh": float(result["natural_export_kwh"][0]),
                "total_site_export_kwh": float(
                    result["natural_export_kwh"][0] + result["battery_export_kwh"][0]
                ),
                "paid_import_shortfall_kwh": float(result["paid_import_kwh"][0]),
                "validated": False,
            }
            if planner_status == "baseline_infeasible":
                status = planner_status
                plan["reason_codes"].append(planner_status)
            elif "terminal_obligation_unbounded" in reason:
                status = "horizon_insufficient"
            # Deterministic stresses are diagnostics, never confidence estimates.
            stress = simulate(
                config,
                sim_intervals,
                np.array(sim_pv) * 0.5,
                np.array(sim_load) * 1.5,
                initial,
                power,
            )
            plan["stress_failed"] = bool(stress["failed"][0])
            if any(r.grid_charge_allowed for r in config.tariff) and not config.grid_charge_intent:
                conditional, cpower, cstatus = policy_search(
                    config, sim_intervals, sim_pv, sim_load, initial, True
                )
                plan["conditional_free_charge"] = {
                    "battery_export_kwh": float(conditional["battery_export_kwh"][0]),
                    "policy_power_kw": cpower,
                    "status": cstatus,
                    "validated": False,
                    "assumption": "HA must explicitly execute permitted charging",
                }
    totals = {
        "pv_48h_kwh": sum(r["pv_kwh"] for r in rows)
        if rows and all(r["pv_kwh"] is not None for r in rows)
        else None,
        "load_48h_kwh": sum(r["load_kwh"] for r in rows)
        if rows and all(r["load_kwh"] is not None for r in rows)
        else None,
    }
    calibration = store.meta("calibration") or {}
    residual_file = store.root / "models" / "paired-residuals.npz"
    model_versions = {
        "pv": "physical-v1",
        "load": model_id or "weekday-profile-v1",
        "uncertainty": model_id,
        "adapter": (store.meta("recent_bias") or {}).get("id") if model_id else None,
    }
    if (
        not simulation_blockers.intersection(reason)
        and "soc_outside_bounds" not in reason
        and calibration.get("complete_nonoverlapping_paths", 0) >= 30
        and calibration.get("model_signature") == digest(model_versions)
        and calibration.get("configuration_hash") == digest(payload)
        and residual_file.exists()
    ):
        from scipy.stats import beta

        from .learning import paired_scenarios

        blocks = np.load(residual_file)["blocks"]
        if blocks.shape[1:] == (len(rows), 2):
            scenarios_pv, scenarios_load = paired_scenarios(
                total_pv, load, blocks, config.scenario_count, seed=42
            )
            scenario_result, scenario_power, scenario_status = policy_search(
                config,
                sim_intervals,
                scenarios_pv[:, origin_indices],
                scenarios_load[:, origin_indices],
                initial,
            )
            verifier_pv, verifier_load = paired_scenarios(
                total_pv, load, blocks, config.scenario_count, seed=718
            )
            verification = simulate(
                config,
                sim_intervals,
                verifier_pv[:, origin_indices],
                verifier_load[:, origin_indices],
                initial,
                scenario_power,
            )
            failures = int(verification["failed"].sum())
            upper = (
                float(beta.ppf(0.95, failures + 1, config.scenario_count - failures))
                if failures < config.scenario_count
                else 1.0
            )
            plan["scenario_diagnostics"] = {
                "count": config.scenario_count,
                "calibration_blocks": len(blocks),
                "verifier_failures": failures,
                "sampled_failure_upper_95": upper,
                "operating_path_confidence_validated": False,
                "limitation": "Bootstrap scenario error is not independent real-world tail validation",
                "status": scenario_status,
                "candidate_export_kwh": float(np.min(scenario_result["battery_export_kwh"])),
                "candidate_policy_power_kw": scenario_power,
            }
            weights = np.array(
                [(dt(r["end"]) - dt(r["start"])).total_seconds() / 3600 for r in rows]
            )
            for feature, values in (("pv", scenarios_pv), ("load", scenarios_load)):
                for quantile, label in ((0.05, "p05"), (0.5, "p50"), (0.95, "p95")):
                    totals[f"{feature}_48h_{label}_kwh"] = float(
                        np.quantile(values @ weights, quantile)
                    )
            pv_low, pv_high = np.quantile(scenarios_pv, [0.05, 0.95], axis=0)
            load_low, load_high = np.quantile(scenarios_load, [0.05, 0.95], axis=0)
            for i, row in enumerate(rows):
                row.update(
                    pv_p05_kw=float(pv_low[i]),
                    pv_p95_kw=float(pv_high[i]),
                    load_p05_kw=float(load_low[i]),
                    load_p95_kw=float(load_high[i]),
                )
    for feature, power in (("pv", "pv_kw"), ("load", "load_kw")):
        deadline = issue + timedelta(hours=24)
        selected = [r for r in rows if dt(r["start"]) < deadline]
        totals[f"{feature}_24h_kwh"] = (
            sum(
                r[power] * (min(dt(r["end"]), deadline) - dt(r["start"])).total_seconds() / 3600
                for r in selected
            )
            if selected and all(r[power] is not None for r in selected)
            else None
        )
    expiry = issue + timedelta(seconds=config.lease_seconds)
    if soc:
        expiry = min(expiry, dt(soc["end"]) + timedelta(seconds=config.soc_freshness_seconds))
    if weather:
        expiry = min(
            expiry, dt(weather["received_at"]) + timedelta(seconds=config.weather_freshness_seconds)
        )
    identifier = uuid.uuid4().hex
    snapshot = {
        "schema_version": "1.0",
        "forecast_id": identifier,
        "status": status,
        "issued_at": issue.isoformat(),
        "valid_until": expiry.isoformat(),
        "configuration_hash": digest(payload),
        "configuration_revision": payload["revision"],
        "weather_vintage": weather["id"] if weather else None,
        "input_watermark": issue.isoformat(),
        "model_versions": model_versions,
        "resolution_minutes": 60,
        "horizon_hours": 48,
        "units": {"power": "kW AC", "energy": "kWh", "soc": "%"},
        "assumptions": [
            "Uncalibrated baseline; shadow simulation only",
            "Bank allocation is physical unless bank labels are validated",
            "Aggregate AC boundary",
        ],
        "series": rows,
        "totals": totals,
        "export_plan": plan,
        "history_intervals": count,
    }
    store.insert_document("forecasts", identifier, snapshot, digest(payload))
    return snapshot


def leased(store, snapshot, clock=None):
    clock = clock or now()
    if snapshot is None:
        return {
            "schema_version": "1.0",
            "status": "warming_up",
            "forecast_id": None,
            "export_plan": {
                "safe_battery_export_remaining_kwh": 0,
                "advisory_export_power_kw": 0,
                "reason_codes": ["forecast_missing"],
            },
        }
    value = copy.deepcopy(snapshot)
    reasons = []
    if dt(value["valid_until"]) <= clock:
        reasons.append("lease_expired")
    if store.meta("invalidated_at") and dt(store.meta("invalidated_at")) >= dt(value["issued_at"]):
        reasons.append("inputs_or_configuration_changed")
    config = store.configuration()
    if config is None or digest(config) != value["configuration_hash"]:
        reasons.append("configuration_changed")
    if reasons:
        value["status"] = "stale"
        value["export_plan"]["reason_codes"] = sorted(
            set(value["export_plan"]["reason_codes"] + reasons)
        )
        for key in (
            "safe_battery_export_remaining_kwh",
            "safe_battery_export_now_kwh",
            "advisory_export_power_kw",
        ):
            value["export_plan"][key] = 0.0
    return value
