"""Read-only Home Assistant REST/WebSocket client and selected-source ingestion."""

import json
import math
import os
import threading
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx
from websockets.sync.client import connect as websocket_connect

from .composition import dt
from .home_assistant import BridgeMetadata, sensor_contexts
from .schemas import Batch, Observation
from .store import digest, now, stamp


class HAClient:
    def __init__(self, url, token):
        parsed = urlparse(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("Use your Home Assistant HTTP(S) origin")
        self.url, self.token = url.rstrip("/"), token

    def get(self, path):
        with httpx.Client(timeout=20, headers={"Authorization": "Bearer " + self.token}) as client:
            response = client.get(self.url + "/api/" + path)
            response.raise_for_status()
            return response.json()

    def reports(self, entities):
        # State.as_dict can cache last_reported while HA updates the live State object.
        # Read only the selected entities' actual report timestamps, without writing HA state.
        template = (
            "{% set ns = namespace(result={}) %}{% for e in "
            + json.dumps(entities)
            + " %}{% set s = states[e] %}{% if s is not none %}{% set ns.result = dict(ns.result, **{e: s.last_reported.isoformat()}) %}{% endif %}{% endfor %}{{ ns.result | to_json }}"
        )
        with httpx.Client(timeout=20, headers={"Authorization": "Bearer " + self.token}) as client:
            response = client.post(self.url + "/api/template", json={"template": template})
            response.raise_for_status()
            return json.loads(response.text)

    def commands(self, commands):
        url = (
            self.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
            + "/api/websocket"
        )
        with websocket_connect(url, open_timeout=15, close_timeout=3, max_size=16_000_000) as ws:
            json.loads(ws.recv(timeout=20))
            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            if json.loads(ws.recv(timeout=20)).get("type") != "auth_ok":
                raise ValueError("Home Assistant authentication failed")
            results = []
            for identifier, command in enumerate(commands, 1):
                ws.send(json.dumps({"id": identifier, **command}))
                response = json.loads(ws.recv(timeout=30))
                if not response.get("success"):
                    raise ValueError("Home Assistant API command failed: " + command["type"])
                results.append(response["result"])
            return results


def save_connection(store, url, token):
    path = store.root / "home-assistant-connection.json"
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump({"url": url.rstrip("/"), "token": token}, output)
    os.replace(temporary, path)


def client_for(store):
    path = store.root / "home-assistant-connection.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    return HAClient(payload["url"], payload["token"])


def finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def catalog(states, statistics=(), excluded=()):
    result = {}
    for state in states:
        source, attrs = state["entity_id"], state.get("attributes", {})
        unit = attrs.get("unit_of_measurement")
        if (
            not source.startswith("sensor.")
            or source in excluded
            or source.startswith("sensor.energy_forecast_")
            or unit not in ("W", "kW", "Wh", "kWh", "%", "°C", "W/m²")
        ):
            continue
        name = attrs.get("friendly_name", source)
        contexts = sensor_contexts(source, name, unit, attrs.get("device_class"))
        stored_energy = "battery_stored_energy" in contexts
        if (
            unit in ("Wh", "kWh")
            and attrs.get("state_class") not in ("total", "total_increasing")
            and not stored_energy
        ):
            continue
        result[source] = {
            "source": source,
            "name": name,
            "unit": unit,
            "kind": "state"
            if stored_energy
            else "mean_power"
            if unit in ("W", "kW")
            else "counter"
            if unit in ("Wh", "kWh")
            else "state",
            "device_class": attrs.get("device_class"),
            "state_class": attrs.get("state_class"),
            "inactive": False,
            "contexts": sensor_contexts(source, name, unit, attrs.get("device_class")),
        }
    for meta in statistics:
        source = meta["statistic_id"]
        unit = (
            meta.get("statistics_unit_of_measurement")
            or meta.get("unit_of_measurement")
            or meta.get("display_unit_of_measurement")
        )
        if (
            source in result
            or source in excluded
            or source.startswith("sensor.energy_forecast_")
            or unit not in ("W", "kW", "Wh", "kWh")
        ):
            continue
        if unit in ("Wh", "kWh") and not meta.get("has_sum"):
            continue
        name = meta.get("name") or source
        result[source] = {
            "source": source,
            "name": name,
            "unit": unit,
            "kind": "mean_power" if unit in ("W", "kW") else "counter",
            "inactive": True,
            "contexts": sensor_contexts(source, name, unit),
        }
    return list(result.values())


def selected_inputs(store, config):
    # Derive selections from the saved configuration so later mapping edits cannot diverge.
    metadata = {s["source"]: s for s in (store.meta("ha_bridge") or {}).get("sources", [])}
    overrides = {(s["source"], s["feature"]): s for s in store.meta("bridge_inputs") or []}
    banks = {b["target"]: b for b in config.get("banks", []) if b.get("target")}
    result = []
    for mapping in config.get("mappings", []):
        for source in mapping["sources"]:
            meta = metadata.get(source["source"])
            if not meta:
                continue
            feature = mapping["feature"]
            previous = overrides.get((source["source"], feature), {})
            result.append(
                {
                    "source": source["source"],
                    "feature": feature,
                    "unit": meta["unit"],
                    "kind": meta["kind"],
                    "boundary": source.get("measurement_boundary")
                    or (
                        banks[feature].get("measurement_boundary", "unknown")
                        if feature in banks
                        else "stored"
                        if feature in ("battery_soc", "battery_stored_energy")
                        else "environment"
                        if meta["unit"] in ("°C", "W/m²")
                        else "AC"
                    ),
                    "epoch": source.get("epoch") or previous.get("epoch", "ha-direct-v1"),
                    "history": meta["kind"] in ("counter", "mean_power")
                    and feature not in ("battery_soc", "export_limit"),
                }
            )
    return result


def ingest_records(store, records):
    # Recorder may revise an hour. Keep immutable older revisions and skip identical repeats.
    accepted = 0
    for offset in range(0, len(records), 500):
        rows = []
        for row in records[offset : offset + 500]:
            row = Observation.model_validate(row).model_dump(mode="json")
            with store.connect() as db:
                previous = db.execute(
                    "SELECT payload FROM observations WHERE source=? AND epoch=? AND feature=? AND start=? AND end=? ORDER BY revision DESC LIMIT 1",
                    (row["source"], row["epoch"], row["feature"], row["start"], row["end"]),
                ).fetchone()
            revision = 0
            if previous:
                old = json.loads(previous[0])
                revision = old["revision"] + 1
                check = Observation.model_validate({**row, "revision": old["revision"]}).model_dump(
                    mode="json"
                )
                if old == check:
                    continue
            rows.append(Observation.model_validate({**row, "revision": revision}))
        if rows:
            payload = [r.model_dump(mode="json") for r in rows]
            accepted += store.ingest(Batch(batch_id="ha-" + digest(payload), observations=rows))[
                "accepted"
            ]
    return accepted


class HAPoller:
    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="ha-reader")

    def start(self):
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        self.thread.join(timeout=5)

    def run(self):
        while not self.stop.is_set():
            if client_for(self.store):
                try:
                    self.poll()
                except Exception as error:
                    status = self.store.meta("ha_ingestion") or {}
                    self.store.meta(
                        "ha_ingestion",
                        {
                            **status,
                            "status": "error",
                            "error": type(error).__name__,
                            "attempted_at": stamp(),
                        },
                    )
            history = self.store.meta("ha_history") or {}
            older = self.store.meta("ha_history_older") or {}
            self.stop.wait(
                10
                if not history.get("complete")
                or (
                    int(self.store.meta("ha_history_days") or 730) > 90
                    and not older.get("complete")
                )
                else 30
            )

    def poll(self):
        store = self.store
        client = client_for(store)
        if not client:
            return
        current = now()
        states = client.get("states")
        status = store.meta("ha_ingestion") or {}
        if not status.get("catalog_at") or current - dt(status["catalog_at"]) > timedelta(hours=1):
            home = client.get("config")
            statistics, registry = client.commands(
                [{"type": "recorder/list_statistic_ids"}, {"type": "config/entity_registry/list"}]
            )
            excluded = {e["entity_id"] for e in registry if e.get("platform") == "energy_forecast"}
            bridge = BridgeMetadata(
                home={
                    "name": home["location_name"],
                    "latitude": home["latitude"],
                    "longitude": home["longitude"],
                    "timezone": home["time_zone"],
                },
                sources=catalog(states, statistics, excluded),
                ha_version=home.get("version"),
            )
            store.meta(
                "ha_bridge",
                {
                    **bridge.model_dump(mode="json"),
                    "url": client.url,
                    "pairing": "direct_api",
                    "connected_at": stamp(),
                },
            )
            status["catalog_at"] = stamp()
        config = store.configuration()
        if not config:
            store.meta(
                "ha_ingestion", {**status, "status": "connected", "last_success_at": stamp()}
            )
            return
        selected = selected_inputs(store, config)
        store.meta("bridge_inputs", selected)
        validity = {
            (src["source"], profile["feature"]): src
            for profile in config["mappings"]
            for src in profile["sources"]
        }

        def is_current(mapping):
            source = validity[(mapping["source"], mapping["feature"])]
            return (not source.get("valid_from") or dt(source["valid_from"]) <= current) and (
                not source.get("valid_to") or current < dt(source["valid_to"])
            )

        by_id = {s["entity_id"]: s for s in states}
        reports = (
            client.reports(
                sorted({s["source"] for s in selected if s["source"] in by_id and is_current(s)})
            )
            if selected
            else {}
        )
        records, source_status = [], []
        previous = store.meta("ha_live_previous") or {}
        for mapping in selected:
            source = mapping["source"]
            state = by_id.get(source)
            if not is_current(mapping):
                source_status.append(
                    {"source": source, "feature": mapping["feature"], "status": "historical_only"}
                )
                continue
            if not state:
                source_status.append({"source": source, "status": "historical_only"})
                continue
            value = finite(state["state"])
            reported = dt(
                reports.get(source) or state.get("last_reported") or state["last_updated"]
            )
            unit = state.get("attributes", {}).get("unit_of_measurement")
            age = (current - reported).total_seconds()
            allowed_age = config["soc_freshness_seconds"] if mapping["kind"] == "state" else 900
            quality = (
                "valid"
                if value is not None and unit == mapping["unit"] and age <= allowed_age
                else "invalid"
            )
            source_status.append(
                {
                    "source": source,
                    "feature": mapping["feature"],
                    "value": value,
                    "status": "reporting" if quality == "valid" else "unavailable_or_stale",
                    "last_reported": reported.isoformat(),
                    "unit": unit,
                }
            )
            sample_key = f"{source}:{mapping['feature']}:{mapping['epoch']}"
            last = previous.get(sample_key)
            if mapping["kind"] in ("state", "counter"):
                if last and last.get("reported") == reported.isoformat():
                    continue
                # The reading is timestamped at its source; age is checked for current readiness.
                quality = "valid" if value is not None and unit == mapping["unit"] else "invalid"
            start, end = reported - timedelta(seconds=1), reported
            reasons = [] if quality == "valid" else ["source_stale_or_missing"]
            if mapping["kind"] == "mean_power":
                last = previous.get(sample_key)
                end = current
                start = dt(last["at"]) if last else current - timedelta(seconds=30)
                if (
                    not last
                    or last["value"] is None
                    or value is None
                    or (end - start).total_seconds() > 90
                ):
                    quality, reasons = "invalid", ["power_gap_or_initial_sample"]
                else:
                    value = (last["value"] + value) / 2
            elif mapping["kind"] == "counter":
                start = (
                    dt(last["reported"])
                    if last and last.get("reported")
                    else reported - timedelta(seconds=1)
                )
            records.append(
                {
                    **{
                        k: mapping[k]
                        for k in ("source", "feature", "unit", "kind", "boundary", "epoch")
                    },
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "value": value,
                    "quality": quality,
                    "reasons": reasons,
                    "provenance": "live",
                }
            )
            previous[sample_key] = {
                "value": finite(state["state"]),
                "at": current.isoformat(),
                "reported": reported.isoformat(),
            }
        live = ingest_records(store, records)
        store.meta("ha_live_previous", previous)
        historical = [s for s in selected if s["history"]]
        imported = 0
        if historical:
            signature = digest(
                [
                    {k: s[k] for k in ("source", "feature", "epoch", "unit", "boundary")}
                    for s in historical
                ]
            )
            checkpoint = store.meta("ha_history") or {}
            limit = (current - timedelta(minutes=10)).replace(minute=0, second=0, microsecond=0)
            # Recent history first
            # Older data can be requested by extending the installation setting.
            beginning = limit - timedelta(days=90)
            start = (
                dt(checkpoint["through"]) if checkpoint.get("signature") == signature else beginning
            )
            start = min(start, limit - timedelta(hours=3))  # Recheck recent Recorder revisions.
            end = min(start + timedelta(days=7), limit)
            older = False
            older_checkpoint = store.meta("ha_history_older") or {}
            older_limit = beginning
            older_start = (
                dt(older_checkpoint["through"])
                if older_checkpoint.get("signature") == signature
                else limit - timedelta(days=int(store.meta("ha_history_days") or 730))
            )
            if (
                checkpoint.get("signature") == signature
                and checkpoint.get("complete")
                and older_start < older_limit
            ):
                older = True
                start, end = older_start, min(older_start + timedelta(days=28), older_limit)
            result = client.commands(
                [
                    {
                        "type": "recorder/statistics_during_period",
                        "statistic_ids": sorted({s["source"] for s in historical}),
                        "start_time": start.isoformat(),
                        "end_time": end.isoformat(),
                        "period": "hour",
                        "units": {"power": "kW", "energy": "kWh"},
                        "types": ["mean", "change"],
                    }
                ]
            )[0]
            records = []
            for mapping in historical:
                for row in result.get(mapping["source"], []):
                    first = (
                        dt(row["start"])
                        if isinstance(row["start"], str)
                        else datetime.fromtimestamp(
                            row["start"] / (1000 if row["start"] > 1e11 else 1), UTC
                        )
                    )
                    value = finite(
                        row.get("mean") if mapping["kind"] == "mean_power" else row.get("change")
                    )
                    records.append(
                        {
                            **{k: mapping[k] for k in ("source", "feature", "boundary", "epoch")},
                            "start": first.isoformat(),
                            "end": (first + timedelta(hours=1)).isoformat(),
                            "value": value,
                            "unit": "kW" if mapping["kind"] == "mean_power" else "kWh",
                            "kind": "mean_power"
                            if mapping["kind"] == "mean_power"
                            else "interval_energy",
                            "quality": "valid" if value is not None else "invalid",
                            "reasons": [] if value is not None else ["recorder_value_missing"],
                            "provenance": "statistics",
                        }
                    )
            imported = ingest_records(store, records)
            store.meta(
                "ha_history_older" if older else "ha_history",
                {
                    "signature": signature,
                    "from": beginning.isoformat(),
                    "through": end.isoformat(),
                    "target": limit.isoformat(),
                    "complete": end >= (older_limit if older else limit),
                },
            )
        status.pop("error", None)
        store.meta(
            "ha_ingestion",
            {
                **status,
                "status": "connected",
                "last_success_at": stamp(),
                "last_live_accepted": live,
                "last_history_accepted": imported,
                "sources": source_status,
            },
        )
        # Keep a fresh advisory snapshot even while the bounded model worker is training.
        from .forecast import refresh
        from .jobs import enqueue

        refresh(store)
        last = store.meta("ha_calibration_requested_at")
        if imported and (not last or current - dt(last) >= timedelta(minutes=5)):
            enqueue(store, "pv_calibrate")
            store.meta("ha_calibration_requested_at", stamp())


def tariff_from_states(states, import_source=None, export_source=None):
    """Combine independent helper schedules, applying each sensor's GST multiplier once."""
    candidates = [
        s
        for s in states
        if s.get("attributes", {}).get("windows")
        and s["attributes"].get("unit_of_measurement") == "AUD/kWh"
    ]

    def pick(direction, source):
        matches = (
            [s for s in candidates if s["entity_id"] == source]
            if source
            else [s for s in candidates if direction in s["entity_id"]]
        )
        if len(matches) != 1:
            raise ValueError("Select one import and one export tariff helper sensor")
        return matches[0]

    imports, exports = pick("import", import_source), pick("export", export_source)

    def minute(value):
        hour, minutes = map(int, value.split(":")[:2])
        return hour * 60 + minutes

    cuts = sorted(
        {
            0,
            1440,
            *[
                minute(w[k])
                for s in (imports, exports)
                for w in s["attributes"]["windows"]
                for k in ("start", "end")
            ],
        }
    )

    def rate(sensor, at):
        windows = sensor["attributes"]["windows"]
        matching = [
            w
            for w in windows
            if minute(w["start"]) == minute(w["end"])
            or (minute(w["start"]) <= at < minute(w["end"]))
            or (
                minute(w["start"]) > minute(w["end"])
                and (at >= minute(w["start"]) or at < minute(w["end"]))
            )
        ]
        if not matching:
            raise ValueError("Tariff helper schedule has an uncovered interval")
        chosen = min(matching, key=lambda w: (at - minute(w["start"])) % 1440)
        return float(chosen["rate"]) * float(sensor["attributes"].get("gst_multiplier", 1))

    def clock(at):
        return f"{at % 1440 // 60:02d}:{at % 60:02d}"

    rules = [
        {
            "name": f"{clock(a)}–{clock(b)}",
            "start": clock(a),
            "end": clock(b),
            "import_rate": rate(imports, a),
            "export_rate": rate(exports, a),
            "grid_charge_allowed": rate(imports, a) == 0,
            "export_allowed": True,
        }
        for a, b in zip(cuts, cuts[1:])
    ]
    return {
        "rules": rules,
        "import_source": imports["entity_id"],
        "export_source": exports["entity_id"],
        "source": "energy_tariff_helper",
        "imported_at": stamp(),
    }
