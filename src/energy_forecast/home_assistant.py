"""Read-only HA onboarding; the supplied token is used once and never persisted."""

from urllib.parse import urlparse

import httpx
from pydantic import Field

from .schemas import StrictModel
from .store import stamp


class HomeLocation(StrictModel):
    name: str = Field(max_length=100)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str = Field(max_length=100)


class SourceMetadata(StrictModel):
    source: str = Field(max_length=150)
    name: str = Field(max_length=150)
    unit: str | None = Field(default=None, max_length=30)
    kind: str = Field(default="mean_power", max_length=30)
    inactive: bool = False
    device_class: str | None = Field(default=None, max_length=60)
    state_class: str | None = Field(default=None, max_length=60)
    contexts: list[str] = Field(default_factory=list, max_length=10)


class BridgeMetadata(StrictModel):
    home: HomeLocation
    sources: list[SourceMetadata] = Field(default_factory=list, max_length=10000)
    ha_version: str | None = Field(default=None, max_length=30)


class ConnectionRequest(StrictModel):
    url: str = Field(max_length=300)
    access_token: str = Field(min_length=1, max_length=2000)


def sensor_contexts(source, name, unit, device_class=None):
    import re

    text = (source + " " + name).lower().replace("_", " ")
    if re.search(
        r"nominal|rated|installed capacity|maximum capacity|maximum battery|forecast|prediction|setpoint|limit",
        text,
    ):
        return []
    result = []
    label = name.lower().replace("_", " ")
    household_label = bool(re.search(r"household|home load|home consumption", label))
    pv_label = bool(re.search(r"pv|solar|generation|yield", label))
    if unit in ("W", "kW", "Wh", "kWh"):
        if pv_label or (not household_label and re.search(r"pv|solar|generation|yield", text)):
            result.append("pv_generation")
        if (
            not pv_label
            and re.search(
                r"household|home load|home consumption|load power|instantaneous load|total load|consumption",
                text,
            )
            and not re.search(r"battery charge|grid import|grid export|grid to", text)
        ):
            result.append("household_load")
        if not result and device_class in ("power", "energy"):
            result.append("power_energy_other")
    if unit == "%" and (
        re.search(r"\bsoc\b|state of charge", text)
        or (device_class == "battery" and re.search(r"storage|inverter|ess", text))
    ):
        result.append("battery_soc")
    return result


def connect(store, payload, service_origin, integration_token):
    parsed = urlparse(payload.url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use your Home Assistant HTTP(S) origin without embedded credentials")
    url = payload.url.rstrip("/")
    with httpx.Client(
        base_url=url,
        headers={"Authorization": "Bearer " + payload.access_token},
        timeout=20,
        follow_redirects=False,
    ) as client:
        response = client.get("/api/config")
        response.raise_for_status()
        home = response.json()
        response = client.get("/api/states")
        response.raise_for_status()
        sources = []
        for state in response.json():
            entity = state["entity_id"]
            attrs = state.get("attributes", {})
            if not entity.startswith("sensor.") or entity.startswith("sensor.energy_forecast_"):
                continue
            unit = attrs.get("unit_of_measurement")
            if unit not in ("W", "kW", "Wh", "kWh", "%", "°C", "W/m²"):
                continue
            if unit in ("Wh", "kWh") and attrs.get("state_class") not in (
                "total",
                "total_increasing",
            ):
                continue
            kind = (
                "mean_power"
                if unit in ("W", "kW")
                else "counter"
                if unit in ("Wh", "kWh")
                else "state"
            )
            sources.append(
                {
                    "source": entity,
                    "name": attrs.get("friendly_name", entity),
                    "unit": unit,
                    "kind": kind,
                    "device_class": attrs.get("device_class"),
                    "state_class": attrs.get("state_class"),
                    "contexts": sensor_contexts(
                        entity, attrs.get("friendly_name", entity), unit, attrs.get("device_class")
                    ),
                }
            )
        metadata = BridgeMetadata(
            home={
                "name": home.get("location_name", "Home"),
                "latitude": home["latitude"],
                "longitude": home["longitude"],
                "timezone": home["time_zone"],
            },
            sources=sources,
            ha_version=home.get("version"),
        )
        pairing = "integration_required"
        flow = client.post("/api/config/config_entries/flow", json={"handler": "energy_forecast"})
        if flow.status_code == 200:
            result = flow.json()
            if result.get("type") == "form" and result.get("step_id") == "user":
                paired = client.post(
                    "/api/config/config_entries/flow/" + result["flow_id"],
                    json={"url": service_origin, "token": integration_token, "site_id": "home"},
                )
                if paired.status_code == 200 and paired.json().get("type") == "create_entry":
                    pairing = "paired"
                else:
                    pairing = "manual_pairing_required"
            elif result.get("reason") == "already_configured":
                pairing = "paired"
    record = {
        **metadata.model_dump(mode="json"),
        "url": url,
        "connected_at": stamp(),
        "pairing": pairing,
    }
    store.meta("ha_bridge", record)
    return record
