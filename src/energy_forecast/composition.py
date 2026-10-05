"""Normalize before selection; never stitch cumulative meter readings directly."""

from collections import defaultdict
from datetime import datetime, timedelta


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize(records, mapping):
    sources = {s.source: s for s in mapping.sources}
    previous = {}
    output = []
    for record in sorted(
        records, key=lambda r: (r["source"], r["epoch"], dt(r["start"]), dt(r["end"]))
    ):
        source = sources.get(record["source"])
        if source is None:
            continue
        start, end = dt(record["start"]), dt(record["end"])
        if source.valid_from and start < source.valid_from:
            continue
        if source.valid_to and end > source.valid_to:
            continue
        value, reasons = record["value"], list(record["reasons"])
        quality = record["quality"]
        hours = (end - start).total_seconds() / 3600
        if record["kind"] == "counter":
            key = (record["source"], record["epoch"])
            old = previous.get(key)
            previous[key] = record
            if old is None or value is None or old["value"] is None:
                value, quality = None, "invalid"
                reasons.append("counter_epoch_start")
            elif old["unit"] != record["unit"] or old["boundary"] != record["boundary"]:
                value, quality = None, "invalid"
                reasons.append("counter_semantics_changed")
            elif old["quality"] == "invalid":
                value, quality = None, "invalid"
                reasons.append("counter_previous_invalid")
            elif dt(old["end"]) != start:
                value, quality = None, "invalid"
                reasons.append("counter_gap")
            else:
                value -= old["value"]
                if value < 0:
                    value, quality = None, "invalid"
                    reasons.append("counter_reset")
        if record["kind"] == "state" or record["unit"] not in ("W", "kW", "Wh", "kWh"):
            value, quality = None, "invalid"
            reasons.append("not_an_energy_measurement")
        if record["boundary"] not in ("AC", "DC"):
            value, quality = None, "invalid"
            reasons.append("not_at_electrical_bus")
        if value is not None:
            if record["unit"] in ("W", "Wh"):
                value /= 1000
            value *= source.sign * source.scale
            if record["kind"] == "mean_power":
                value *= hours
            if record["boundary"] == "DC":
                value *= source.dc_to_ac_efficiency
            if record["coverage"] < mapping.min_coverage:
                quality = "invalid"
                reasons.append("incomplete_coverage")
            if value < 0 and mapping.mode != "derived":
                quality = "invalid"
                reasons.append("negative_energy")
            if abs(value) > mapping.maximum_kw * hours:
                quality = "invalid"
                reasons.append("physical_limit")
        if value is None:
            quality = "invalid"
            reasons.append("missing")
        output.append(
            {
                **record,
                "energy_kwh": value,
                "quality": quality,
                "reasons": sorted(set(reasons)),
                "coefficient": source.coefficient,
                "priority": source.priority,
            }
        )
    return output


def align(normalized, mapping):
    """Aggregate covered native intervals onto a UTC grid without summing overlaps."""
    groups = defaultdict(list)
    minutes = mapping.resolution_minutes
    for row in normalized:
        start, end = dt(row["start"]), dt(row["end"])
        cursor = start.replace(minute=start.minute // minutes * minutes, second=0, microsecond=0)
        while cursor < end:
            stop = cursor + timedelta(minutes=minutes)
            first, last = max(start, cursor), min(end, stop)
            fraction = (last - first).total_seconds() / (end - start).total_seconds()
            item = {
                **row,
                "part_start": first,
                "part_end": last,
                "energy_kwh": row["energy_kwh"] * fraction
                if row["energy_kwh"] is not None
                else None,
            }
            if (end - start).total_seconds() > minutes * 60 and (first != start or last != end):
                # Coarse energy cannot establish finer measured peaks/targets.
                item["quality"] = "imputed"
                item["reasons"] = [*item["reasons"], "coarse_disaggregation"]
            groups[(row["source"], row["epoch"], cursor, stop)].append(item)
            cursor = stop
    output = []
    for (_, _, start, end), rows in sorted(groups.items()):
        valid = [
            r for r in rows if r["quality"] in ("valid", "suspect") and r["energy_kwh"] is not None
        ]
        full = [r for r in valid if r["part_start"] == start and r["part_end"] == end]
        if full:
            # A direct covered statistic replaces overlapping raw samples of that same meter.
            selected = full[:1]
        else:
            selected = []
            last = start
            for row in sorted(valid, key=lambda r: (r["part_start"], r["part_end"])):
                if row["part_start"] < last:
                    continue
                selected.append(row)
                last = row["part_end"]
        duration = sum((r["part_end"] - r["part_start"]).total_seconds() for r in selected)
        coverage = duration / (end - start).total_seconds()
        value = sum(r["energy_kwh"] for r in selected) if coverage >= mapping.min_coverage else None
        reasons = sorted({reason for r in selected for reason in r["reasons"]})
        if value is None:
            reasons = sorted(
                {reason for r in rows for reason in r["reasons"]} | {"incomplete_aligned_coverage"}
            )
        if value is not None and coverage < 1 - 1e-6:
            value = None
            reasons.append("aligned_gap_not_ground_truth")
        proxy = {
            **rows[0],
            "start": start.isoformat(),
            "end": end.isoformat(),
            "energy_kwh": value,
            "quality": "invalid"
            if value is None
            else "suspect"
            if any(r["quality"] == "suspect" for r in selected)
            else "valid",
            "reasons": reasons,
            "ids": [r["id"] for r in selected],
            "coverage": coverage,
        }
        output.append(proxy)
    return output


def compose(records, mapping):
    groups = defaultdict(list)
    for value in align(normalize(records, mapping), mapping):
        groups[(value["start"], value["end"])].append(value)
    result = []
    last_source = None
    for (start, end), values in sorted(groups.items()):
        accepted = [
            v
            for v in values
            if v["quality"] in ("valid", "suspect") and v["energy_kwh"] is not None
        ]
        # Epoch overlap for the same source is ambiguous, never sum or silently choose.
        ambiguous = len({v["source"] for v in accepted}) != len(accepted)
        reasons = []
        if ambiguous:
            accepted = []
            reasons.append("overlapping_source_epochs")
        if mapping.mode in ("stitch", "fallback"):
            accepted.sort(key=lambda v: (-v["priority"], v["source"]))
            chosen = accepted[:1]
            value = chosen[0]["energy_kwh"] if chosen else None
            if len(accepted) > 1 and abs(
                accepted[0]["energy_kwh"] - accepted[1]["energy_kwh"]
            ) > max(0.1, abs(accepted[0]["energy_kwh"]) * 0.2):
                reasons.append("source_disagreement")
            if chosen and last_source and chosen[0]["source"] != last_source:
                reasons.append("source_transition")
            if chosen:
                last_source = chosen[0]["source"]
        else:
            chosen = accepted
            if len({v["source"] for v in chosen}) != len(mapping.sources):
                value = None
                reasons.append("missing_component")
            else:
                value = sum(
                    v["energy_kwh"] * (v["coefficient"] if mapping.mode == "derived" else 1)
                    for v in chosen
                )
                if value < 0:
                    reasons.append("negative_composed_energy")
                    value = None
        if (
            value is not None
            and value > mapping.maximum_kw * (dt(end) - dt(start)).total_seconds() / 3600
        ):
            value = None
            reasons.append("composed_physical_limit")
        if value is None:
            reasons = sorted(
                set(reasons) | {reason for source in values for reason in source["reasons"]}
            )
        result.append(
            {
                "start": start,
                "end": end,
                "energy_kwh": value,
                "quality": "invalid"
                if value is None
                else "suspect"
                if reasons or any(v["quality"] == "suspect" for v in chosen)
                else "valid",
                "sources": [v["source"] for v in chosen],
                "observation_ids": [
                    identifier for v in chosen for identifier in v.get("ids", [v["id"]])
                ],
                "reasons": reasons or ([] if value is not None else ["no_valid_source"]),
            }
        )
    return result
