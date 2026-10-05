"""Causal household-first dispatch. AC conservation and stored-energy losses."""

import numpy as np


def simulate(config, intervals, pv, load, initial_kwh, export_kw=0.0, conditional=False):
    pv, load = np.atleast_2d(pv).astype(float), np.atleast_2d(load).astype(float)
    if pv.shape != load.shape or pv.shape[1] != len(intervals):
        raise ValueError("Trajectory dimensions disagree")
    if (
        not np.all(np.isfinite(pv))
        or not np.all(np.isfinite(load))
        or np.any(pv < 0)
        or np.any(load < 0)
    ):
        raise ValueError("Trajectories require finite nonnegative AC power")
    b = config.battery
    energy = np.full(pv.shape[0], float(initial_kwh))
    if not b.reserve_kwh <= initial_kwh <= b.upper_kwh:
        raise ValueError("Initial energy outside configured bounds")
    failed = np.zeros(pv.shape[0], dtype=bool)
    paid = np.zeros_like(energy)
    extra = np.zeros_like(energy)
    natural = np.zeros_like(energy)
    caps = {}
    rows = []
    # Origin tracking is conservative: initial stored energy is unverified when restricted.
    solar_energy = np.zeros_like(energy)
    for i, interval in enumerate(intervals):
        dt = interval["hours"]
        tariff = interval["tariff"]
        energy -= b.standby_kw * dt
        solar_energy = np.maximum(0, solar_energy - b.standby_kw * dt)
        failed |= energy < b.reserve_kwh - 1e-8
        e_available = np.maximum(0, energy - b.reserve_kwh)
        deficit = np.maximum(0, load[:, i] - pv[:, i])
        surplus = np.maximum(0, pv[:, i] - load[:, i])
        shared = max(0, (config.shared_inverter_kw or 1e9))
        discharge_limit = np.minimum(b.discharge_kw, np.maximum(0, shared - pv[:, i]))
        discharge = np.minimum(
            deficit, np.minimum(discharge_limit, e_available * b.eta_discharge / dt)
        )
        import_power = deficit - discharge
        export_limit = min(
            config.export_limit_kw, tariff["export_kw"] if tariff["export_kw"] is not None else 1e9
        )
        if not tariff["export_allowed"]:
            export_limit = 0
        cap_key = tariff["window_id"]
        used = caps.setdefault(cap_key, np.zeros_like(energy))
        if tariff["export_cap_kwh"] is not None:
            export_headroom = np.minimum(
                export_limit, np.maximum(0, tariff["export_cap_kwh"] - used) / dt
            )
        else:
            export_headroom = np.full_like(energy, export_limit)
        taper = np.clip(
            (b.upper_kwh - energy) / max(0.01, b.upper_kwh * (1 - b.taper_start_pct / 100)), 0, 1
        )
        charge_limit = b.charge_kw * taper
        charge = np.minimum(
            surplus,
            np.minimum(charge_limit, np.maximum(0, b.upper_kwh - energy) / b.eta_charge / dt),
        )
        # No charge/discharge simultaneously, and free grid import shares site import limit.
        free = tariff["import_rate"] == 0
        import_limit = min(
            config.import_limit_kw, tariff["import_kw"] if tariff["import_kw"] is not None else 1e9
        )
        grid_charge = np.zeros_like(energy)
        if free and tariff["grid_charge_allowed"] and (config.grid_charge_intent or conditional):
            grid_charge = np.minimum(
                np.maximum(0, charge_limit - charge),
                np.maximum(0, b.upper_kwh - energy - charge * dt * b.eta_charge)
                / b.eta_charge
                / dt,
            )
            grid_charge = np.minimum(grid_charge, np.maximum(0, import_limit - import_power))
            grid_charge = np.where((discharge <= 1e-9) & (surplus <= 1e-9), grid_charge, 0)
        base_export = np.minimum(np.maximum(0, surplus - charge), export_headroom)
        can_export = (
            (deficit <= 1e-9) & (charge <= 1e-9) & (grid_charge <= 1e-9) & (import_power <= 1e-9)
        )
        target = export_kw if tariff["export_allowed"] and tariff["export_rate"] > 0 else 0.0
        discretionary = np.minimum(
            target,
            np.minimum(
                np.maximum(0, discharge_limit - discharge),
                np.maximum(0, e_available * b.eta_discharge / dt - discharge),
            ),
        )
        discretionary = np.minimum(discretionary, np.maximum(0, export_headroom - base_export))
        if not tariff["reexport_grid_energy"]:
            discretionary = np.minimum(discretionary, solar_energy * b.eta_discharge / dt)
        discretionary = np.where(can_export, discretionary, 0)
        discharge += discretionary
        solar_energy = (
            np.maximum(0, solar_energy - discharge * dt / b.eta_discharge)
            + charge * dt * b.eta_charge
        )
        energy += (charge + grid_charge) * dt * b.eta_charge - discharge * dt / b.eta_discharge
        export_power = base_export + discretionary
        # Curtailed solar balances the AC bus when the site export cap binds.
        curtailed = np.maximum(0, surplus - charge - base_export)
        residual = (
            pv[:, i]
            - curtailed
            + import_power
            + grid_charge
            + discharge
            - load[:, i]
            - export_power
            - charge
            - grid_charge
        )
        if np.max(np.abs(residual)) > 1e-7:
            raise RuntimeError("AC conservation failed")
        failed |= (
            (energy < b.reserve_kwh - 1e-8)
            | (energy > b.upper_kwh + 1e-8)
            | (import_power + grid_charge > import_limit + 1e-8)
        )
        if not free:
            paid += import_power * dt
        failed |= paid > config.paid_import_tolerance_kwh + 1e-8
        extra += discretionary * dt
        natural += base_export * dt
        used += export_power * dt
        rows.append(
            {
                "start": interval["start"],
                "end": interval["end"],
                "stored_kwh": float(energy[0]),
                "soc_pct": float(energy[0] / b.capacity_kwh * 100),
                "charge_kw": float(charge[0] + grid_charge[0]),
                "discharge_kw": float(discharge[0]),
                "grid_import_kw": float(import_power[0] + grid_charge[0]),
                "grid_export_kw": float(export_power[0]),
                "battery_export_kw": float(discretionary[0]),
                "curtailed_kw": float(curtailed[0]),
                "tariff": tariff,
            }
        )
    if config.terminal_reserve_kwh is not None:
        failed |= energy < config.terminal_reserve_kwh - 1e-8
    return {
        "failed": failed,
        "paid_import_kwh": paid,
        "battery_export_kwh": extra,
        "natural_export_kwh": natural,
        "terminal_kwh": energy,
        "series": rows,
    }


def policy_search(config, intervals, pv, load, initial_kwh, conditional=False):
    baseline = simulate(config, intervals, pv, load, initial_kwh, conditional=conditional)
    if np.any(baseline["failed"]):
        return baseline, 0.0, "baseline_infeasible"
    best, power = baseline, 0.0
    # Bounded common causal policy templates; no perfect-foresight scenario dispatch.
    for candidate in np.linspace(0, config.battery.discharge_kw, 25)[1:]:
        outcome = simulate(config, intervals, pv, load, initial_kwh, float(candidate), conditional)
        if (
            not np.any(outcome["failed"])
            and np.min(outcome["battery_export_kwh"]) > np.min(best["battery_export_kwh"]) + 1e-9
        ):
            best, power = outcome, float(candidate)
    return best, power, "shadow"
