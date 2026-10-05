"""Only public location queries leave the household. Archive every receipt."""

import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pandas as pd
import pvlib

from .store import digest, now

FIELDS = "temperature_2m,shortwave_radiation,direct_normal_irradiance,diffuse_radiation,cloud_cover"


def fetch(config, store, historical=None, run=None, available_at=None):
    run = run.astimezone(UTC) if run else None
    available_at = available_at.astimezone(UTC) if available_at else None
    params = {
        "latitude": config.latitude,
        "longitude": config.longitude,
        "hourly": FIELDS,
        "timezone": "UTC",
        "timeformat": "unixtime",
        "models": config.weather_model,
    }
    url = "https://api.open-meteo.com/v1/forecast"
    kind = "live_forecast"
    if run:
        if config.weather_model == "best_match":
            raise ValueError("Single-run imports require an explicit weather model")
        params.update(run=run.astimezone(UTC).strftime("%Y-%m-%dT%H:%M"), forecast_days=4)
        url = "https://single-runs-api.open-meteo.com/v1/forecast"
        kind = "single_run_archive"
    elif historical:
        start, end = historical
        if (end - start).days > 31 or end < start:
            raise ValueError("Weather history pages must be 0–31 days")
        params.update(start_date=start.isoformat(), end_date=end.isoformat())
        url = "https://archive-api.open-meteo.com/v1/archive"
        kind = "reanalysis_pretraining_only"
    else:
        params["forecast_days"] = 4
    response = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=20) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
            break
        except httpx.HTTPError:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    raw = response.json()
    hourly = raw["hourly"]
    intervals = []
    for i, epoch in enumerate(hourly["time"]):
        end = datetime.fromtimestamp(epoch, UTC)
        # Radiation is a preceding-hour mean. Temperature is sampled at the end label.
        intervals.append(
            {
                "start": (end - timedelta(hours=1)).isoformat(),
                "end": end.isoformat(),
                **{field: hourly[field][i] for field in FIELDS.split(",")},
            }
        )
    document = {
        "id": uuid.uuid4().hex,
        "received_at": now().isoformat(),
        "provider": "open-meteo",
        "kind": kind,
        "provider_run_at": run.isoformat() if run else None,
        "provider_available_at": available_at.isoformat() if available_at else None,
        "model": config.weather_model,
        "source_epoch": digest(
            {"lat": config.latitude, "lon": config.longitude, "model": config.weather_model}
        ),
        "units": raw["hourly_units"],
        "intervals": intervals,
        "raw": raw,
    }
    store.insert_document(
        "weather", document["id"], document, digest(config.model_dump(mode="json"))
    )
    if not historical and not run:
        store.meta("invalidated_at", now().isoformat())
    return document


def physical_pv(config, intervals):
    times = pd.DatetimeIndex(
        [
            pd.Timestamp(r["start"]) + (pd.Timestamp(r["end"]) - pd.Timestamp(r["start"])) / 2
            for r in intervals
        ]
    )
    solar = pvlib.solarposition.get_solarposition(times, config.latitude, config.longitude)
    ghi = np.array(
        [
            r["shortwave_radiation"] if r["shortwave_radiation"] is not None else np.nan
            for r in intervals
        ]
    )
    dni = np.array(
        [
            r["direct_normal_irradiance"] if r["direct_normal_irradiance"] is not None else np.nan
            for r in intervals
        ]
    )
    dhi = np.array(
        [
            r["diffuse_radiation"] if r["diffuse_radiation"] is not None else np.nan
            for r in intervals
        ]
    )
    temp = np.array(
        [r["temperature_2m"] if r["temperature_2m"] is not None else np.nan for r in intervals]
    )
    banks, poa, groups = {}, {}, {}
    for bank in config.banks:
        irradiance = pvlib.irradiance.get_total_irradiance(
            bank.tilt, bank.azimuth, solar.apparent_zenith, solar.azimuth, dni, ghi, dhi
        )["poa_global"].to_numpy()
        irradiance = np.maximum(0, irradiance)
        # Missing weather stays missing even if the solar geometry says night.
        irradiance = np.where(
            (solar.apparent_elevation.to_numpy() <= 0)
            & np.isfinite(ghi)
            & np.isfinite(dni)
            & np.isfinite(dhi),
            0,
            irradiance,
        )
        cell_temp = temp + irradiance * 0.025
        power = (
            bank.dc_kwp
            * irradiance
            / 1000
            * np.maximum(0, 1 - 0.004 * (cell_temp - 25))
            * bank.conversion_efficiency
        )
        banks[bank.id], poa[bank.id] = power, irradiance
        groups.setdefault(bank.inverter_group, []).append(bank.id)
    for group, members in groups.items():
        total = sum(banks[key] for key in members)
        factor = np.minimum(1, config.inverter_limits_kw[group] / np.maximum(total, 1e-9))
        for key in members:
            banks[key] *= factor
    total = sum(banks.values(), np.zeros(len(intervals)))
    return total, banks, poa
