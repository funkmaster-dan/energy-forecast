import { useEffect, useRef, useState } from "react";
import { Alert, Box, Button, Paper, Stack, Typography } from "@mui/material";
import * as echarts from "echarts";

type Coverage = {
  profiles: {
    feature: string;
    mode: string;
    intervals: number;
    invalid_intervals: number;
    coverage: {
      date: string;
      fraction: number;
      covered_hours: number;
      day_hours: number;
    }[];
    reasons: Record<string, number>;
  }[];
};
export function CoveragePanel() {
  const [value, setValue] = useState<Coverage | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    fetch("/v1/sites/home/quality")
      .then(async (r) => {
        if (!r.ok) throw Error("Coverage could not be loaded");
        setValue(await r.json());
      })
      .catch((e) => setError(e.message));
  }, []);
  return (
    <Paper sx={{ p: 3, mb: 3 }}>
      <Typography variant="h5">Measured coverage</Typography>
      <Typography color="text.secondary" sx={{ my: 2 }}>
        Each square is one local day. Coverage uses the union of valid
        intervals, including 23/25-hour DST days. Missing days remain visible.
      </Typography>
      {error && <Alert severity="error">{error}</Alert>}
      {value?.profiles.map((p) => (
        <Box key={p.feature} sx={{ my: 3 }}>
          <Stack direction="row" gap={2}>
            <Typography fontWeight={600}>
              {p.feature.replaceAll("_", " ")}
            </Typography>
            <Typography color="text.secondary">
              {p.mode} · {p.intervals} intervals · {p.invalid_intervals} invalid
            </Typography>
          </Stack>
          <Box
            sx={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill,minmax(20px,1fr))",
              gap: "4px",
              my: 2,
            }}
          >
            {p.coverage.map((day) => (
              <Box
                key={day.date}
                title={`${day.date}: ${(day.fraction * 100).toFixed(1)}% (${day.covered_hours.toFixed(1)}/${day.day_hours} hours)`}
                aria-label={`${day.date}: ${(day.fraction * 100).toFixed(1)} percent covered`}
                tabIndex={0}
                sx={{
                  height: 20,
                  borderRadius: 1,
                  bgcolor:
                    day.fraction >= 0.9
                      ? "#3d9d7c"
                      : day.fraction > 0
                        ? "#b88842"
                        : "#64736955",
                }}
              />
            ))}
          </Box>
          <Typography variant="caption">
            {Object.entries(p.reasons)
              .map(([key, count]) => `${key.replaceAll("_", " ")}: ${count}`)
              .join(" · ") ||
              "No composed reason flags in the selected history."}
          </Typography>
        </Box>
      ))}
    </Paper>
  );
}
export function QualityOverlay({
  rows,
  dark,
}: {
  rows: {
    start: string;
    end: string;
    energy_kwh: number | null;
    quality: string;
  }[];
  dark: boolean;
}) {
  const node = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!node.current || !rows.length) return;
    const chart = echarts.init(node.current, dark ? "dark" : undefined);
    chart.setOption({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis" },
      grid: { left: 55, right: 20, bottom: 55, top: 25 },
      xAxis: { type: "time", axisLabel: { hideOverlap: true } },
      yAxis: { type: "value", name: "kWh" },
      dataZoom: [{ type: "inside" }, { type: "slider" }],
      series: [
        {
          name: "Composed measured energy",
          type: "line",
          showSymbol: false,
          connectNulls: false,
          data: rows.map((r) => [r.start, r.energy_kwh]),
        },
        {
          name: "Suspect observations retained",
          type: "scatter",
          symbolSize: 5,
          itemStyle: { color: "#dfaa55" },
          data: rows
            .filter((r) => r.quality === "suspect")
            .map((r) => [r.start, r.energy_kwh]),
        },
      ],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(node.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [rows, dark]);
  return <Box ref={node} sx={{ height: 300, my: 2 }} />;
}
export function TariffTimeline({
  rows,
  zone,
}: {
  rows: {
    start: string;
    end: string;
    tariff: {
      name: string;
      import_rate: number;
      export_rate: number;
      grid_charge_allowed: boolean;
    };
  }[];
  zone: string;
}) {
  const total = rows.reduce(
    (n, r) => n + Date.parse(r.end) - Date.parse(r.start),
    0,
  );
  return (
    <Box sx={{ my: 3 }}>
      <Box
        sx={{
          display: "flex",
          minHeight: 70,
          borderRadius: 2,
          overflow: "hidden",
        }}
      >
        {rows.map((r, i) => (
          <Box
            key={r.start}
            sx={{
              flex: (Date.parse(r.end) - Date.parse(r.start)) / total,
              p: 1,
              bgcolor: r.tariff.grid_charge_allowed ? "#318363" : "#576da1",
              borderRight: "1px solid #ffffff50",
              minWidth: 0,
            }}
            title={`${r.tariff.name}: import $${r.tariff.import_rate}/kWh · export $${r.tariff.export_rate}/kWh`}
          >
            <Typography
              fontSize={11}
              color="#fff"
              sx={{ overflow: "hidden", textOverflow: "ellipsis" }}
            >
              {r.tariff.name}
            </Typography>
            <Typography fontSize={11} color="#fff">
              ${r.tariff.import_rate} / ${r.tariff.export_rate}
            </Typography>
            <Typography fontSize={10} color="#fff">
              {new Date(r.start).toLocaleTimeString("en-AU", {
                timeZone: zone,
                hour: "2-digit",
                minute: "2-digit",
              })}
            </Typography>
            <span className="sr-only">Segment {i + 1}</span>
          </Box>
        ))}
      </Box>
      <Typography variant="caption">
        Import / export AUD per kWh. Green segments permit grid charging;
        charging also requires a confirmed execution assumption.
      </Typography>
    </Box>
  );
}
export function AddBank({ onAdd }: { onAdd: () => void }) {
  return (
    <Button sx={{ mb: 2 }} onClick={onAdd}>
      Add panel bank
    </Button>
  );
}
