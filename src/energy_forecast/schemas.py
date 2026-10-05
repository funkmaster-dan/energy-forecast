"""API v1: energy at the aggregate AC bus, stored energy inside the battery."""

from datetime import UTC, date, datetime, time
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Quality(StrEnum):
    valid = "valid"
    suspect = "suspect"
    invalid = "invalid"
    imputed = "imputed"


class Observation(StrictModel):
    feature: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    source: str = Field(min_length=1, max_length=150)
    epoch: str = Field(min_length=1, max_length=80)
    start: datetime
    end: datetime
    value: float | None
    unit: Literal["W", "kW", "Wh", "kWh", "%", "°C", "W/m²"]
    kind: Literal["mean_power", "interval_energy", "counter", "state"]
    boundary: Literal["AC", "DC", "stored", "environment"]
    revision: int = Field(default=0, ge=0)
    provenance: Literal["live", "statistics", "raw_history"] = "live"
    quality: Quality = Quality.valid
    reasons: list[str] = Field(default_factory=list, max_length=20)
    coverage: float = Field(default=1, ge=0, le=1)

    @field_validator("start", "end")
    @classmethod
    def utc(cls, value):
        if value.tzinfo is None:
            raise ValueError("An explicit UTC offset is required")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def interval(self):
        if self.end <= self.start:
            raise ValueError("end must follow start")
        if self.kind == "mean_power" and self.unit not in ("W", "kW"):
            raise ValueError("mean_power requires W or kW")
        if self.kind in ("interval_energy", "counter") and self.unit not in ("Wh", "kWh"):
            raise ValueError("energy requires Wh or kWh")
        if self.source.startswith(("sensor.energy_forecast_", "binary_sensor.energy_forecast_")):
            raise ValueError("Forecast output cannot be an input")
        return self


class Batch(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    batch_id: str = Field(min_length=1, max_length=128)
    observations: list[Observation] = Field(min_length=1, max_length=5000)


class Source(StrictModel):
    source: str
    priority: int = 0
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    scale: float = Field(default=1, gt=0)
    sign: Literal[-1, 1] = 1
    dc_to_ac_efficiency: float = Field(default=0.96, gt=0, le=1)
    coefficient: float = Field(default=1, ge=-1, le=1)
    component: str | None = None

    @field_validator("valid_from", "valid_to")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Source validity requires timezone")
        return value


class Mapping(StrictModel):
    feature: str
    mode: Literal["stitch", "fallback", "sum", "derived"] = "stitch"
    sources: list[Source] = Field(min_length=1, max_length=30)
    min_coverage: float = Field(default=0.9, ge=0, le=1)
    maximum_kw: float = Field(default=50, gt=0)
    resolution_minutes: Literal[5, 15, 60] = 60

    @model_validator(mode="after")
    def unique(self):
        ids = [s.source for s in self.sources]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate source")
        if self.mode == "sum" and (
            any(not s.component for s in self.sources)
            or len({s.component for s in self.sources}) != len(ids)
        ):
            raise ValueError("Sum requires explicit distinct disjoint components")
        return self


class Bank(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    dc_kwp: float = Field(gt=0, le=100)
    tilt: float = Field(ge=0, le=90)
    azimuth: float = Field(ge=0, lt=360)
    inverter_group: str = "main"
    target: str | None = None
    conversion_efficiency: float = Field(default=0.96, gt=0, le=1)


class Battery(StrictModel):
    capacity_kwh: float = Field(default=10, gt=0, le=500)
    reserve_kwh: float = Field(default=2, ge=0)
    upper_kwh: float = Field(default=10, gt=0)
    charge_kw: float = Field(default=3, gt=0)
    discharge_kw: float = Field(default=3, gt=0)
    eta_charge: float = Field(default=0.95, gt=0, le=1)
    eta_discharge: float = Field(default=0.95, gt=0, le=1)
    standby_kw: float = Field(default=0, ge=0)
    taper_start_pct: float = Field(default=90, ge=50, lt=100)
    parameter_source: str = "configured usable capacity"
    independent_stored_energy: bool = False

    @model_validator(mode="after")
    def bounds(self):
        if not self.reserve_kwh < self.upper_kwh <= self.capacity_kwh:
            raise ValueError("Require reserve < upper <= capacity")
        return self


class TariffRule(StrictModel):
    name: str
    start: time = time(0)
    end: time = time(0)
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)))
    months: list[int] = Field(default_factory=lambda: list(range(1, 13)))
    dates: list[date] = Field(default_factory=list)
    priority: int = 0
    import_rate: float = Field(ge=0)
    export_rate: float = Field(default=0, ge=0)
    grid_charge_allowed: bool = False
    export_allowed: bool = True
    export_kw: float | None = Field(default=None, ge=0)
    import_kw: float | None = Field(default=None, ge=0)
    export_cap_kwh: float | None = Field(default=None, ge=0)
    reexport_grid_energy: bool = True

    @field_validator("weekdays", "months")
    @classmethod
    def ranges(cls, value, info):
        allowed = range(7) if info.field_name == "weekdays" else range(1, 13)
        if not value or any(v not in allowed for v in value):
            raise ValueError("Invalid calendar selector")
        return value


class CalendarEvent(StrictModel):
    start: date
    end: date
    start_time: time = time(0)
    end_time: time = time(0)
    label: str
    kind: Literal["school_holiday", "public_holiday", "household", "pupil_free"]
    profile: str = "government"
    source: str
    version: str
    known_at: datetime | None = None

    @field_validator("known_at")
    @classmethod
    def knowledge_time(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("known_at requires an explicit offset")
        return value

    @model_validator(mode="after")
    def dates(self):
        if (self.end, self.end_time) <= (self.start, self.start_time):
            raise ValueError("Calendar end is exclusive and must follow start")
        return self


class Calendars(StrictModel):
    public_jurisdiction: Literal["ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"] = "SA"
    school_jurisdiction: Literal["ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"] = "SA"
    region: str | None = None
    enabled: bool = False
    covered_years: list[int] = Field(default_factory=list)
    school_profiles: list[str] = Field(default_factory=lambda: ["government"])
    school_combination: Literal["any", "all"] = "any"
    events: list[CalendarEvent] = Field(default_factory=list, max_length=5000)


class Configuration(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    revision: int = Field(default=0, ge=0)
    name: str = Field(default="My household", max_length=100)
    timezone: str = "Australia/Adelaide"
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    weather_model: Literal["best_match", "ecmwf_ifs", "gfs_global", "bom_access_global"] = (
        "best_match"
    )
    banks: list[Bank] = Field(default_factory=list, max_length=50)
    inverter_limits_kw: dict[str, float] = Field(default_factory=lambda: {"main": 5})
    battery: Battery = Field(default_factory=Battery)
    mappings: list[Mapping] = Field(default_factory=list)
    tariff: list[TariffRule] = Field(min_length=1, max_length=100)
    calendars: Calendars = Field(default_factory=Calendars)
    export_limit_kw: float = Field(default=5, ge=0)
    import_limit_kw: float = Field(default=10, gt=0)
    shared_inverter_kw: float | None = Field(default=None, gt=0)
    mandatory_phase_limits: bool = False
    required_dynamic_limit: bool = False
    soc_freshness_seconds: int = Field(default=120, ge=30, le=3600)
    lease_seconds: int = Field(default=60, ge=15, le=120)
    weather_freshness_seconds: int = Field(default=7200, ge=900, le=21600)
    recent_days: int = Field(default=14, ge=7, le=30)
    learning_paused: bool = False
    target_confidence: float = Field(default=0.99, ge=0.9, le=0.999)
    terminal_reserve_kwh: float | None = Field(default=None, ge=0)
    paid_import_tolerance_kwh: float = Field(default=0, ge=0, le=0.1)
    grid_charge_intent: bool = False
    scenario_count: int = Field(default=2000, ge=100, le=10000)

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Use a valid IANA timezone") from error
        return value

    @model_validator(mode="after")
    def validate_topology(self):
        if len({b.id for b in self.banks}) != len(self.banks):
            raise ValueError("Bank IDs must be unique")
        for bank in self.banks:
            if self.inverter_limits_kw.get(bank.inverter_group, 0) <= 0:
                raise ValueError("Each bank needs a positive inverter group limit")
        if len({m.feature for m in self.mappings}) != len(self.mappings):
            raise ValueError("Mapping features must be unique")
        if (
            self.terminal_reserve_kwh is not None
            and self.terminal_reserve_kwh > self.battery.upper_kwh
        ):
            raise ValueError("Terminal reserve exceeds battery upper bound")
        return self


class ExportPlan(StrictModel):
    safe_battery_export_remaining_kwh: float = Field(default=0, ge=0)
    safe_battery_export_now_kwh: float = Field(default=0, ge=0)
    advisory_export_power_kw: float = Field(default=0, ge=0)
    target_path_confidence: float = Field(default=0.99, gt=0, lt=1)
    estimated_path_confidence: float | None = Field(default=None, ge=0, le=1)
    required_reserve_kwh: float | None = None
    required_reserve_soc_pct: float | None = None
    source_soc_at: datetime | None = None
    source_soc_pct: float | None = None
    replacement_budget: Literal[True] = True
    conditional_on_free_charge: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    schedule: list[dict] = Field(default_factory=list)
    shadow_candidate: dict | None = None
    stress_failed: bool | None = None
    conditional_free_charge: dict | None = None
    scenario_diagnostics: dict | None = None


class ForecastResponse(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    forecast_id: str | None = None
    status: Literal[
        "warming_up",
        "shadow",
        "ready",
        "degraded",
        "stale",
        "invalid_configuration",
        "baseline_infeasible",
        "horizon_insufficient",
    ]
    issued_at: datetime | None = None
    valid_until: datetime | None = None
    configuration_hash: str | None = None
    configuration_revision: int | None = None
    weather_vintage: str | None = None
    input_watermark: datetime | None = None
    model_versions: dict[str, str | None] = Field(default_factory=dict)
    resolution_minutes: int = 60
    horizon_hours: int = 48
    units: dict[str, str] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    series: list[dict] = Field(default_factory=list)
    totals: dict[str, float | None] = Field(default_factory=dict)
    export_plan: ExportPlan
    history_intervals: int = 0
    historical_replay: bool = False
    observed_series: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def authorization(self):
        plan = self.export_plan
        if self.status != "ready" and (
            plan.safe_battery_export_remaining_kwh
            or plan.safe_battery_export_now_kwh
            or plan.advisory_export_power_kw
        ):
            raise ValueError("Non-ready forecasts cannot authorize discretionary export")
        if self.status == "ready" and (
            not self.forecast_id
            or not self.configuration_hash
            or not self.issued_at
            or not self.valid_until
            or not plan.source_soc_at
            or plan.estimated_path_confidence is None
            or plan.estimated_path_confidence < plan.target_path_confidence
        ):
            raise ValueError("Ready requires a leased generation and validated path confidence")
        return self
