from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from energy_forecast.battery import policy_search, simulate
from energy_forecast.schemas import TariffRule
from energy_forecast.tariff import boundaries, resolve, validate


def interval(config, hours=1, tariff=None):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(hours=hours)).isoformat(),
        "hours": hours,
        "tariff": tariff or resolve(config, start),
    }


def test_overnight_anchor_and_partial_boundary(config):
    config.tariff.append(
        TariffRule(
            name="free monday",
            start="23:15",
            end="01:30",
            weekdays=[0],
            priority=1,
            import_rate=0,
            grid_charge_allowed=True,
        )
    )
    # Monday local to Tuesday local; Adelaide +10:30 in January.
    assert resolve(config, datetime(2026, 1, 5, 13, tzinfo=timezone.utc))["name"] == "free monday"
    assert resolve(config, datetime(2026, 1, 5, 14, tzinfo=timezone.utc))["name"] == "free monday"
    assert resolve(config, datetime(2026, 1, 5, 15, tzinfo=timezone.utc))["name"] == "default"
    points = boundaries(
        config,
        datetime(2026, 1, 5, 12, tzinfo=timezone.utc),
        datetime(2026, 1, 5, 16, tzinfo=timezone.utc),
    )
    assert datetime(2026, 1, 5, 12, 45, tzinfo=timezone.utc) in points


def test_dst_realizations_and_half_hour_zone(config):
    config.tariff.append(
        TariffRule(name="peak", start="02:15", end="03:30", priority=1, import_rate=0.5)
    )
    start = datetime(2026, 4, 4, 15, tzinfo=timezone.utc)
    end = start + timedelta(hours=5)
    points = boundaries(config, start, end)
    peak_minutes = sum(
        (b - a).total_seconds() / 60
        for a, b in zip(points, points[1:])
        if resolve(config, a)["name"] == "peak"
    )
    assert peak_minutes == 120  # repeated 02:15–03:00 then 02:15–03:30
    config.timezone = "Australia/Darwin"
    assert resolve(config, datetime(2026, 1, 1, 17, tzinfo=timezone.utc))["name"] == "peak"


def test_tariff_ties_rejected(config):
    config.tariff.append(TariffRule(name="tie", import_rate=0.2))
    with pytest.raises(ValueError, match="Ambiguous"):
        validate(config)


def test_stored_energy_efficiency_and_ac_balance(config):
    config.battery.eta_charge = 0.9
    config.battery.eta_discharge = 0.8
    result = simulate(config, [interval(config)], [0], [2], 8)
    assert result["terminal_kwh"][0] == pytest.approx(5.5)
    assert not result["failed"][0]
    result = simulate(config, [interval(config)], [3], [1], 5)
    assert result["terminal_kwh"][0] == pytest.approx(6.8)


def test_baseline_low_power_and_free_duration(config):
    config.battery.discharge_kw = 0.5
    result, power, status = policy_search(config, [interval(config)], [0], [2], 8)
    assert status == "baseline_infeasible"
    assert power == 0
    assert result["paid_import_kwh"][0] == 1.5
    free = resolve(config, datetime(2026, 1, 1, tzinfo=timezone.utc))
    free.update(import_rate=0, grid_charge_allowed=True)
    result = simulate(config, [interval(config, 0.25, free)], [0], [0], 2, conditional=True)
    assert result["terminal_kwh"][0] == pytest.approx(
        2 + config.battery.charge_kw * 0.25 * config.battery.eta_charge
    )


def test_free_charge_intent_and_origin_restriction(config):
    free = interval(config)["tariff"]
    free.update(import_rate=0, grid_charge_allowed=True)
    assert simulate(config, [interval(config, 1, free)], [0], [0], 2)["terminal_kwh"][0] == 2
    prohibited = interval(config)["tariff"]
    prohibited["reexport_grid_energy"] = False
    assert (
        simulate(config, [interval(config, 1, prohibited)], [0], [0], 8, export_kw=2)[
            "battery_export_kwh"
        ][0]
        == 0
    )


def test_export_cap_curtailment_and_shared_power(config):
    tariff = interval(config)["tariff"]
    tariff.update(export_cap_kwh=0.5)
    result = simulate(config, [interval(config, 1, tariff)], [8], [1], 10)
    assert result["natural_export_kwh"][0] == 0.5
    assert result["series"][0]["curtailed_kw"] == 6.5
    config.shared_inverter_kw = 3
    result = simulate(config, [interval(config)], [3], [3], 8, export_kw=2)
    assert result["battery_export_kwh"][0] == 0


def test_joint_policy_one_power_and_terminal_bounds(config):
    intervals = [interval(config), interval(config)]
    result, power, _ = policy_search(config, intervals, [[0, 0], [0, 0]], [[0, 2], [0, 1]], 8)
    assert not np.any(result["failed"])
    assert np.all(result["terminal_kwh"] >= 2)
    assert 0 <= power <= config.battery.discharge_kw


def test_free_grid_charging_shares_headroom_with_pv_and_inverter(config):
    tariff = interval(config)["tariff"]
    tariff.update(import_rate=0, grid_charge_allowed=True)
    config.battery.charge_kw = 5
    config.shared_inverter_kw = 3
    result = simulate(config, [interval(config, 1, tariff)], [1], [0], 2, conditional=True)
    assert result["series"][0]["charge_kw"] == 3
    assert result["series"][0]["grid_import_kw"] == 2
    assert result["series"][0]["grid_export_kw"] == 0
    assert result["terminal_kwh"][0] == pytest.approx(2 + 3 * config.battery.eta_charge)
