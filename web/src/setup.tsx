import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  MenuItem,
  Paper,
  Stack,
  Stepper,
  Step,
  StepLabel,
  TextField,
  Typography,
} from "@mui/material";
import * as L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { Config } from "./types";
import { InverterForm, MappingForm, type RecordValue } from "./forms";
export type HAInfo = {
  url?: string;
  pairing: string;
  integration_token?: string;
  connected_at: string;
  home: { name: string; latitude: number; longitude: number; timezone: string };
  sources: RecordValue[];
};
type Request = (path: string, method?: string, data?: unknown) => Promise<any>;
export function LocationMap({
  latitude,
  longitude,
  onMove,
}: {
  latitude: number;
  longitude: number;
  onMove: (lat: number, lon: number) => void;
}) {
  const node = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const marker = useRef<L.CircleMarker | null>(null);
  const callback = useRef(onMove);
  callback.current = onMove;
  useEffect(() => {
    if (!node.current) return;
    const m = L.map(node.current, { scrollWheelZoom: false }).setView(
      [latitude, longitude],
      15,
    );
    map.current = m;
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(m);
    marker.current = L.circleMarker([latitude, longitude], {
      radius: 10,
      color: "#164d3d",
      weight: 3,
      fillColor: "#73cdb1",
      fillOpacity: 1,
    }).addTo(m);
    m.on("click", (e: L.LeafletMouseEvent) =>
      callback.current(e.latlng.lat, e.latlng.lng),
    );
    const observer = new ResizeObserver(() => m.invalidateSize());
    observer.observe(node.current);
    return () => {
      observer.disconnect();
      m.remove();
      map.current = null;
    };
  }, []);
  useEffect(() => {
    if (Number.isFinite(latitude) && Number.isFinite(longitude)) {
      marker.current?.setLatLng([latitude, longitude]);
      map.current?.panTo([latitude, longitude]);
    }
  }, [latitude, longitude]);
  return (
    <Box
      ref={node}
      aria-label="Home location map"
      sx={{
        height: 340,
        width: "100%",
        borderRadius: 2,
        overflow: "hidden",
        my: 2,
        zIndex: 0,
      }}
    />
  );
}
function SourceSelect({
  label,
  value,
  sources,
  onChange,
  units,
  context,
}: {
  label: string;
  value: string;
  sources: RecordValue[];
  onChange: (v: string) => void;
  units?: string[];
  context?: string;
}) {
  return (
    <TextField
      select
      fullWidth
      label={label}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <MenuItem value="">Choose a Home Assistant sensor</MenuItem>
      {sources
        .filter(
          (s) =>
            (!units || units.includes(s.unit)) &&
            (!context || !s.contexts || s.contexts.includes(context)),
        )
        .map((s) => (
          <MenuItem key={s.source} value={s.source}>
            {s.name} · {s.unit} · {s.source}
            {s.inactive ? " · historical" : ""}
          </MenuItem>
        ))}
      {value && !sources.some((s) => s.source === value) && (
        <MenuItem value={value}>{value}</MenuItem>
      )}
    </TextField>
  );
}
export function SetupFlow({
  config,
  setConfig,
  saved,
  save,
  request,
  onError,
  onNotice,
  busy,
}: {
  config: Config;
  setConfig: (c: Config) => void;
  saved: Config | null;
  save: () => Promise<void>;
  request: Request;
  onError: (e: string) => void;
  onNotice: (e: string) => void;
  busy: boolean;
}) {
  const [step, setStep] = useState(0);
  const [ha, setHA] = useState<HAInfo | null>(null);
  const [url, setUrl] = useState("http://homeassistant.local:8123");
  const [access, setAccess] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  useEffect(() => {
    request("/v1/sites/home/home-assistant")
      .then((value: HAInfo | null) => {
        setHA(value);
        if (value?.url) setUrl(value.url);
        if (value && !saved)
          setConfig({
            ...config,
            name: value.home.name,
            latitude: value.home.latitude,
            longitude: value.home.longitude,
            timezone: value.home.timezone,
          });
      })
      .catch((e) => onError(e.message));
  }, []);
  async function connect() {
    setConnecting(true);
    try {
      const info = await request(
        "/v1/sites/home/home-assistant/connect",
        "POST",
        { url, access_token: access },
      );
      setAccess("");
      setHA(info);
      if (!saved)
        setConfig({
          ...config,
          name: info.home.name,
          latitude: info.home.latitude,
          longitude: info.home.longitude,
          timezone: info.home.timezone,
        });
      setStep(1);
      onNotice(
        "Home Assistant connected. Your home location is ready to review.",
      );
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setConnecting(false);
    }
  }
  const sources = ha?.sources || [];
  const mappings = config.mappings as RecordValue[];
  const selected = (feature: string) =>
    mappings.find((m) => m.feature === feature)?.sources?.[0]?.source || "";
  const assign = (feature: string, source: string) => {
    const remaining = mappings.filter((m) => m.feature !== feature);
    setConfig({
      ...config,
      mappings: source
        ? [
            ...remaining,
            { feature, mode: "stitch", sources: [{ source, priority: 0 }] },
          ]
        : remaining,
    });
  };
  async function finish() {
    await save();
    const inputs = [];
    for (const mapping of config.mappings as RecordValue[]) {
      for (const entry of mapping.sources || []) {
        const meta = sources.find((s) => s.source === entry.source);
        if (!meta) continue;
        const bank = config.banks.find((b) => b.target === mapping.feature);
        inputs.push({
          source: entry.source,
          feature: mapping.feature,
          unit: meta.unit,
          kind: meta.kind,
          boundary: bank
            ? "unknown"
            : mapping.feature === "battery_soc"
              ? "stored"
              : "AC",
          epoch: "commissioned-v1",
          history: mapping.feature !== "battery_soc",
          history_period: "hour",
        });
      }
    }
    await request("/v1/sites/home/bridge/inputs", "PUT", inputs);
    onNotice(
      "Setup saved. The HA bridge will collect the selected sensors and history.",
    );
  }
  const steps = [
    "Connect HA",
    "Home location",
    "Household sensors",
    "Solar banks",
    "Review & save",
  ];
  return (
    <Stack spacing={3}>
      <Paper sx={{ p: { xs: 2, md: 3 } }}>
        <Typography variant="h5" sx={{ mb: 1 }}>
          Set up your household
        </Typography>
        <Typography color="text.secondary">
          Connect Home Assistant first, then review your location and select the
          measurements you want to use.
        </Typography>
        <Stepper
          activeStep={step}
          alternativeLabel
          sx={{ my: 3, overflowX: "auto" }}
        >
          {steps.map((label, i) => (
            <Step
              key={label}
              onClick={() => {
                if (i === 0 || ha) setStep(i);
              }}
              sx={{ cursor: "pointer", minWidth: 80 }}
            >
              <StepLabel>{label}</StepLabel>
            </Step>
          ))}
        </Stepper>
        {step === 0 && (
          <Stack spacing={2}>
            <Typography variant="h6">Connect to Home Assistant</Typography>
            <TextField
              label="Home Assistant URL"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="http://homeassistant.local:8123"
            />
            <TextField
              label="Home Assistant access token"
              type="password"
              value={access}
              onChange={(e) => setAccess(e.target.value)}
              autoComplete="off"
              helperText="Use a long-lived token from your HA profile. It is used for this connection and is not stored by the service."
            />
            <Button
              variant="contained"
              disabled={connecting || !access || !url}
              onClick={() => void connect()}
            >
              {connecting ? "Connecting…" : "Connect Home Assistant"}
            </Button>
            {ha && (
              <Alert severity="success">
                Connected to {ha.home.name}
                {ha.pairing === "paired" ? " · HACS bridge paired" : ""}
              </Alert>
            )}
            {ha && ha.pairing !== "paired" && (
              <Alert severity="info">
                Install{" "}
                <a
                  href="https://github.com/funkmaster-dan/ha-energy-forecast"
                  target="_blank"
                  rel="noreferrer"
                >
                  Energy Forecast through HACS
                </a>
                , then use this service’s URL and integration token to pair it.
                {ha.integration_token && (
                  <TextField
                    fullWidth
                    sx={{ mt: 2 }}
                    label="Integration token"
                    value={ha.integration_token}
                    inputProps={{ readOnly: true }}
                  />
                )}
              </Alert>
            )}
            {ha && (
              <Button onClick={() => setStep(1)}>
                Continue with connected home
              </Button>
            )}
          </Stack>
        )}
        {step === 1 && (
          <>
            <Typography variant="h6">Your home location</Typography>
            <Typography color="text.secondary">
              The initial location comes from Home Assistant. Click the map to
              adjust it.
            </Typography>
            <LocationMap
              latitude={config.latitude}
              longitude={config.longitude}
              onMove={(latitude, longitude) =>
                setConfig({ ...config, latitude, longitude })
              }
            />
            <Box className="fields">
              {(["name", "timezone", "latitude", "longitude"] as const).map(
                (key) => (
                  <TextField
                    key={key}
                    label={
                      {
                        name: "Home name",
                        timezone: "Time zone",
                        latitude: "Latitude",
                        longitude: "Longitude",
                      }[key]
                    }
                    value={config[key]}
                    type={
                      key === "latitude" || key === "longitude"
                        ? "number"
                        : "text"
                    }
                    inputProps={{ step: "any" }}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        [key]:
                          key === "latitude" || key === "longitude"
                            ? Number(e.target.value)
                            : e.target.value,
                      })
                    }
                  />
                ),
              )}
            </Box>
            {ha && (
              <Button
                sx={{ mt: 2 }}
                onClick={() =>
                  setConfig({
                    ...config,
                    latitude: ha.home.latitude,
                    longitude: ha.home.longitude,
                    timezone: ha.home.timezone,
                  })
                }
              >
                Use Home Assistant location
              </Button>
            )}
          </>
        )}
        {step === 2 && (
          <Stack spacing={2}>
            <Typography variant="h6">Household measurements</Typography>
            <Typography color="text.secondary">
              Choose gross household consumption excluding battery charging, and
              the current battery state of charge.
            </Typography>
            <SourceSelect
              context="household_load"
              label="Household consumption sensor"
              value={selected("household_load")}
              sources={sources}
              units={["W", "kW", "Wh", "kWh"]}
              onChange={(v) => assign("household_load", v)}
            />
            <SourceSelect
              context="battery_soc"
              label="Battery state of charge sensor"
              value={selected("battery_soc")}
              sources={sources}
              units={["%"]}
              onChange={(v) => assign("battery_soc", v)}
            />
            <Button onClick={() => setAdvanced(!advanced)}>
              {advanced ? "Hide" : "Show"} source composition options
            </Button>
            {advanced && (
              <MappingForm
                value={config.mappings}
                sources={sources}
                onChange={(mappings) => setConfig({ ...config, mappings })}
              />
            )}
          </Stack>
        )}
        {step === 3 && (
          <Stack spacing={2}>
            <Typography variant="h6">Solar panel banks</Typography>
            <Typography color="text.secondary">
              Select each bank’s generation sensor and enter its tilt and
              direction. Generation history and Open-Meteo irradiance will
              calibrate its output scale automatically.
            </Typography>
            {config.banks.map((bank, i) => (
              <Paper key={bank.id} variant="outlined" sx={{ p: 2 }}>
                <Stack
                  direction="row"
                  justifyContent="space-between"
                  alignItems="center"
                >
                  <Typography variant="h6">
                    {bank.name || `Bank ${i + 1}`}
                  </Typography>
                  <Button
                    onClick={() =>
                      setConfig({
                        ...config,
                        banks: config.banks.filter((b) => b.id !== bank.id),
                        mappings: config.mappings.filter(
                          (m: any) => m.feature !== bank.target,
                        ),
                      })
                    }
                  >
                    Remove bank
                  </Button>
                </Stack>
                <Box className="fields" sx={{ my: 2 }}>
                  <TextField
                    label="Bank name (optional)"
                    value={bank.name || ""}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        banks: config.banks.map((b) =>
                          b.id === bank.id ? { ...b, name: e.target.value } : b,
                        ),
                      })
                    }
                  />
                  <TextField
                    type="number"
                    label="Tilt · degrees"
                    value={bank.tilt}
                    inputProps={{ min: 0, max: 90 }}
                    helperText="0° is flat; 90° is vertical"
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        banks: config.banks.map((b) =>
                          b.id === bank.id
                            ? { ...b, tilt: Number(e.target.value) }
                            : b,
                        ),
                      })
                    }
                  />
                  <TextField
                    type="number"
                    label="Azimuth · degrees"
                    value={bank.azimuth}
                    inputProps={{ min: 0, max: 359 }}
                    helperText="North 0° · east 90° · south 180° · west 270°"
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        banks: config.banks.map((b) =>
                          b.id === bank.id
                            ? { ...b, azimuth: Number(e.target.value) }
                            : b,
                        ),
                      })
                    }
                  />
                </Box>
                <SourceSelect
                  context="pv_generation"
                  label="Bank generation sensor"
                  value={selected(bank.target || `pv_bank_${bank.id}`)}
                  sources={sources}
                  units={["W", "kW", "Wh", "kWh"]}
                  onChange={(v) =>
                    assign(bank.target || `pv_bank_${bank.id}`, v)
                  }
                />
                <Chip
                  label="Output scale learned from history"
                  size="small"
                  sx={{ mt: 2 }}
                />
              </Paper>
            ))}
            <Button
              onClick={() => {
                let n = 1;
                while (config.banks.some((b) => b.id === `bank_${n}`)) n++;
                setConfig({
                  ...config,
                  banks: [
                    ...config.banks,
                    {
                      id: `bank_${n}`,
                      name: `Bank ${n}`,
                      tilt: 25,
                      azimuth: 0,
                      target: `pv_bank_bank_${n}`,
                      inverter_group: "main",
                      conversion_efficiency: 1,
                      dc_kwp: null,
                    },
                  ],
                });
              }}
            >
              Add panel bank
            </Button>
            <Button onClick={() => setAdvanced(!advanced)}>
              {advanced ? "Hide" : "Show"} known inverter limits
            </Button>
            {advanced && (
              <InverterForm
                value={
                  (config.inverter_limits_kw || {}) as Record<string, number>
                }
                onChange={(v) =>
                  setConfig({ ...config, inverter_limits_kw: v })
                }
              />
            )}
          </Stack>
        )}
        {step === 4 && (
          <Stack spacing={2}>
            <Typography variant="h6">Ready to collect your history</Typography>
            <Typography>
              {config.name} · {config.timezone}
            </Typography>
            <Typography>
              {config.banks.length} solar banks · {config.mappings.length}{" "}
              measurement profiles
            </Typography>
            <Typography color="text.secondary">
              Review battery assumptions, tariff periods and calendars in their
              dedicated screens. Calibration will show progress as history
              becomes available.
            </Typography>
            <Button
              variant="contained"
              disabled={busy}
              onClick={() => void finish().catch((e) => onError(e.message))}
            >
              Save household setup
            </Button>
          </Stack>
        )}
        <Stack direction="row" justifyContent="space-between" sx={{ mt: 3 }}>
          <Button disabled={step === 0} onClick={() => setStep(step - 1)}>
            Back
          </Button>
          {step > 0 && step < 4 && (
            <Button variant="contained" onClick={() => setStep(step + 1)}>
              Continue
            </Button>
          )}
        </Stack>
      </Paper>
    </Stack>
  );
}
