# Delivery status — 0.2.0 alpha

This is a working forecasting/data collection and shadow-planning release. The broader specification is not fully complete. Root planning documents remain the full backlog; released behavior is described by this repository and its v1 contracts. No unsupported model/path confidence is promoted.

| Milestone | Delivered | Remaining exit conditions |
|---|---|---|
| M0 | Two public repositories, MIT, independent main branches, contributor guides, Python/npm lockfiles | None for bootstrap |
| M1 | Authenticated API/GUI, raw/revision/batch journal, setup key, scoped tokens, durable jobs, non-root image, persistence/backup | Household commissioning; Docker runtime smoke passed in Debian LXC; live input verification remains |
| M2 | HA-first onboarding with location map and context-filtered selectors, HACS pairing/options/discovery, selected live input forwarding, hourly/5-minute/raw Recorder paging, retry checkpoints, composition/alignment, coverage/review UI | Live household PV/boundary/transition audit; richer contextual/stalling/cross-meter fault rules and overlap correction need measured data |
| M3 | Direct archived live weather, run/reanalysis import jobs, pvlib POA, jurisdiction/profile imports, coverage/partial-day/ICS handling | Actual archive coverage and all regional school datasets need commissioning; no automatic education-site scrapers |
| M4 | Fixed TOU priority/date/month/weekday rules, exact boundaries/DST, conservative origin/caps/efficiency/taper battery simulation, passive-fit gate | Passive measured parameter identification; tariff-specific tiered rates and per-phase validation are not implemented |
| M5 | Historical irradiance calibration of geometry-only bank models, physical/shared-clipping PV, weekday load baseline, bounded LightGBM candidate training, direct ordered TCN quantiles, chronological metrics, linked charts/replay | Weather-aware CNN training/inference and matched calendar-only ablation are implemented; household/calendar feature ablation and bank-specific tree activation need validated labels/vintages; PV candidates remain review-only |
| M6 | Hourly bias/uncertainty and daily recent-profile jobs, weekly main jobs, pause/quality/holdout gates, checkpoints, atomic activation/rollback, compatible paired residual blocks | Real recent adaptation/drift/coverage evidence and representative replay commissioning; optimizer checkpoint recovery checked mechanically rather than with long production training |
| M7 | Common causal policy search, baseline infeasibility, terminal obligation, independent bootstrap verifier, replacement budgets/short leases/local HA expiry | Operating tail validation/readiness promotion remains disabled; no globally optimal/per-phase solver |
| M8 | Forms throughout the GUI without JSON editing, all 11 GUI sections, free MUI/ECharts, themes/mobile, quality heatmap/tariff preview, HACS packaging, Compose/Quadlet/backup docs | GUI calibration plots become useful with actual candidates/outcomes; optional scenario explorer and detailed source/calibration analyses remain future work |
| M9 | Shadow data collection and evaluation scaffolding | Live commissioning and enough independent measured outcomes; 99% export confidence is not established |

## Checks recorded

- Focused service pytest and Ruff; integration protocol/retry pytest and Ruff.
- TypeScript/Vite build and frontend lint; Playwright desktop/mobile checks with screenshot inspection.
- Isolated HA Core 2026.9.4: config flow, setup of ten entities, selected-input forwarding, cached readiness local expiry, options reload and unload.
- Rootless Podman image and Docker/Compose LXC startup, real Open-Meteo receipt, rolling 48-hour baseline/quarter-hour simulation, asynchronous synthetic CNN candidate, failed-gate retention, health/persistence/replacement and clean-volume restore.
- Public contents include only code, generalized documentation and synthetic contracts/fixtures. Credentials, actual household observations, local setup keys, backups, HA runtime and model artifacts stay outside Git.

Neither API nor integration has a device actuation path. A model finishing training and a feasible shadow simulation both remain distinct from export readiness. Current export remaining/now/power values are zero whenever readiness is unvalidated.

The 0.2.0 setup changes passed the basic service suite (51 tests), integration suite (7 tests), lint/build, and a manual Playwright desktop/mobile setup journey using synthetic HA metadata. Actual household HACS pairing and calibrated bank output still need live verification.
