export type Json =
  null | boolean | number | string | Json[] | { [key: string]: Json };
export type Config = {
  revision: number;
  name: string;
  timezone: string;
  latitude: number;
  longitude: number;
  weather_model?:
    "best_match" | "ecmwf_ifs" | "gfs_global" | "bom_access_global";
  banks: {
    id: string;
    dc_kwp: number;
    tilt: number;
    azimuth: number;
    inverter_group: string;
    target?: string;
    conversion_efficiency: number;
  }[];
  battery: {
    capacity_kwh: number;
    reserve_kwh: number;
    upper_kwh: number;
    charge_kw: number;
    discharge_kw: number;
    eta_charge: number;
    eta_discharge: number;
    standby_kw: number;
    taper_start_pct: number;
    parameter_source: string;
  };
  mappings: Json[];
  tariff: Json[];
  calendars: Json;
  learning_paused: boolean;
  recent_days: number;
  terminal_reserve_kwh: number | null;
  [key: string]: unknown;
};
export type Series = {
  start: string;
  end: string;
  pv_kw: number | null;
  load_kw: number | null;
  pv_kwh: number | null;
  load_kwh: number | null;
  temperature_c: number | null;
  ghi_w_m2: number | null;
  dni_w_m2: number | null;
  dhi_w_m2: number | null;
  load_p05_kw: number | null;
  load_p95_kw: number | null;
  banks_kw: Record<string, number | null>;
  poa_w_m2: Record<string, number | null>;
};
export type Schedule = {
  start: string;
  end: string;
  soc_pct: number;
  stored_kwh: number;
  grid_import_kw: number;
  grid_export_kw: number;
  battery_export_kw: number;
  charge_kw: number;
  discharge_kw: number;
  tariff: {
    name: string;
    export_rate: number;
    import_rate: number;
    window_id: string;
    grid_charge_allowed: boolean;
  };
};
export type Forecast = {
  forecast_id: string | null;
  status: string;
  issued_at?: string;
  valid_until?: string;
  weather_vintage?: string;
  historical_replay?: boolean;
  observed_series?: {
    start: string;
    end: string;
    load_kw: number | null;
    pv_kw: number | null;
    quality_flags: string[];
  }[];
  series?: Series[];
  totals?: { pv_48h_kwh: number | null; load_48h_kwh: number | null };
  export_plan: {
    safe_battery_export_remaining_kwh: number;
    advisory_export_power_kw: number;
    required_reserve_kwh?: number;
    source_soc_pct?: number;
    source_soc_at?: string;
    reason_codes: string[];
    schedule?: Schedule[];
    shadow_candidate?: {
      battery_export_kwh: number;
      paid_import_shortfall_kwh: number;
      natural_pv_export_kwh: number;
      total_site_export_kwh: number;
    };
    conditional_free_charge?: Json;
  };
};
