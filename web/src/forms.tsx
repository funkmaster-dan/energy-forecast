import {
  Box,
  Button,
  Checkbox,
  FormControlLabel,
  MenuItem,
  Paper,
  Stack,
  TextField,
  Typography,
  Chip,
} from "@mui/material";
import type { Json } from "./types";
export type RecordValue = Record<string, any>;
function NumberField({
  label,
  value,
  onChange,
  optional = false,
}: {
  label: string;
  value: unknown;
  onChange: (v: number | null) => void;
  optional?: boolean;
}) {
  return (
    <TextField
      label={label}
      type="number"
      value={value ?? ""}
      inputProps={{ step: "any" }}
      onChange={(e) =>
        onChange(
          e.target.value === "" && optional ? null : Number(e.target.value),
        )
      }
    />
  );
}
export function TariffForm({
  value,
  onChange,
}: {
  value: Json[];
  onChange: (v: Json[]) => void;
}) {
  const rows = value as RecordValue[];
  const change = (i: number, key: string, v: unknown) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, [key]: v } : r)));
  return (
    <Stack spacing={2}>
      {rows.map((r, i) => (
        <Paper key={i} variant="outlined" sx={{ p: 2 }}>
          <Stack direction="row" justifyContent="space-between">
            <Typography variant="h6">{r.name || `Period ${i + 1}`}</Typography>
            <Button
              onClick={() => onChange(rows.filter((_, j) => j !== i))}
              disabled={rows.length === 1}
            >
              Remove period
            </Button>
          </Stack>
          <Box className="fields" sx={{ my: 2 }}>
            <TextField
              label="Period name"
              value={r.name || ""}
              onChange={(e) => change(i, "name", e.target.value)}
            />
            <TextField
              label="Starts"
              type="time"
              value={String(r.start || "00:00").slice(0, 5)}
              onChange={(e) => change(i, "start", e.target.value)}
              InputLabelProps={{ shrink: true }}
            />
            <TextField
              label="Ends"
              type="time"
              value={String(r.end || "00:00").slice(0, 5)}
              onChange={(e) => change(i, "end", e.target.value)}
              InputLabelProps={{ shrink: true }}
            />
            <NumberField
              label="Import · AUD/kWh"
              value={r.import_rate}
              onChange={(v) => change(i, "import_rate", v)}
            />
            <NumberField
              label="Export · AUD/kWh"
              value={r.export_rate || 0}
              onChange={(v) => change(i, "export_rate", v)}
            />
            <NumberField
              label="Priority"
              value={r.priority || 0}
              onChange={(v) => change(i, "priority", v)}
            />
            <NumberField
              optional
              label="Export limit · kW"
              value={r.export_kw}
              onChange={(v) => change(i, "export_kw", v)}
            />
            <NumberField
              optional
              label="Import limit · kW"
              value={r.import_kw}
              onChange={(v) => change(i, "import_kw", v)}
            />
            <NumberField
              optional
              label="Window export cap · kWh"
              value={r.export_cap_kwh}
              onChange={(v) => change(i, "export_cap_kwh", v)}
            />
          </Box>
          <Typography variant="caption">Days</Typography>
          <Stack direction="row" flexWrap="wrap" gap={1} sx={{ my: 1 }}>
            {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d, n) => (
              <Chip
                key={d}
                label={d}
                color={
                  (r.weekdays || [0, 1, 2, 3, 4, 5, 6]).includes(n)
                    ? "primary"
                    : "default"
                }
                onClick={() =>
                  change(
                    i,
                    "weekdays",
                    (r.weekdays || [0, 1, 2, 3, 4, 5, 6]).includes(n)
                      ? (r.weekdays || [0, 1, 2, 3, 4, 5, 6]).filter(
                          (x: number) => x !== n,
                        )
                      : [...(r.weekdays || []), n],
                  )
                }
              />
            ))}
          </Stack>
          <Typography variant="caption">Season months</Typography>
          <Stack direction="row" flexWrap="wrap" gap={1} sx={{ my: 1 }}>
            {[
              "Jan",
              "Feb",
              "Mar",
              "Apr",
              "May",
              "Jun",
              "Jul",
              "Aug",
              "Sep",
              "Oct",
              "Nov",
              "Dec",
            ].map((m, n) => (
              <Chip
                key={m}
                label={m}
                color={
                  (
                    r.months || [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
                  ).includes(n + 1)
                    ? "primary"
                    : "default"
                }
                onClick={() => {
                  const months = r.months || [
                    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
                  ];
                  change(
                    i,
                    "months",
                    months.includes(n + 1)
                      ? months.filter((x: number) => x !== n + 1)
                      : [...months, n + 1],
                  );
                }}
              />
            ))}
          </Stack>
          <TextField
            fullWidth
            sx={{ my: 1 }}
            label="Specific dates (optional, separated by commas)"
            placeholder="2026-12-25, 2026-12-26"
            value={(r.dates || []).join(", ")}
            onChange={(e) =>
              change(
                i,
                "dates",
                e.target.value
                  .split(",")
                  .map((v) => v.trim())
                  .filter(Boolean),
              )
            }
          />
          <FormControlLabel
            control={
              <Checkbox
                checked={!!r.grid_charge_allowed}
                onChange={(_, v) => change(i, "grid_charge_allowed", v)}
              />
            }
            label="Permit grid charging"
          />
          <FormControlLabel
            control={
              <Checkbox
                checked={r.export_allowed !== false}
                onChange={(_, v) => change(i, "export_allowed", v)}
              />
            }
            label="Permit export"
          />
          <FormControlLabel
            control={
              <Checkbox
                checked={r.reexport_grid_energy !== false}
                onChange={(_, v) => change(i, "reexport_grid_energy", v)}
              />
            }
            label="Permit re-export of grid energy"
          />
        </Paper>
      ))}
      <Button
        onClick={() =>
          onChange([
            ...rows,
            {
              name: `Period ${rows.length + 1}`,
              start: "16:00",
              end: "21:00",
              priority: 1,
              import_rate: 0.3,
              export_rate: 0.1,
            },
          ])
        }
      >
        Add tariff period
      </Button>
    </Stack>
  );
}
export function CalendarForm({
  value,
  onChange,
}: {
  value: Json;
  onChange: (v: Json) => void;
}) {
  const c = value as RecordValue;
  const set = (key: string, v: unknown) => onChange({ ...c, [key]: v });
  const events = (c.events || []) as RecordValue[];
  return (
    <Stack spacing={2}>
      <FormControlLabel
        control={
          <Checkbox
            checked={!!c.enabled}
            onChange={(_, v) => set("enabled", v)}
          />
        }
        label="Use holiday and household calendars"
      />
      <Box className="fields">
        {[
          ["public_jurisdiction", "Public holiday state"],
          ["school_jurisdiction", "School calendar state"],
        ].map(([key, label]) => (
          <TextField
            key={key}
            select
            label={label}
            value={c[key] || "SA"}
            onChange={(e) => set(key, e.target.value)}
          >
            {["ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"].map((s) => (
              <MenuItem key={s} value={s}>
                {s}
              </MenuItem>
            ))}
          </TextField>
        ))}
        <TextField
          label="Region / school division"
          value={c.region || ""}
          onChange={(e) => set("region", e.target.value || null)}
        />
        <TextField
          label="Reviewed years (comma separated)"
          value={(c.covered_years || []).join(", ")}
          onChange={(e) =>
            set(
              "covered_years",
              e.target.value
                .split(",")
                .map((v) => Number(v.trim()))
                .filter((v) => v > 1900),
            )
          }
        />
        <TextField
          label="School profiles (comma separated)"
          value={(c.school_profiles || ["government"]).join(", ")}
          onChange={(e) =>
            set(
              "school_profiles",
              e.target.value
                .split(",")
                .map((v) => v.trim())
                .filter(Boolean),
            )
          }
        />
        <TextField
          select
          label="Combine school calendars"
          value={c.school_combination || "any"}
          onChange={(e) => set("school_combination", e.target.value)}
        >
          <MenuItem value="any">Any profile is on holiday</MenuItem>
          <MenuItem value="all">All profiles are on holiday</MenuItem>
        </TextField>
      </Box>
      {events.map((event, i) => (
        <Paper variant="outlined" key={i} sx={{ p: 2 }}>
          <Box className="fields">
            {[
              ["label", "Event"],
              ["start", "Start date"],
              ["end", "End date (exclusive)"],
              ["source", "Source"],
              ["version", "Version"],
              ["profile", "School profile"],
            ].map(([key, label]) => (
              <TextField
                key={key}
                label={label}
                type={key === "start" || key === "end" ? "date" : "text"}
                value={event[key] || ""}
                InputLabelProps={{ shrink: true }}
                onChange={(e) =>
                  set(
                    "events",
                    events.map((r, j) =>
                      j === i ? { ...r, [key]: e.target.value } : r,
                    ),
                  )
                }
              />
            ))}
            <TextField
              type="datetime-local"
              label="First known at · UTC"
              InputLabelProps={{ shrink: true }}
              value={event.known_at ? String(event.known_at).slice(0, 16) : ""}
              helperText="Used to keep later schedules out of historical validation."
              onChange={(e) =>
                set(
                  "events",
                  events.map((r, j) =>
                    j === i
                      ? {
                          ...r,
                          known_at: e.target.value
                            ? e.target.value + ":00Z"
                            : null,
                        }
                      : r,
                  ),
                )
              }
            />
            <TextField
              select
              label="Event type"
              value={event.kind || "household"}
              onChange={(e) =>
                set(
                  "events",
                  events.map((r, j) =>
                    j === i ? { ...r, kind: e.target.value } : r,
                  ),
                )
              }
            >
              {[
                "school_holiday",
                "public_holiday",
                "household",
                "pupil_free",
              ].map((k) => (
                <MenuItem value={k} key={k}>
                  {k.replaceAll("_", " ")}
                </MenuItem>
              ))}
            </TextField>
          </Box>
          <Button
            onClick={() =>
              set(
                "events",
                events.filter((_, j) => j !== i),
              )
            }
          >
            Remove event
          </Button>
        </Paper>
      ))}
      <Button
        onClick={() =>
          set("events", [
            ...events,
            {
              start: new Date().toISOString().slice(0, 10),
              end: new Date(Date.now() + 86400000).toISOString().slice(0, 10),
              label: "",
              kind: "household",
              source: "Household",
              version: "1",
              profile: "government",
              known_at: new Date().toISOString(),
            },
          ])
        }
      >
        Add calendar event
      </Button>
    </Stack>
  );
}
export function MappingForm({
  value,
  onChange,
  sources = [],
}: {
  value: Json[];
  onChange: (v: Json[]) => void;
  sources?: RecordValue[];
}) {
  const rows = value as RecordValue[];
  const set = (i: number, key: string, v: unknown) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, [key]: v } : r)));
  return (
    <Stack spacing={2}>
      {rows.map((r, i) => (
        <Paper variant="outlined" key={i} sx={{ p: 2 }}>
          <Box className="fields">
            <TextField
              select
              label="Measurement profile"
              value={r.feature}
              onChange={(e) => set(i, "feature", e.target.value)}
            >
              {Array.from(
                new Set([
                  "household_load",
                  "battery_soc",
                  "pv_generation",
                  "pv_generation_energy",
                  "battery_charge",
                  "battery_discharge",
                  "battery_stored_energy",
                  "export_limit",
                  "outdoor_temperature",
                  ...rows.map((row) => row.feature),
                ]),
              ).map((feature) => (
                <MenuItem key={feature} value={feature}>
                  {(
                    {
                      household_load: "Household consumption",
                      battery_soc: "Battery state of charge",
                      pv_generation: "Post-inverter solar power",
                      pv_generation_energy: "Solar energy counter",
                      battery_charge: "Battery AC charging",
                      battery_discharge: "Battery AC discharge",
                      battery_stored_energy:
                        "Independent stored battery energy",
                      export_limit: "Live export limit",
                      outdoor_temperature: "Outdoor temperature",
                    } as Record<string, string>
                  )[feature] || feature.replaceAll("_", " ")}
                </MenuItem>
              ))}
            </TextField>
            <TextField
              select
              label="Combine sources"
              value={r.mode || "stitch"}
              onChange={(e) => set(i, "mode", e.target.value)}
            >
              {[
                ["stitch", "Replacement meters (stitch)"],
                ["fallback", "Redundant meters (fallback)"],
                ["sum", "Independent components (sum)"],
                ["derived", "Energy balance (derived)"],
              ].map(([k, label]) => (
                <MenuItem key={k} value={k}>
                  {label}
                </MenuItem>
              ))}
            </TextField>
            <NumberField
              label="Maximum expected power · kW"
              value={r.maximum_kw || 50}
              onChange={(v) => set(i, "maximum_kw", v)}
            />
          </Box>
          {(r.sources || []).map((source: RecordValue, n: number) => {
            const update = (key: string, v: unknown) =>
              set(
                i,
                "sources",
                r.sources.map((s: RecordValue, j: number) =>
                  j === n ? { ...s, [key]: v } : s,
                ),
              );
            return (
              <Box key={n} className="fields" sx={{ my: 2 }}>
                <TextField
                  label="HA source"
                  select={sources.length > 0}
                  value={source.source || ""}
                  onChange={(e) => update("source", e.target.value)}
                >
                  {sources
                    .filter((s) => {
                      const feature = String(r.feature || "");
                      if (feature === "battery_stored_energy")
                        return (
                          ["Wh", "kWh"].includes(s.unit) &&
                          s.kind === "state" &&
                          s.contexts?.includes("battery_stored_energy")
                        );
                      if (feature === "battery_soc")
                        return (
                          s.unit === "%" &&
                          (!s.contexts || s.contexts.includes("battery_soc"))
                        );
                      if (
                        feature === "household_load" ||
                        feature.startsWith("pv_")
                      ) {
                        if (!["W", "kW", "Wh", "kWh"].includes(s.unit))
                          return false;
                        const context =
                          feature === "household_load"
                            ? "household_load"
                            : "pv_generation";
                        return (
                          r.mode === "derived" ||
                          (r.mode === "sum" &&
                            context === "household_load" &&
                            s.contexts?.includes("power_energy_other")) ||
                          !s.contexts ||
                          s.contexts.includes(context)
                        );
                      }
                      return true;
                    })
                    .map((s) => (
                      <MenuItem value={s.source} key={s.source}>
                        {s.name} · {s.unit} · {s.source}{" "}
                        {s.inactive ? "(history)" : ""}
                      </MenuItem>
                    ))}
                </TextField>
                <TextField
                  select
                  label="Measurement boundary"
                  value={source.measurement_boundary || ""}
                  onChange={(e) =>
                    update("measurement_boundary", e.target.value || null)
                  }
                >
                  <MenuItem value="">Use profile default</MenuItem>
                  {["AC", "DC", "stored", "environment", "unknown"].map((v) => (
                    <MenuItem key={v} value={v}>
                      {v}
                    </MenuItem>
                  ))}
                </TextField>
                <TextField
                  label="Meter epoch (optional)"
                  value={source.epoch || ""}
                  helperText="Change this when the meter or its meaning changes."
                  onChange={(e) => update("epoch", e.target.value || null)}
                />
                <NumberField
                  label="Priority"
                  value={source.priority || 0}
                  onChange={(v) => update("priority", v)}
                />
                <TextField
                  label="Component name (for sums)"
                  value={source.component || ""}
                  onChange={(e) => update("component", e.target.value || null)}
                />
                <NumberField
                  label="Scale correction"
                  value={source.scale ?? 1}
                  onChange={(v) => update("scale", v)}
                />
                <TextField
                  select
                  label="Sign"
                  value={source.sign ?? 1}
                  onChange={(e) => update("sign", Number(e.target.value))}
                >
                  <MenuItem value={1}>As reported</MenuItem>
                  <MenuItem value={-1}>Invert sign</MenuItem>
                </TextField>
                {r.mode === "derived" && (
                  <NumberField
                    label="Balance coefficient"
                    value={source.coefficient ?? 1}
                    onChange={(v) => update("coefficient", v)}
                  />
                )}
                <TextField
                  type="datetime-local"
                  label="Valid from (UTC, optional)"
                  InputLabelProps={{ shrink: true }}
                  value={
                    source.valid_from
                      ? String(source.valid_from).slice(0, 16)
                      : ""
                  }
                  onChange={(e) =>
                    update(
                      "valid_from",
                      e.target.value ? e.target.value + ":00Z" : null,
                    )
                  }
                />
                <TextField
                  type="datetime-local"
                  label="Valid until (UTC, optional)"
                  InputLabelProps={{ shrink: true }}
                  value={
                    source.valid_to ? String(source.valid_to).slice(0, 16) : ""
                  }
                  onChange={(e) =>
                    update(
                      "valid_to",
                      e.target.value ? e.target.value + ":00Z" : null,
                    )
                  }
                />
                <Button
                  onClick={() =>
                    set(
                      i,
                      "sources",
                      r.sources.filter((_: unknown, j: number) => j !== n),
                    )
                  }
                >
                  Remove source
                </Button>
              </Box>
            );
          })}
          <Button
            onClick={() =>
              set(i, "sources", [
                ...(r.sources || []),
                { source: "", priority: 0 },
              ])
            }
          >
            Add source
          </Button>
          <Button onClick={() => onChange(rows.filter((_, j) => j !== i))}>
            Remove profile
          </Button>
        </Paper>
      ))}
      <Button
        onClick={() =>
          onChange([
            ...rows,
            {
              feature: "household_load",
              mode: "stitch",
              sources: [{ source: "", priority: 0 }],
            },
          ])
        }
      >
        Add composition profile
      </Button>
    </Stack>
  );
}
export function InverterForm({
  value,
  onChange,
}: {
  value: Record<string, number>;
  onChange: (v: Record<string, number>) => void;
}) {
  return (
    <Stack spacing={2}>
      {Object.entries(value).map(([key, n]) => (
        <Stack direction="row" gap={2} key={key}>
          <TextField
            label="Inverter group"
            value={key}
            onChange={(e) => {
              const next = { ...value };
              delete next[key];
              next[e.target.value] = n;
              onChange(next);
            }}
          />
          <NumberField
            label="AC limit · kW"
            value={n}
            onChange={(v) => onChange({ ...value, [key]: v || 0 })}
          />
          <Button
            onClick={() => {
              const next = { ...value };
              delete next[key];
              onChange(next);
            }}
          >
            Remove limit
          </Button>
        </Stack>
      ))}
      <Button
        onClick={() =>
          onChange({
            ...value,
            [`inverter_${Object.keys(value).length + 1}`]: 5,
          })
        }
      >
        Add known inverter limit
      </Button>
    </Stack>
  );
}
