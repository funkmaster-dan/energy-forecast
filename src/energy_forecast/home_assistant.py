"""Home Assistant onboarding metadata; credentials stay in a private installation file."""

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
    if (
        unit in ("Wh", "kWh")
        and re.search(r"battery|storage", text)
        and re.search(r"stored energy|remaining energy|remaining capacity|available energy", text)
        and not re.search(r"nominal|rated|maximum|\bmax\b|forecast", text)
    ):
        return ["battery_stored_energy"]
    if re.search(
        r"nominal|rated|\bmax(?:imum)?\b|\bmin(?:imum)?\b|capacity|forecast|prediction|setpoint|limit",
        text,
    ):
        return []
    result = []
    label = name.lower().replace("_", " ")
    household_label = bool(re.search(r"household|home load|home consumption", label))
    pv_label = bool(re.search(r"pv|solar|generation|yield", label))
    if unit in ("W", "kW", "Wh", "kWh"):
        if (
            pv_label or (not household_label and re.search(r"pv|solar|generation|yield", text))
        ) and not re.search(r"solar to|pv to|self consumption", text):
            result.append("pv_generation")
        if (
            not pv_label
            and re.search(
                r"household|home load|home consumption|load power|instantaneous load|total load|consumption",
                text,
            )
            and not re.search(
                r"battery charge|grid import|grid export|grid consumption|grid to", text
            )
        ):
            result.append("household_load")
        if not result:
            result.append(
                "electrical_flow"
                if re.search(
                    r"grid import|grid export|grid consumption|grid to|battery charge|battery discharge|battery to|solar to|pv to",
                    text,
                )
                else "power_energy_other"
            )
    if unit == "%" and (
        re.search(r"\bsoc\b|state of charge", text)
        or (
            device_class == "battery"
            and re.search(r"\bstorage\b|\binverter\b|\bess\b|alphaess", text)
        )
    ):
        result.append("battery_soc")
    return result


def connect(store, payload, service_origin=None, integration_token=None):
    from .ha_client import HAClient, catalog, save_connection

    client = HAClient(payload.url, payload.access_token)
    home = client.get("config")
    states = client.get("states")
    statistics, registry = client.commands(
        [{"type": "recorder/list_statistic_ids"}, {"type": "config/entity_registry/list"}]
    )
    excluded = {e["entity_id"] for e in registry if e.get("platform") == "energy_forecast"}
    metadata = BridgeMetadata(
        home={
            "name": home.get("location_name", "Home"),
            "latitude": home["latitude"],
            "longitude": home["longitude"],
            "timezone": home["time_zone"],
        },
        sources=catalog(states, statistics, excluded),
        ha_version=home.get("version"),
    )
    save_connection(store, payload.url, payload.access_token)
    record = {
        **metadata.model_dump(mode="json"),
        "url": client.url,
        "connected_at": stamp(),
        "pairing": "direct_api",
        "history_days": store.meta("ha_history_days") or 730,
    }
    store.meta("ha_bridge", record)
    return record
