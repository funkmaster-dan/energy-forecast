# Configuration examples

Full validated fields are in `contracts/configuration-v1.json`. The GUI provides forms for location, sources, panel banks, batteries, tariff rules and calendars. Save the draft configuration after reviewing it; no JSON editing is required. The examples below document the API for developers. Resolved-date previews use the saved revision. Source names below are placeholders, not a commissioned installation.

## Source composition

```json
[
  {
    "feature": "household_load",
    "mode": "stitch",
    "resolution_minutes": 60,
    "sources": [
      {"source": "sensor.old_load", "priority": 0, "valid_to": "2026-06-01T00:00:00Z"},
      {"source": "sensor.current_load", "priority": 1, "valid_from": "2026-06-01T00:00:00Z"}
    ]
  },
  {"feature": "pv_generation", "mode": "stitch", "sources": [{"source": "sensor.pv_ac"}]},
  {"feature": "battery_soc", "mode": "stitch", "sources": [{"source": "sensor.battery_soc"}]}
]
```

`stitch` and `fallback` select one accepted source per interval. `sum` requires explicitly distinct disjoint `component` names. Never combine a whole-home meter with its own circuits. `derived` accepts bounded coefficients in [-1,1], not code or arbitrary expressions. A verified AC balance may use PV + grid import + battery discharge − grid export − battery charge, with each flow mapped once.

Normalize units, signs and DC conversion before composition. Difference counters within their source epoch, and change the epoch on meter/unit/boundary changes. Do not subtract across a reset or gap. Native five-minute components can aggregate to an hourly target. A direct covered hourly statistic replaces overlapping raw samples of the same meter. Incomplete totals and coarse-to-fine disaggregation are not ground truth.

Use `maximum_kw` for installation-specific physical bounds. Genuine high demand remains valid below those limits. Suspect values can remain in robust fitting; hard-invalid values are excluded. Quality reviews preserve the original observation and add an auditable mask. Raw data and retained extremes are visible in Data quality.

## Tariffs and charging

```json
[
  {"name":"Default", "start":"00:00", "end":"00:00", "priority":0, "import_rate":0.30, "export_rate":0.05},
  {"name":"Evening", "start":"16:15", "end":"21:00", "priority":1, "import_rate":0.40, "export_rate":0.25},
  {"name":"Free lunch", "start":"11:00", "end":"14:00", "priority":1, "import_rate":0, "export_rate":0.05, "grid_charge_allowed":true}
]
```

Prices are AUD/kWh. Equal start/end means all day. Cross-midnight rules belong to the start day's weekday/month/date selector. Higher priorities override lower ones; ties and uncovered dates are rejected. `months`, `weekdays` and explicit `dates` provide seasonal/exception rules. Rule-level import/export kW and export-energy caps are optional. Grid-origin re-export can be prohibited; initial battery origin is then unverified and conservatively unavailable for discretionary export.

Zero price alone does not permit charging. Free charge is limited by time, site import headroom, battery power, efficiency and taper. Unconfirmed charging is reported separately as conditional. The service never executes charging.

## Calendars

Configure public and school jurisdiction separately (ACT/NSW/NT/QLD/SA/TAS/VIC/WA). Import official school/region/profile ranges and explicitly mark reviewed `covered_years`; unknown years remain unknown. Multiple profiles use `any`/`all`. Public holidays use the pinned `holidays` library, with regional exceptions imported explicitly. Public/calendar features never change tariff exceptions implicitly.

JSON events use exclusive end dates/times and source/version provenance. CSV columns are `start,end,label` with optional `start_time,end_time`. ICS date-only exclusive ends and timed events are normalized in the site zone. `known_at` records when a schedule was known; retrospective data must not imply earlier knowledge.

References: [Fair Work 2026 holidays](https://www.fairwork.gov.au/employment-conditions/public-holidays/2026-public-holidays), [SA official school dates](https://www.sa.gov.au/topics/education-and-learning/general-information/term-dates), [NSW calendars](https://education.nsw.gov.au/schooling/calendars). Validate imported jurisdiction/region/profile dates against their official source. No nationwide school-date service is assumed.

## Weather imports

Live forecast refresh is automatic. `weather_history` jobs accept a 0–31-day `{ "start":"YYYY-MM-DD", "end":"YYYY-MM-DD" }` page of reanalysis, labeled pretraining-only. `weather_run` accepts a past UTC initialization `run` and optional documented `available_at`, and requires an explicit configured weather model. Unknown run availability stays unknown; initialization is not receipt/availability. Original arrays and revisions remain archived.

Provider references: [forecast interval semantics](https://open-meteo.com/en/docs), [single-run archives and availability](https://open-meteo.com/en/docs/single-runs-api). Archive variable/model/date coverage must be checked for the actual site. Weather import failure never fabricates future irradiance.
