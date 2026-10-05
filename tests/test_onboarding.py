import json

import httpx
from fastapi.testclient import TestClient

from energy_forecast import security
from energy_forecast.api import create_app
from energy_forecast.home_assistant import ConnectionRequest, connect


def test_ha_connect_defaults_location_and_does_not_store_access_token(store, monkeypatch):
    original = httpx.Client
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path == "/api/config":
            return httpx.Response(
                200,
                json={
                    "location_name": "My home",
                    "latitude": -35.1,
                    "longitude": 138.8,
                    "time_zone": "Australia/Adelaide",
                    "version": "2026.9.4",
                },
            )
        if request.url.path == "/api/states":
            return httpx.Response(
                200,
                json=[
                    {
                        "entity_id": "sensor.pv1",
                        "attributes": {
                            "friendly_name": "Roof bank",
                            "unit_of_measurement": "W",
                            "unrelated_private_attribute": "must not forward",
                        },
                    },
                    {
                        "entity_id": "sensor.energy_forecast_pv",
                        "attributes": {"unit_of_measurement": "W"},
                    },
                ],
            )
        if request.url.path == "/api/config/config_entries/flow":
            return httpx.Response(
                200, json={"type": "form", "step_id": "user", "flow_id": "fixture"}
            )
        return httpx.Response(200, json={"type": "create_entry"})

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond))
    )
    record = connect(
        store,
        ConnectionRequest(url="http://ha.test:8123", access_token="synthetic-one-use-token"),
        "http://service.test:8080",
        "synthetic-integration-token",
    )
    assert record["home"]["latitude"] == -35.1
    assert record["pairing"] == "paired"
    assert len(record["sources"]) == 1
    persisted = json.dumps(store.meta("ha_bridge"))
    assert "synthetic-one-use-token" not in persisted
    assert "unrelated_private_attribute" not in persisted
    assert json.loads(calls[-1].content)["url"] == "http://service.test:8080"


def test_ha_metadata_can_pair_before_site_configuration(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    token = security.mint(app.state.store, "integration")
    client = TestClient(app, headers={"Authorization": "Bearer " + token})
    result = client.post(
        "/v1/sites/home/bridge/metadata",
        json={
            "home": {
                "name": "Home",
                "latitude": -35,
                "longitude": 139,
                "timezone": "Australia/Adelaide",
            },
            "sources": [],
        },
    )
    assert result.status_code == 200
    assert app.state.store.configuration() is None
    assert app.state.store.meta("ha_bridge")["pairing"] == "paired"
    assert client.get("/v1/sites/home/home-assistant").status_code == 403


def test_sensor_contexts_reject_parameters_and_wrong_measurements():
    from energy_forecast.home_assistant import sensor_contexts

    assert sensor_contexts("sensor.pv1_power", "North PV bank", "W", "power") == ["pv_generation"]
    assert sensor_contexts("sensor.instantaneous_load", "Household load", "W", "power") == [
        "household_load"
    ]
    assert sensor_contexts("sensor.battery_soc", "Battery SoC", "%", "battery") == ["battery_soc"]
    assert (
        sensor_contexts(
            "sensor.maximum_battery_capacity", "Maximum battery capacity", "%", "battery"
        )
        == []
    )
    assert (
        sensor_contexts("sensor.inverter_nominal_power", "Inverter nominal power", "W", "power")
        == []
    )
    assert "household_load" not in sensor_contexts(
        "sensor.grid_import", "Grid import", "kWh", "energy"
    )
    assert sensor_contexts("sensor.phone_battery", "Phone battery", "%", "battery") == []
