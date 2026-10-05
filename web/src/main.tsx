import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Alert,
  AppBar,
  Box,
  Button,
  Chip,
  CssBaseline,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  Switch,
  Tab,
  Tabs,
  TextField,
  ThemeProvider,
  Toolbar,
  Typography,
  createTheme,
} from "@mui/material";
import * as echarts from "echarts";
import type { Config, Forecast, Json, Series } from "./types";
import { CoveragePanel, QualityOverlay, TariffTimeline } from "./panels";
import { SetupFlow } from "./setup";
import { TariffForm, CalendarForm } from "./forms";
import "./style.css";

const base = "/v1/sites/home";
let csrf = "";
async function api<T>(
  path: string,
  method = "GET",
  data?: unknown,
): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({ detail: r.statusText }));
    throw Error(
      typeof e.detail === "string" ? e.detail : JSON.stringify(e.detail),
    );
  }
  return r.json();
}
const defaults: Config = {
  revision: 0,
  name: "My household",
  timezone: "Australia/Adelaide",
  latitude: -34.9,
  longitude: 138.6,
  banks: [],
  inverter_limits_kw: {},
  battery: {
    capacity_kwh: 10,
    reserve_kwh: 2,
    upper_kwh: 10,
    charge_kw: 3,
    discharge_kw: 3,
    eta_charge: 0.95,
    eta_discharge: 0.95,
    standby_kw: 0,
    taper_start_pct: 90,
    parameter_source:
      "Configured usable capacity — verify during commissioning",
  },
  mappings: [],
  tariff: [
    {
      name: "Default",
      start: "00:00",
      end: "00:00",
      weekdays: [0, 1, 2, 3, 4, 5, 6],
      months: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
      priority: 0,
      import_rate: 0.3,
      export_rate: 0.05,
      grid_charge_allowed: false,
      export_allowed: true,
    },
  ],
  calendars: {
    enabled: false,
    public_jurisdiction: "SA",
    school_jurisdiction: "SA",
    covered_years: [],
    events: [],
    school_profiles: ["government"],
    school_combination: "any",
  },
  learning_paused: false,
  recent_days: 14,
  terminal_reserve_kwh: null,
};
const format = (v: number | null | undefined, unit = "") =>
  v == null ? "Unavailable" : `${v.toFixed(2)} ${unit}`;
const reasons: Record<string, string> = {
  forecast_missing: "No forecast has been issued yet.",
  load_history_missing: "More covered household history is needed.",
  soc_stale: "Battery state is missing or too old.",
  weather_missing: "Fresh forecast weather is unavailable.",
  insufficient_tail_validation:
    "Whole-horizon export confidence has not been validated.",
  terminal_obligation_unbounded:
    "Demand beyond the horizon needs a configured terminal reserve.",
  lease_expired: "This forecast lease has expired.",
  inputs_or_configuration_changed:
    "Inputs or settings changed; a replacement forecast is needed.",
  baseline_infeasible:
    "Even with no discretionary export, the baseline needs paid imports or breaches a limit.",
  pv_geometry_missing: "Add panel banks and inverter limits to forecast PV.",
};
function JsonView({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}
function Empty({ children }: { children: React.ReactNode }) {
  return (
    <Paper className="empty">
      <Typography color="text.secondary">{children}</Typography>
    </Paper>
  );
}
function Metric({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail?: string;
}) {
  return (
    <Paper className="metric">
      <Typography className="eyebrow">{label}</Typography>
      <Typography variant="h4">{value}</Typography>
      <Typography variant="body2" color="text.secondary">
        {detail}
      </Typography>
    </Paper>
  );
}
function Charts({
  forecast,
  zone,
  dark,
}: {
  forecast: Forecast;
  zone: string;
  dark: boolean;
}) {
  const node = useRef<HTMLDivElement>(null);
  const [hours, setHours] = useState(48);
  const [bands, setBands] = useState(true);
  const [banks, setBanks] = useState(true);
  useEffect(() => {
    if (!node.current || !forecast.series?.length) return;
    const chart = echarts.init(node.current, dark ? "dark" : undefined);
    const rows = forecast.series.filter(
      (r) =>
        Date.parse(r.start) - Date.parse(forecast.series![0].start) <
        hours * 3600000,
    );
    const schedule = forecast.export_plan.schedule || [];
    const local = (t: string) =>
      new Date(t).toLocaleString("en-AU", {
        timeZone: zone,
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        timeZoneName: "shortOffset",
      });
    const names = [
      "Household & PV · kW AC",
      "Interval energy · kWh",
      schedule.length ? "Battery · %" : "Battery · fresh SoC needed",
      "Irradiance · W/m²",
      "Outdoor temperature · °C",
      schedule.length ? "Grid flow · kW AC" : "Grid flow · fresh inputs needed",
    ];
    const xAxis = names.map((_, i) => ({
      type: "time" as const,
      gridIndex: i,
      splitNumber: node.current!.clientWidth < 500 ? 3 : 6,
      axisLabel: {
        hideOverlap: true,
        formatter: (n: number) =>
          new Date(n).toLocaleString("en-AU", {
            timeZone: zone,
            day: "numeric",
            month: "short",
            hour: "2-digit",
            minute: "2-digit",
          }),
      },
      axisPointer: { show: true },
    }));
    const yAxis = names.map((name, i) => ({
      type: "value" as const,
      gridIndex: i,
      min: i === 4 ? undefined : 0,
      axisLabel: { color: dark ? "#b8c8c4" : "#53685e" },
    }));
    const grid = names.map((_, i) => ({
      left: 65,
      right: 28,
      top: 55 + i * 180,
      height: 115,
    }));
    const series: echarts.SeriesOption[] = [];
    const line = (
      name: string,
      index: number,
      values: (number | null)[],
      times = rows.map((r) => r.start),
      color?: string,
    ) =>
      series.push({
        name,
        type: "line",
        xAxisIndex: index,
        yAxisIndex: index,
        showSymbol: false,
        connectNulls: false,
        lineStyle: { width: 2, color },
        itemStyle: { color },
        data: values.map((v, i) => [times[i], v]),
      });
    line(
      "Household forecast",
      0,
      rows.map((r) => r.load_kw),
      undefined,
      "#e39750",
    );
    line(
      "PV forecast",
      0,
      rows.map((r) => r.pv_kw),
      undefined,
      "#4cad88",
    );
    const observed = (forecast.observed_series || []).filter(
      (r) => Date.parse(r.start) - Date.parse(rows[0].start) < hours * 3600000,
    );
    if (observed.length) {
      line(
        "Household measured (hourly mean)",
        0,
        observed.map((r) => r.load_kw),
        observed.map((r) => r.start),
        "#fac858",
      );
      line(
        "PV measured (hourly mean)",
        0,
        observed.map((r) => r.pv_kw),
        observed.map((r) => r.start),
        "#91cc75",
      );
    }
    if (bands) {
      line(
        "Load P05 (held-out calibration)",
        0,
        rows.map((r) => r.load_p05_kw),
      );
      line(
        "Load P95 (held-out calibration)",
        0,
        rows.map((r) => r.load_p95_kw),
      );
    }
    if (banks)
      Object.keys(rows[0].banks_kw).forEach((key) =>
        line(
          `PV ${key} (${forecast.units?.[`bank:${key}`] || "kW"})`,
          0,
          rows.map((r) => r.banks_kw[key]),
        ),
      );
    line(
      "Household energy",
      1,
      rows.map((r) => r.load_kwh),
    );
    line(
      "PV energy",
      1,
      rows.map((r) => r.pv_kwh),
    );
    const selected = schedule.filter(
      (r) => Date.parse(r.start) - Date.parse(rows[0].start) < hours * 3600000,
    );
    const times = selected.map((r) => r.start);
    line(
      "Battery SoC (shadow simulation)",
      2,
      selected.map((r) => r.soc_pct),
      times,
    );
    line(
      "GHI (Open-Meteo)",
      3,
      rows.map((r) => r.ghi_w_m2),
    );
    line(
      "DNI (Open-Meteo)",
      3,
      rows.map((r) => r.dni_w_m2),
    );
    line(
      "DHI (Open-Meteo)",
      3,
      rows.map((r) => r.dhi_w_m2),
    );
    Object.keys(rows[0].poa_w_m2).forEach((key) =>
      line(
        `POA ${key} (calculated)`,
        3,
        rows.map((r) => r.poa_w_m2[key]),
      ),
    );
    line(
      "Temperature (forecast)",
      4,
      rows.map((r) => r.temperature_c),
    );
    line(
      "Grid import (shadow)",
      5,
      selected.map((r) => r.grid_import_kw),
      times,
    );
    line(
      "Grid export (shadow)",
      5,
      selected.map((r) => r.grid_export_kw),
      times,
    );
    line(
      "Battery export (shadow)",
      5,
      selected.map((r) => r.battery_export_kw),
      times,
    );
    chart.setOption({
      backgroundColor: "transparent",
      title: names.map((text, i) => ({
        text,
        left: 65,
        top: 20 + i * 180,
        textStyle: { fontSize: 14, fontWeight: 500 },
      })),
      legend: { show: false },
      tooltip: {
        trigger: "axis",
        confine: true,
        axisPointer: {
          label: {
            formatter: (p: { value: number }) =>
              local(new Date(p.value).toISOString()),
          },
        },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid,
      xAxis,
      yAxis,
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1, 2, 3, 4, 5] },
        { type: "slider", xAxisIndex: [0, 1, 2, 3, 4, 5], bottom: 3 },
      ],
      series,
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(node.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [forecast, zone, dark, hours, bands, banks]);
  function csv() {
    const rows = forecast.series || [];
    const keys: (keyof Series)[] = [
      "start",
      "end",
      "pv_kw",
      "load_kw",
      "pv_kwh",
      "load_kwh",
      "temperature_c",
      "ghi_w_m2",
    ];
    const blob = new Blob(
      [
        keys.join(",") +
          "\n" +
          rows.map((r) => keys.map((k) => r[k] ?? "").join(",")).join("\n"),
      ],
      { type: "text/csv" },
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `forecast-${forecast.forecast_id}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }
  return (
    <Paper sx={{ p: 2 }}>
      <Stack direction="row" flexWrap="wrap" gap={2} alignItems="center">
        <Button
          variant={hours === 24 ? "contained" : "outlined"}
          onClick={() => setHours(24)}
        >
          24 hours
        </Button>
        <Button
          variant={hours === 48 ? "contained" : "outlined"}
          onClick={() => setHours(48)}
        >
          48 hours
        </Button>
        <FormControlLabel
          control={<Switch checked={bands} onChange={(_, v) => setBands(v)} />}
          label="Calibrated ranges"
        />
        <FormControlLabel
          control={<Switch checked={banks} onChange={(_, v) => setBanks(v)} />}
          label="Panel banks"
        />
        <Button onClick={csv}>Export CSV</Button>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ my: 2 }}>
        Issue:{" "}
        {forecast.issued_at
          ? new Date(forecast.issued_at).toLocaleString()
          : ""}{" "}
        · Vintage {forecast.weather_vintage?.slice(0, 8)}. Scroll to zoom;
        cursors and zoom are linked. Missing ranges mean calibration is
        unavailable.
      </Typography>
      <div ref={node} style={{ height: 1120, width: "100%" }} />
    </Paper>
  );
}
function App() {
  const [dark, setDark] = useState(
    localStorage.getItem("energy-theme") !== "light",
  );
  const theme = useMemo(
    () =>
      createTheme({
        palette: {
          mode: dark ? "dark" : "light",
          primary: { main: dark ? "#73cdb1" : "#207258" },
          background: {
            default: dark ? "#0e1c1b" : "#f3f6f3",
            paper: dark ? "#162a27" : "#fff",
          },
        },
        typography: { fontFamily: "Inter, system-ui, sans-serif" },
        shape: { borderRadius: 14 },
      }),
    [dark],
  );
  const [auth, setAuth] = useState<{
    setup_required: boolean;
    signed_in: boolean;
  } | null>(null);
  const [password, setPassword] = useState("");
  const [setupKey, setSetupKey] = useState("");
  const [tab, setTab] = useState("Overview");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [config, setConfig] = useState<Config>(defaults);
  const [saved, setSaved] = useState<Config | null>(null);
  const [forecast, setForecast] = useState<Forecast | null>(null);
  const [diagnostics, setDiagnostics] = useState<Record<string, unknown>>({});
  const [data, setData] = useState<unknown[]>([]);
  const [jobs, setJobs] = useState<Record<string, unknown>[]>([]);
  const [models, setModels] = useState<Record<string, unknown>[]>([]);
  const [archive, setArchive] = useState<
    { forecast_id: string; issued_at: string; status: string }[]
  >([]);
  const [replay, setReplay] = useState("latest");
  const [preview, setPreview] = useState<unknown>(null);
  const [day, setDay] = useState(new Date().toISOString().slice(0, 10));
  const [review, setReview] = useState<{
    id: number;
    quality: string;
    reason: string;
  } | null>(null);
  const [ticks, setTicks] = useState(Date.now());
  async function action(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function load() {
    const [c, f, d, j, m, a] = await Promise.all([
      api<Config | null>(base + "/configuration"),
      api<Forecast>(base + "/forecast/latest"),
      api<Record<string, unknown>>(base + "/diagnostics"),
      api<Record<string, unknown>[]>(base + "/jobs"),
      api<Record<string, unknown>[]>(base + "/models"),
      api<typeof archive>(base + "/forecasts"),
    ]);
    setSaved(c);
    setConfig(c || defaults);
    if (!c) setTab("Setup");
    setForecast(f);
    setDiagnostics(d);
    setJobs(j);
    setModels(m);
    setArchive(a);
  }
  useEffect(() => {
    void action(async () => {
      const a = await api<{
        setup_required: boolean;
        signed_in: boolean;
        csrf: string;
      }>("/auth/status");
      csrf = a.csrf || "";
      setAuth(a);
      if (a.signed_in) await load();
    });
  }, []);
  useEffect(() => {
    if (!auth?.signed_in) return;
    const timer = setInterval(() => {
      setTicks(Date.now());
      if (replay === "latest")
        void api<Forecast>(base + "/forecast/latest")
          .then(setForecast)
          .catch((e) => setError(e.message));
    }, 10000);
    return () => clearInterval(timer);
  }, [auth, replay]);
  useEffect(() => {
    if (tab === "Data quality" && auth?.signed_in)
      void action(async () =>
        setData(await api<unknown[]>(base + "/observations?limit=500")),
      );
    setPreview(null);
  }, [tab]);
  const expired =
    !!forecast?.valid_until && Date.parse(forecast.valid_until) <= ticks;
  const ready = forecast?.status === "ready" && !expired && replay === "latest";
  const pages = [
    "Overview",
    "Forecasts",
    "Setup",
    "Tariff",
    "Calendars",
    "Data quality",
    "Models",
    "Calibration",
    "Battery",
    "Plan details",
    "Administration",
  ];
  async function save() {
    const c = await api<Config>(base + "/configuration", "PUT", config);
    setConfig(c);
    setSaved(c);
    setNotice(
      "Configuration saved. Previous plans invalidated; refresh jobs queued.",
    );
    setForecast(await api<Forecast>(base + "/forecast/latest"));
  }
  async function job(kind: string) {
    await api(base + "/jobs", "POST", { kind });
    setNotice(`${kind.replace("_", " ")} job queued.`);
    setJobs(await api(base + "/jobs"));
  }
  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <AppBar
        position="sticky"
        color="transparent"
        elevation={0}
        sx={{
          backdropFilter: "blur(16px)",
          borderBottom: "1px solid",
          borderColor: "divider",
        }}
      >
        <Toolbar>
          <Box className="brand-mark">↗</Box>
          <Box sx={{ flexGrow: 1 }}>
            <Typography fontWeight={700}>Energy Forecast</Typography>
            <Typography variant="caption" color="text.secondary">
              HOUSEHOLD ENERGY · ADVISORY
            </Typography>
          </Box>
          <Button
            onClick={() => {
              setDark(!dark);
              localStorage.setItem("energy-theme", dark ? "light" : "dark");
            }}
          >
            {dark ? "Light" : "Dark"}
          </Button>
          {auth?.signed_in && (
            <Button
              onClick={() =>
                void action(async () => {
                  await api("/auth/logout", "POST");
                  setAuth({ ...auth, signed_in: false });
                  setPassword("");
                })
              }
            >
              Sign out
            </Button>
          )}
        </Toolbar>
      </AppBar>
      <main>
        {error && (
          <Alert severity="error" onClose={() => setError("")} sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}
        {notice && (
          <Alert
            severity="success"
            onClose={() => setNotice("")}
            sx={{ mb: 2 }}
          >
            {notice}
          </Alert>
        )}
        {!auth ? (
          <Empty>Loading connection…</Empty>
        ) : !auth.signed_in ? (
          <Paper className="login">
            <Typography variant="h4">
              {auth.setup_required ? "Set up your household" : "Welcome back"}
            </Typography>
            <Typography color="text.secondary" sx={{ my: 2 }}>
              {auth.setup_required
                ? "Create a local administrator. Read /data/setup-key in your service container to claim this installation."
                : "Sign in to review forecasts and settings."}
            </Typography>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void action(async () => {
                  if (auth.setup_required)
                    await api("/auth/setup", "POST", {
                      password,
                      setup_key: setupKey,
                    });
                  const result = await api<{ csrf: string }>(
                    "/auth/login",
                    "POST",
                    { password },
                  );
                  csrf = result.csrf;
                  setAuth({ setup_required: false, signed_in: true });
                  setPassword("");
                  setSetupKey("");
                  await load();
                });
              }}
            >
              <Stack spacing={2}>
                {auth.setup_required && (
                  <TextField
                    required
                    label="Local setup key"
                    value={setupKey}
                    onChange={(e) => setSetupKey(e.target.value)}
                    autoComplete="off"
                  />
                )}
                <TextField
                  required
                  type="password"
                  label={
                    auth.setup_required
                      ? "New password (12+ characters)"
                      : "Password"
                  }
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete={
                    auth.setup_required ? "new-password" : "current-password"
                  }
                />
                <Button type="submit" variant="contained" disabled={busy}>
                  {auth.setup_required ? "Create administrator" : "Sign in"}
                </Button>
              </Stack>
            </form>
          </Paper>
        ) : (
          <>
            <Box className="page-heading">
              <Box>
                <Typography className="eyebrow">
                  {saved?.name || "WELCOME TO YOUR ENERGY WORKSPACE"}
                </Typography>
                <Typography variant="h3">{tab}</Typography>
              </Box>
              <Stack direction="row" spacing={1}>
                <Chip
                  label={
                    expired
                      ? "Lease expired"
                      : forecast?.status?.replaceAll("_", " ") || "Warming up"
                  }
                  color={ready ? "success" : "default"}
                />
                <Button disabled={busy} onClick={() => void action(load)}>
                  Refresh
                </Button>
              </Stack>
            </Box>
            <Tabs
              value={tab}
              onChange={(_, v) => setTab(v)}
              variant="scrollable"
              scrollButtons="auto"
              sx={{ mb: 3 }}
            >
              {pages.map((p) => (
                <Tab key={p} value={p} label={p} />
              ))}
            </Tabs>
            {!saved && (
              <Alert severity="info" sx={{ mb: 3 }}>
                Start in Setup. Add your site, source mappings, panel geometry,
                battery and tariff. The default fields are examples.
              </Alert>
            )}
            {tab === "Overview" && (
              <>
                <Box className="metrics">
                  <Metric
                    label="Permitted battery export"
                    value={format(
                      ready
                        ? forecast?.export_plan
                            .safe_battery_export_remaining_kwh
                        : 0,
                      "kWh",
                    )}
                    detail={
                      ready
                        ? "Replacement budget for the current lease"
                        : "Export disabled until validation supports readiness"
                    }
                  />
                  <Metric
                    label="PV · next 48 hours"
                    value={format(forecast?.totals?.pv_48h_kwh, "kWh")}
                    detail="AC physical baseline / active model"
                  />
                  <Metric
                    label="Household · next 48 hours"
                    value={format(forecast?.totals?.load_48h_kwh, "kWh")}
                    detail="Gross load, excluding battery charging"
                  />
                  <Metric
                    label="Battery state"
                    value={format(forecast?.export_plan.source_soc_pct, "%")}
                    detail={`Reserve ${format(forecast?.export_plan.required_reserve_kwh, "kWh")} · ${forecast?.export_plan.reason_codes.includes("soc_stale") ? "last report is stale" : "last reported " + (forecast?.export_plan.source_soc_at ? new Date(forecast.export_plan.source_soc_at).toLocaleTimeString() : "unavailable")}`}
                  />
                </Box>
                <Paper sx={{ p: 3, my: 3 }}>
                  <Typography variant="h5">What limits export?</Typography>
                  <Stack spacing={1} sx={{ mt: 2 }}>
                    {(
                      forecast?.export_plan.reason_codes || ["forecast_missing"]
                    ).map((r) => (
                      <Typography key={r}>
                        • {reasons[r] || r.replaceAll("_", " ")}
                      </Typography>
                    ))}
                  </Stack>
                  <Typography
                    variant="body2"
                    color="text.secondary"
                    sx={{ mt: 2 }}
                  >
                    Issued{" "}
                    {forecast?.issued_at
                      ? new Date(forecast.issued_at).toLocaleString()
                      : "not yet"}{" "}
                    · Lease ends{" "}
                    {forecast?.valid_until
                      ? new Date(forecast.valid_until).toLocaleTimeString()
                      : "—"}
                    . Successive budgets replace earlier ones.
                  </Typography>
                </Paper>
                {forecast?.series?.length ? (
                  <Charts
                    forecast={forecast}
                    zone={config.timezone}
                    dark={dark}
                  />
                ) : (
                  <Empty>
                    Charts appear once weather and configured source history are
                    available.
                  </Empty>
                )}
              </>
            )}
            {tab === "Forecasts" && (
              <>
                <Stack direction="row" spacing={2} sx={{ mb: 2 }}>
                  <Select
                    size="small"
                    value={replay}
                    onChange={(e) =>
                      void action(async () => {
                        setReplay(e.target.value);
                        setForecast(
                          await api<Forecast>(
                            e.target.value === "latest"
                              ? base + "/forecast/latest"
                              : base + "/forecasts/" + e.target.value,
                          ),
                        );
                      })
                    }
                  >
                    <MenuItem value="latest">Latest forecast</MenuItem>
                    {archive.map((a) => (
                      <MenuItem key={a.forecast_id} value={a.forecast_id}>
                        {new Date(a.issued_at).toLocaleString()} · {a.status}
                      </MenuItem>
                    ))}
                  </Select>
                </Stack>
                {replay !== "latest" && (
                  <Alert severity="info" sx={{ mb: 2 }}>
                    Archived forecast replay. Original weather and predictions
                    are preserved. This is not a live authorization.
                  </Alert>
                )}
                {forecast?.series?.length ? (
                  <Charts
                    forecast={forecast}
                    zone={config.timezone}
                    dark={dark}
                  />
                ) : (
                  <Empty>No covered forecast intervals yet.</Empty>
                )}
              </>
            )}
            {tab === "Setup" && (
              <SetupFlow
                config={config}
                setConfig={setConfig}
                saved={saved}
                save={save}
                request={api}
                onError={setError}
                onNotice={setNotice}
                busy={busy}
              />
            )}
            {(tab === "Tariff" || tab === "Calendars") && (
              <Stack spacing={3}>
                <Paper sx={{ p: 3 }}>
                  <Typography sx={{ mb: 2 }}>
                    {tab === "Tariff"
                      ? "One default rule covers all dates. Higher priority rules override it; ties are rejected. Zero price and permitted grid charging are separate fields."
                      : "Choose public and school jurisdictions separately. Import official date ranges and explicitly confirm covered years; uncovered years stay unknown."}
                  </Typography>
                  {tab === "Tariff" ? (
                    <TariffForm
                      value={config.tariff}
                      onChange={(tariff) => setConfig({ ...config, tariff })}
                    />
                  ) : (
                    <CalendarForm
                      value={config.calendars}
                      onChange={(calendars) =>
                        setConfig({ ...config, calendars })
                      }
                    />
                  )}
                  <Button
                    variant="contained"
                    disabled={busy}
                    onClick={() => void action(save)}
                  >
                    Save configuration
                  </Button>
                </Paper>
                <Paper sx={{ p: 3 }}>
                  <Stack direction="row" spacing={2}>
                    <TextField
                      type="date"
                      label="Resolved date"
                      value={day}
                      onChange={(e) => setDay(e.target.value)}
                      InputLabelProps={{ shrink: true }}
                    />
                    <Button
                      disabled={!saved}
                      onClick={() =>
                        void action(async () =>
                          setPreview(
                            await api(
                              base +
                                (tab === "Tariff"
                                  ? "/tariff/preview"
                                  : "/calendars/preview") +
                                "?day=" +
                                day,
                            ),
                          ),
                        )
                      }
                    >
                      Preview saved rules
                    </Button>
                  </Stack>
                  {tab === "Tariff" && Array.isArray(preview) && (
                    <TariffTimeline rows={preview} zone={config.timezone} />
                  )}
                  {preview != null && <JsonView value={preview} />}
                </Paper>
                {tab === "Calendars" && (
                  <Paper sx={{ p: 3 }}>
                    <Typography variant="h6">Import calendar file</Typography>
                    <Typography color="text.secondary">
                      Imports are previewed before inclusion in the
                      configuration.
                    </Typography>
                    <CalendarUpload
                      onImported={(v) => setPreview(v)}
                      onError={setError}
                    />
                    {preview != null && (
                      <Button
                        onClick={() => {
                          const c = config.calendars as { [key: string]: Json };
                          setConfig({
                            ...config,
                            calendars: {
                              ...c,
                              events: [
                                ...(Array.isArray(c.events) ? c.events : []),
                                ...((Array.isArray(preview)
                                  ? preview
                                  : []) as Json[]),
                              ],
                            },
                          });
                          setNotice(
                            "Events added to draft. Confirm coverage and save.",
                          );
                        }}
                      >
                        Add imported events to draft
                      </Button>
                    )}
                  </Paper>
                )}
              </Stack>
            )}
            {tab === "Data quality" && (
              <>
                {" "}
                <CoveragePanel />{" "}
                <Paper sx={{ p: 3 }}>
                  <Typography variant="h5">
                    Observation journal and review
                  </Typography>
                  <Typography color="text.secondary" sx={{ my: 2 }}>
                    Raw revisions remain immutable. Reviews change the versioned
                    quality mask; high demand alone is not a fault. Showing the
                    latest 500 records.
                  </Typography>
                  <Button
                    onClick={() =>
                      void action(async () =>
                        setPreview(await api(base + "/composition")),
                      )
                    }
                  >
                    Inspect composed household load
                  </Button>
                  {Array.isArray(preview) && (
                    <QualityOverlay rows={preview} dark={dark} />
                  )}
                  {preview != null && <JsonView value={preview} />}
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          {[
                            "Interval end",
                            "Feature",
                            "Source / epoch",
                            "Value",
                            "Quality",
                            "Review",
                          ].map((h) => (
                            <th key={h}>{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {(
                          data as {
                            id: number;
                            end: string;
                            feature: string;
                            source: string;
                            epoch: string;
                            value: number | null;
                            unit: string;
                            quality: string;
                            reasons: string[];
                          }[]
                        ).map((r) => (
                          <tr key={r.id}>
                            <td>{new Date(r.end).toLocaleString()}</td>
                            <td>{r.feature}</td>
                            <td>
                              {r.source}
                              <small>{r.epoch}</small>
                            </td>
                            <td>
                              {r.value ?? "Missing"} {r.unit}
                            </td>
                            <td>
                              {r.quality}
                              <small>{r.reasons.join(", ")}</small>
                            </td>
                            <td>
                              <Button
                                size="small"
                                onClick={() =>
                                  setReview({
                                    id: r.id,
                                    quality: r.quality,
                                    reason: "",
                                  })
                                }
                              >
                                Review
                              </Button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {!data.length && (
                    <Empty>No measurements have arrived from HA.</Empty>
                  )}
                </Paper>
              </>
            )}
            {tab === "Models" && (
              <Stack spacing={3}>
                <Paper sx={{ p: 3 }}>
                  <Typography variant="h5">Learning and candidates</Typography>
                  <FormControlLabel
                    control={
                      <Switch
                        checked={config.learning_paused}
                        onChange={(_, v) =>
                          setConfig({ ...config, learning_paused: v })
                        }
                      />
                    }
                    label="Pause parameter learning"
                  />
                  <TextField
                    type="number"
                    label="Recent days (7–30)"
                    value={config.recent_days}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        recent_days: Number(e.target.value),
                      })
                    }
                  />
                  <Stack direction="row" spacing={1} sx={{ my: 2 }}>
                    <Button
                      variant="contained"
                      onClick={() => void action(save)}
                    >
                      Save learning settings
                    </Button>
                    <Button
                      disabled={!saved}
                      onClick={() => void action(() => job("train"))}
                    >
                      Train candidate
                    </Button>
                    <Button
                      onClick={() =>
                        void action(async () => {
                          await api(base + "/models/rollback", "POST");
                          setNotice(
                            "Previous validated model restored; plans invalidated.",
                          );
                        })
                      }
                    >
                      Rollback model
                    </Button>
                  </Stack>
                  <Typography color="text.secondary">
                    Main candidates retain seasonal history and use disjoint
                    chronological holdouts. Failed gates keep the active model.
                    Export readiness needs separate evidence.
                  </Typography>
                </Paper>
                {models.length ? (
                  models.map((m) => (
                    <Paper sx={{ p: 3 }} key={String(m.id)}>
                      <JsonView value={m} />
                      <Button
                        onClick={() =>
                          void action(async () => {
                            await api(
                              base + "/models/" + m.id + "/activate",
                              "POST",
                            );
                            setNotice(
                              "Predictive model activated. Export readiness remains separately gated.",
                            );
                          })
                        }
                      >
                        Activate validated candidate
                      </Button>
                    </Paper>
                  ))
                ) : (
                  <Empty>
                    No trained candidates yet. Baselines remain active.
                  </Empty>
                )}
                <Jobs
                  jobs={jobs}
                  onCancel={(id) =>
                    void action(async () => {
                      await api(base + "/jobs/" + id, "DELETE");
                      setJobs(await api(base + "/jobs"));
                    })
                  }
                />
              </Stack>
            )}
            {tab === "Calibration" && (
              <Stack spacing={3}>
                {[
                  "PV geometry and boosted models",
                  "Household CNN and recent branch",
                  "Joint prediction uncertainty",
                  "Passive battery parameters",
                ].map((title, i) => (
                  <Paper sx={{ p: 3 }} key={title}>
                    <Typography variant="h5">{title}</Typography>
                    {i === 0 && (
                      <>
                        <Button
                          onClick={() => void action(() => job("pv_calibrate"))}
                        >
                          Calibrate panel banks
                        </Button>
                        <JsonView value={diagnostics.pv_bank_calibration} />
                      </>
                    )}
                    <Typography color="text.secondary" sx={{ my: 2 }}>
                      {
                        [
                          "Per-bank output uses a physical allocation until independently measured targets support fitting. PV trees require matching archived weather vintages.",
                          "View candidate versus weekday baseline metrics and declared train/calibration/test ranges in Models. Weather features are checked against the calendar-only model using matching issue-time forecasts and disjoint holdouts.",
                          "Interval ranges and whole-horizon risk are separate. Complete paired residual blocks retain temporal and PV/load dependence. Insufficient tail evidence keeps export disabled.",
                          "Configured efficiencies remain active until independently measured AC flows and stored-energy changes identify separate charge/discharge efficiencies. No test cycling is performed.",
                        ][i]
                      }
                    </Typography>
                    {i === 2 && <JsonView value={diagnostics.calibration} />}
                  </Paper>
                ))}
                <Button onClick={() => void action(() => job("calibrate"))}>
                  Review completed outcomes
                </Button>
              </Stack>
            )}
            {tab === "Battery" && (
              <Paper sx={{ p: 3 }}>
                <Typography variant="h5" sx={{ mb: 2 }}>
                  Stored energy and AC power
                </Typography>
                <Box className="fields">
                  {Object.entries(config.battery).map(([key, value]) =>
                    typeof value === "boolean" ? (
                      <FormControlLabel
                        key={key}
                        control={
                          <Switch
                            checked={value}
                            onChange={(_, checked) =>
                              setConfig({
                                ...config,
                                battery: { ...config.battery, [key]: checked },
                              })
                            }
                          />
                        }
                        label={key.replaceAll("_", " ")}
                      />
                    ) : (
                      <TextField
                        key={key}
                        label={key.replaceAll("_", " ")}
                        value={value}
                        type={typeof value === "number" ? "number" : "text"}
                        inputProps={{ step: "any" }}
                        onChange={(e) =>
                          setConfig({
                            ...config,
                            battery: {
                              ...config.battery,
                              [key]:
                                typeof value === "number"
                                  ? Number(e.target.value)
                                  : e.target.value,
                            },
                          })
                        }
                      />
                    ),
                  )}
                </Box>
                <TextField
                  sx={{ my: 3 }}
                  type="number"
                  label="Terminal reserve (kWh)"
                  value={config.terminal_reserve_kwh ?? ""}
                  helperText="Bound the obligation beyond 48 hours. Blank disables readiness."
                  onChange={(e) =>
                    setConfig({
                      ...config,
                      terminal_reserve_kwh:
                        e.target.value === "" ? null : Number(e.target.value),
                    })
                  }
                />
                <Box>
                  <Button variant="contained" onClick={() => void action(save)}>
                    Save battery assumptions
                  </Button>
                </Box>
              </Paper>
            )}
            {tab === "Plan details" && (
              <Paper sx={{ p: 3 }}>
                <Alert severity="info">
                  Shadow simulations are hypothetical advisory results.
                  Permitted export stays zero while readiness is unvalidated.
                </Alert>
                {forecast?.export_plan.shadow_candidate && (
                  <Box className="metrics" sx={{ my: 3 }}>
                    <Metric
                      label="Shadow battery export"
                      value={format(
                        forecast.export_plan.shadow_candidate
                          .battery_export_kwh,
                        "kWh",
                      )}
                    />
                    <Metric
                      label="Natural PV export"
                      value={format(
                        forecast.export_plan.shadow_candidate
                          .natural_pv_export_kwh,
                        "kWh",
                      )}
                    />
                    <Metric
                      label="Paid import shortfall"
                      value={format(
                        forecast.export_plan.shadow_candidate
                          .paid_import_shortfall_kwh,
                        "kWh",
                      )}
                    />
                  </Box>
                )}
                <JsonView value={forecast?.export_plan} />
              </Paper>
            )}
            {tab === "Administration" && (
              <Stack spacing={3}>
                <Paper sx={{ p: 3 }}>
                  <Typography variant="h5">
                    Service health and redacted diagnostics
                  </Typography>
                  <JsonView value={diagnostics} />
                  <Stack direction="row" spacing={1}>
                    <Button
                      disabled={!saved}
                      onClick={() => void action(() => job("weather"))}
                    >
                      Refresh weather
                    </Button>
                    <Button
                      disabled={!saved}
                      onClick={() => void action(() => job("forecast"))}
                    >
                      Refresh forecast
                    </Button>
                    <Button
                      onClick={() =>
                        void action(async () => {
                          const r = await api<{ download: string }>(
                            base + "/backup",
                            "POST",
                          );
                          window.location.href = r.download;
                        })
                      }
                    >
                      Download backup
                    </Button>
                    <Button
                      onClick={() => {
                        const blob = new Blob(
                          [JSON.stringify(diagnostics, null, 2)],
                          { type: "application/json" },
                        );
                        const url = URL.createObjectURL(blob);
                        const a = document.createElement("a");
                        a.href = url;
                        a.download = "redacted-diagnostics.json";
                        a.click();
                        URL.revokeObjectURL(url);
                      }}
                    >
                      Download diagnostics
                    </Button>
                  </Stack>
                </Paper>
                <Tokens />
                <Jobs
                  jobs={jobs}
                  onCancel={(id) =>
                    void action(async () => {
                      await api(base + "/jobs/" + id, "DELETE");
                      setJobs(await api(base + "/jobs"));
                    })
                  }
                />
              </Stack>
            )}
          </>
        )}
        <Typography variant="caption" color="text.secondary" component="footer">
          Energy Forecast 0.1 · Forecasting runs outside Home Assistant. Battery
          control remains with your HA automations.
        </Typography>
        <Dialog open={!!review} onClose={() => setReview(null)}>
          <DialogTitle>Review observation {review?.id}</DialogTitle>
          <DialogContent>
            <Stack spacing={2} sx={{ pt: 1 }}>
              <Select
                value={review?.quality || "suspect"}
                onChange={(e) =>
                  review && setReview({ ...review, quality: e.target.value })
                }
              >
                {["valid", "suspect", "invalid"].map((q) => (
                  <MenuItem key={q} value={q}>
                    {q}
                  </MenuItem>
                ))}
              </Select>
              <TextField
                label="Reason (required)"
                value={review?.reason || ""}
                onChange={(e) =>
                  review && setReview({ ...review, reason: e.target.value })
                }
              />
            </Stack>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setReview(null)}>Cancel</Button>
            <Button
              disabled={!review || review.reason.length < 3}
              onClick={() =>
                void action(async () => {
                  await api(
                    base + "/observations/" + review!.id + "/review",
                    "PUT",
                    { quality: review!.quality, reason: review!.reason },
                  );
                  setReview(null);
                  setData(await api(base + "/observations?limit=500"));
                })
              }
            >
              Save review
            </Button>
          </DialogActions>
        </Dialog>
      </main>
    </ThemeProvider>
  );
}
function Jobs({
  jobs,
  onCancel,
}: {
  jobs: Record<string, unknown>[];
  onCancel: (id: string) => void;
}) {
  return (
    <Paper sx={{ p: 3 }}>
      <Typography variant="h5">Background jobs</Typography>
      {jobs.length ? (
        jobs.map((j) => (
          <Box
            key={String(j.id)}
            sx={{ py: 1, borderBottom: "1px solid", borderColor: "divider" }}
          >
            <Stack direction="row" spacing={2} alignItems="center">
              <Typography>{String(j.kind)}</Typography>
              <Chip size="small" label={String(j.status)} />
              <Typography variant="caption">
                {Math.round(Number(j.progress) * 100)}%
              </Typography>
              {["queued", "running"].includes(String(j.status)) && (
                <Button onClick={() => onCancel(String(j.id))}>Cancel</Button>
              )}
            </Stack>
            {j.reason != null && (
              <Typography color="text.secondary">{String(j.reason)}</Typography>
            )}
            {j.result != null && <JsonView value={j.result} />}
          </Box>
        ))
      ) : (
        <Empty>No jobs yet.</Empty>
      )}
    </Paper>
  );
}
function Tokens() {
  const [tokens, setTokens] = useState<
    { id: string; scope: string; revoked: boolean }[]
  >([]);
  const [error, setError] = useState("");
  useEffect(() => {
    api<typeof tokens>(base + "/tokens")
      .then(setTokens)
      .catch((e) => setError(e.message));
  }, []);
  return (
    <Paper sx={{ p: 3 }}>
      <Typography variant="h5">Scoped access tokens</Typography>
      {error && <Alert severity="error">{error}</Alert>}
      {tokens.map((t) => (
        <Stack key={t.id} direction="row" spacing={2} sx={{ my: 1 }}>
          <Typography>
            {t.id} · {t.scope} · {t.revoked ? "revoked" : "active"}
          </Typography>
          {!t.revoked && (
            <Button
              onClick={() =>
                void api(base + "/tokens/" + t.id, "DELETE")
                  .then(() => api<typeof tokens>(base + "/tokens"))
                  .then(setTokens)
                  .catch((e) => setError(e.message))
              }
            >
              Revoke
            </Button>
          )}
        </Stack>
      ))}
    </Paper>
  );
}
function CalendarUpload({
  onImported,
  onError,
}: {
  onImported: (v: unknown) => void;
  onError: (e: string) => void;
}) {
  const [format, setFormat] = useState("ics");
  const [kind, setKind] = useState("school_holiday");
  const [source, setSource] = useState("Official education department");
  const [version, setVersion] = useState("2026");
  const [content, setContent] = useState("");
  return (
    <Stack spacing={2} sx={{ mt: 2 }}>
      <Select value={format} onChange={(e) => setFormat(e.target.value)}>
        {["json", "csv", "ics"].map((f) => (
          <MenuItem key={f} value={f}>
            {f.toUpperCase()}
          </MenuItem>
        ))}
      </Select>
      <Select value={kind} onChange={(e) => setKind(e.target.value)}>
        {["school_holiday", "public_holiday", "household", "pupil_free"].map(
          (f) => (
            <MenuItem key={f} value={f}>
              {f.replaceAll("_", " ")}
            </MenuItem>
          ),
        )}
      </Select>
      <TextField
        label="Source / URL"
        value={source}
        onChange={(e) => setSource(e.target.value)}
      />
      <TextField
        label="Dataset version"
        value={version}
        onChange={(e) => setVersion(e.target.value)}
      />
      <Button component="label" variant="outlined">
        Choose calendar file
        <input
          type="file"
          hidden
          accept=".ics,.csv,.json"
          onChange={async (e) => {
            const file = e.target.files?.[0];
            if (file) {
              setContent(await file.text());
              setFormat(
                file.name.toLowerCase().endsWith(".csv")
                  ? "csv"
                  : file.name.toLowerCase().endsWith(".json")
                    ? "json"
                    : "ics",
              );
            }
          }}
        />
      </Button>
      {content && (
        <Typography variant="body2">Calendar file ready to preview.</Typography>
      )}
      <Button
        onClick={() =>
          void api(base + "/calendars/import", "POST", {
            format,
            kind,
            source,
            version,
            content,
          })
            .then(onImported)
            .catch((e) => onError(e.message))
        }
      >
        Validate import
      </Button>
    </Stack>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
