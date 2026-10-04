"""Episode summary, metrics, and the static offline HTML run report (Module 7, Section G, Module 8 metrics).

Built only from a run's manifests, event log, and policy_start.json; never
launches SUMO. All dynamic text is HTML-escaped, graphics are inline SVG, and
every marker links to a unique anchor whose id is the event ID it explains.
`summary.json` and `metrics.csv` are derived artifacts: regenerating them from
the same immutable log gives the same content.
"""

from __future__ import annotations

import csv
import json
from html import escape
from pathlib import Path

from traffic_cps.events import REPORT_FILE
from traffic_cps.explain import (
    ExplainError,
    RunTrace,
    explain_event,
    load_trace,
    name,
    run_policy_views,
)

SUMMARY_FILE = "summary.json"
METRICS_FILE = "metrics.csv"
NOTABLE_LIMIT = 25

PLOT_W, LEFT, RIGHT = 940, 50, 10
QUEUE_H, BAND_H, MARK_H = 170, 16, 26
SERIES = {"NS": "var(--ns)", "WE": "var(--we)"}
LIGHT = {"green": "var(--green)", "yellow": "var(--yellow)", "red": "var(--red)"}
EXPLAINED = ("decision", "operating_action", "safety_override", "safety_violation", "phantom_demand",
             "masked_demand")
SOURCES = {  # marker color and legend label per decision source
    "queue_rule": ("var(--s-queue)", "queue rule"),
    "fixed_schedule": ("var(--s-fixed)", "fixed schedule"),
    "q_greedy": ("var(--s-greedy)", "learned (greedy)"),
    "q_exploration": ("var(--s-explore)", "exploration"),
    "queue_fallback": ("var(--s-fallback)", "fallback to queue rule"),
    "test_script": ("var(--muted)", "test script"),
}

CSS = """
:root { --bg:#ffffff; --fg:#1d232a; --muted:#5d6770; --grid:#e3e7eb; --card:#f6f8fa;
  --ns:#2f6fdd; --we:#c2551f; --green:#2e9d4f; --yellow:#e0b100; --red:#d0d4d8; --alert:#c62828;
  --s-queue:#2f6fdd; --s-fixed:#7b5ea7; --s-greedy:#1f8a70; --s-explore:#d4a017; --s-fallback:#c2551f;
  --cell-na:#e3e7eb; --cell-ns:#c9dcfb; --cell-we:#f8cdb3; }
@media (prefers-color-scheme: dark) { :root { --bg:#14181c; --fg:#e6e9ec; --muted:#9aa4ad; --grid:#2a3138;
  --card:#1c2227; --ns:#6c9cff; --we:#ff9a62; --green:#4cc06f; --yellow:#f0c420; --red:#3a4148; --alert:#ff6b6b;
  --s-queue:#6c9cff; --s-fixed:#b39ddb; --s-greedy:#4cc09a; --s-explore:#f0c420; --s-fallback:#ff9a62;
  --cell-na:#2a3138; --cell-ns:#24395c; --cell-we:#5a3422; } }
body { background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:0; padding:24px 16px; }
main { max-width:1000px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:28px 0 8px; } h3 { font-size:14px; }
.meta { color:var(--muted); margin:0 0 12px; } svg { width:100%; height:auto; display:block; }
svg text { fill:var(--muted); font-size:11px; } .grid { stroke:var(--grid); }
.legend span { display:inline-block; margin-right:14px; color:var(--muted); font-size:13px; }
.swatch { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:middle; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:2px 14px; margin:0; }
dt { color:var(--muted); } dd { margin:0; }
.entry { background:var(--card); border-radius:6px; padding:10px 12px; margin:8px 0; scroll-margin-top:12px; }
.entry:target { outline:2px solid var(--ns); } .entry h3 { margin:0 0 4px; }
.alt, .later { color:var(--muted); font-size:13px; margin-top:4px; }
.lim { color:var(--alert); font-size:13px; } .alert { color:var(--alert); }
.views { display:flex; flex-wrap:wrap; gap:4px 18px; } .view { overflow-x:auto; max-width:100%; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; background:var(--card); padding:10px 12px; border-radius:6px; }
table.q { border-collapse:collapse; font-size:11px; margin:4px 0 10px; }
table.q th, table.q td { border:1px solid var(--grid); padding:2px 4px; text-align:center; min-width:46px; }
td.na { background:var(--cell-na); color:var(--muted); }
td.fb { background:var(--bg); color:var(--muted); font-style:italic; border-style:dashed; }
td.gNS { background:var(--cell-ns); } td.gWE { background:var(--cell-we); }
"""


# ----------------------------------------------------------------------
# Summary and metrics (Module 8 definitions)
# ----------------------------------------------------------------------

def summarize(trace: RunTrace) -> dict:
    from traffic_cps.controllers.queue import queue_choice
    from traffic_cps.contracts import Direction
    from traffic_cps.explain import SCORE_CAP, TIE_TOL

    events = trace.events
    end = next((e for e in reversed(events) if e["type"] == "run_end"), None)
    end_p = end["payload"] if end else {}
    horizon = end_p.get("planned_horizon_s") or trace.manifest.get("horizon_s")
    completion = end_p.get("completion", "unknown (no run_end record)")

    samples = [e for e in events if e["type"] == "observation" and e["sim_time_s"] >= 1]
    # Congestion metrics use the simulator's true counts (Section H); logs without a separate truth
    # field come from clean runs, where measured equals true.
    has_truth = bool(samples) and all(e["payload"].get("true_counts_by_direction") for e in samples)
    key = "true_counts_by_direction" if has_truth else "counts_by_direction"
    totals = [sum(e["payload"][key].values()) for e in samples]
    by_dir = {d: sum(e["payload"][key][d] for e in samples) for d in ("NS", "WE")}
    accounting = end_p.get("vehicle_accounting")
    n = len(samples)
    partial = completion != "completed" or n != horizon
    metrics = {
        "samples": n,
        "partial": partial,
        "mean_queued_vehicles": (sum(totals) / n) if n else None,
        "mean_queued_by_direction": {d: (v / n) if n else None for d, v in by_dir.items()},
        "peak_queued_vehicles": max(totals) if totals else None,
        "queue_exposure_vehicle_seconds": sum(totals),
        "metric_counts": "true" if has_truth else "measured (no separate truth recorded)",
        "completed_vehicles": accounting["arrived"] if accounting and "arrived" in accounting else "not_recorded",
        "remaining_demand": ({"active": accounting["active"], "not_inserted": accounting["not_inserted"]}
                             if accounting and "active" in accounting else "not_recorded"),
        "vehicle_accounting_consistent": accounting.get("consistent") if accounting else None,
        "scheduled_vehicles": accounting.get("scheduled") if accounting else None,
    }
    masked_times = sorted(e["sim_time_s"] for e in events if e["type"] == "masked_demand")
    stretches = sum(1 for i, t in enumerate(masked_times) if i == 0 or t != masked_times[i - 1] + 1)
    errors = [abs(e["payload"]["counts_by_lane"][lane] - e["payload"]["true_counts_by_lane"][lane])
              for e in samples if has_truth for lane in e["payload"]["counts_by_lane"]]
    robustness = {
        "fault_label": (trace.manifest.get("measurement") or {}).get("label", "clean"),
        "measurement": trace.manifest.get("measurement"),
        "phantom_demand_events": sum(1 for e in events if e["type"] == "phantom_demand"),
        "masked_demand_seconds": len(masked_times),
        "masked_demand_stretches": stretches,
        "mean_abs_lane_count_error": (sum(errors) / len(errors)) if errors else None,
    }

    decisions = [e for e in events if e["type"] == "decision"]
    submissions = {e["payload"]["origin"]["id"]: e["payload"]["status"] for e in events
                   if e["type"] == "submission"}
    by_source: dict[str, int] = {}
    for d in decisions:
        by_source[d["payload"]["source"]] = by_source.get(d["payload"]["source"], 0) + 1
    fallbacks = [d for d in decisions if d["payload"]["source"] == "queue_fallback"]
    counters = end_p.get("counters", {})
    behavior = {
        "scheduled_checks": counters.get("scheduled_checks"),
        "constraint_holds": sum(1 for e in events if e["type"] == "constraint_hold"),
        "decisions": len(decisions),
        "by_source": by_source,
        "accepted": sum(1 for d in decisions if submissions.get(d["event_id"]) == "accepted"),
        "switch_requests_accepted": sum(1 for d in decisions if not d["payload"]["retains"]
                                        and submissions.get(d["event_id"]) == "accepted"),
        "blocked": sum(1 for d in decisions if submissions.get(d["event_id"]) == "blocked"),
        "forced_switches": sum(1 for e in events if e["type"] == "operating_action"),
        "fallback_share_of_decisions": (len(fallbacks) / len(decisions)) if decisions else None,
        "fallback_reasons": {r: sum(1 for d in fallbacks if d["payload"]["fallback_reason"] == r)
                             for r in ("unseen_state", "incomplete_action_coverage")},
        "learning_updates": sum(1 for e in events if e["type"] == "learning_update"),
        "feedback_discarded": sum(1 for e in events if e["type"] == "feedback_discarded"),
    }

    def disagrees(d: dict) -> bool:
        p = d["payload"]
        if p["source"] not in ("q_greedy", "q_exploration"):
            return False
        requested = queue_choice({Direction(k): v for k, v in p["scores"].items()},
                                 {Direction(k): v for k, v in p["counts"].items()}, Direction(p["current_green"]),
                                 p["elapsed_green_s"], p["green_service_limit_s"], TIE_TOL, SCORE_CAP)[0]
        return requested.value != p["requested"]

    def notable(items: list[dict]) -> dict:
        return {"total": len(items), "items": [{"event_id": e["event_id"], "time": e["sim_time_s"],
                                                "type": e["type"]} for e in items[:NOTABLE_LIMIT]]}

    return {
        "run": {"run_id": trace.manifest.get("run_id"), "controller": trace.manifest.get("controller"),
                "mode": trace.manifest.get("mode"), "horizon_s": horizon, "completion": completion,
                "failure": end_p.get("failure"), "final_valid_time_s": end_p.get("final_valid_time_s"),
                "trace_complete": trace.complete, "trace_problems": trace.problems,
                "checkpoint_status": end_p.get("checkpoint_status"),
                "provenance": trace.manifest.get("provenance")},
        "metrics": metrics,
        "behavior": behavior,
        "robustness": robustness,
        "safety": {"overrides": sum(1 for e in events if e["type"] == "safety_override"),
                   "violations": sum(1 for e in events if e["type"] == "safety_violation")},
        "notable": {
            "switches": notable([d for d in decisions if not d["payload"]["retains"]
                                 and submissions.get(d["event_id"]) == "accepted"]),
            "forced_switches": notable([e for e in events if e["type"] == "operating_action"]),
            "fallbacks": notable(fallbacks),
            "exploration": notable([d for d in decisions if d["payload"]["source"] == "q_exploration"]),
            "disagreements_with_queue_rule": notable([d for d in decisions if disagrees(d)]),
            "safety": notable([e for e in events if e["type"] in ("safety_override", "safety_violation")]),
            "phantom_demand": notable([e for e in events if e["type"] == "phantom_demand"]),
            "masked_demand": notable([e for e in events if e["type"] == "masked_demand"]),
        },
    }


METRIC_COLUMNS = ("run_id", "controller", "mode", "horizon_s", "completion", "trace_complete", "partial",
                  "samples", "mean_queued_vehicles", "mean_queued_ns", "mean_queued_we", "peak_queued_vehicles",
                  "queue_exposure_vehicle_seconds", "decisions", "switch_requests_accepted", "forced_switches",
                  "blocked", "fallback_share_of_decisions", "exploration", "learning_updates",
                  "safety_overrides", "safety_violations", "completed_vehicles", "active_vehicles",
                  "not_inserted_vehicles", "vehicle_accounting_consistent", "fault_label", "phantom_demand_events",
                  "masked_demand_seconds", "masked_demand_stretches", "mean_abs_lane_count_error")


def write_summary(run_dir: Path | str) -> tuple[Path, Path]:
    run_dir = Path(run_dir)
    s = summarize(load_trace(run_dir))
    summary_path = run_dir / SUMMARY_FILE
    summary_path.write_text(json.dumps(s, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    r, m, b = s["run"], s["metrics"], s["behavior"]
    row = {
        "run_id": r["run_id"], "controller": r["controller"], "mode": r["mode"], "horizon_s": r["horizon_s"],
        "completion": r["completion"], "trace_complete": r["trace_complete"], "partial": m["partial"],
        "samples": m["samples"], "mean_queued_vehicles": m["mean_queued_vehicles"],
        "mean_queued_ns": m["mean_queued_by_direction"]["NS"], "mean_queued_we": m["mean_queued_by_direction"]["WE"],
        "peak_queued_vehicles": m["peak_queued_vehicles"],
        "queue_exposure_vehicle_seconds": m["queue_exposure_vehicle_seconds"], "decisions": b["decisions"],
        "switch_requests_accepted": b["switch_requests_accepted"], "forced_switches": b["forced_switches"],
        "blocked": b["blocked"], "fallback_share_of_decisions": b["fallback_share_of_decisions"],
        "exploration": b["by_source"].get("q_exploration", 0), "learning_updates": b["learning_updates"],
        "safety_overrides": s["safety"]["overrides"], "safety_violations": s["safety"]["violations"],
        "completed_vehicles": m["completed_vehicles"],
        "active_vehicles": m["remaining_demand"]["active"] if isinstance(m["remaining_demand"], dict) else "not_recorded",
        "not_inserted_vehicles": (m["remaining_demand"]["not_inserted"] if isinstance(m["remaining_demand"], dict)
                                  else "not_recorded"),
        "vehicle_accounting_consistent": m["vehicle_accounting_consistent"],
        **{k: s["robustness"][k] for k in ("fault_label", "phantom_demand_events", "masked_demand_seconds",
                                           "masked_demand_stretches", "mean_abs_lane_count_error")},
    }
    csv_path = run_dir / METRICS_FILE
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=METRIC_COLUMNS)
        writer.writeheader()
        writer.writerow({k: "" if row[k] is None else row[k] for k in METRIC_COLUMNS})
    return summary_path, csv_path


def render_summary_text(s: dict) -> str:
    r, m, b = s["run"], s["metrics"], s["behavior"]
    mean = "n/a" if m["mean_queued_vehicles"] is None else f"{m['mean_queued_vehicles']:.2f}"
    lines = [
        f"Run {r['run_id']} ({r['controller']}, {r['mode']}): {r['completion']}; trace complete: "
        f"{r['trace_complete']}; checkpoint: {r['checkpoint_status']}",
        f"Mean queued vehicles {mean}{' (PARTIAL)' if m['partial'] else ''} over {m['samples']} samples; "
        f"peak {m['peak_queued_vehicles']}; exposure {m['queue_exposure_vehicle_seconds']} vehicle-seconds",
        f"Congestion metrics use {m['metric_counts']} counts. Vehicles: {m['completed_vehicles']} completed, "
        f"remaining {m['remaining_demand']} (accounting consistent: {m['vehicle_accounting_consistent']})",
        f"Scheduled checks {b['scheduled_checks']}: {b['decisions']} decisions, {b['constraint_holds']} timing holds",
        "Decisions by source: " + ", ".join(f"{k} {v}/{b['decisions']}" for k, v in sorted(b["by_source"].items())),
        f"Accepted switches {b['switch_requests_accepted']}, forced switches {b['forced_switches']}, "
        f"blocked {b['blocked']}, learning updates {b['learning_updates']}, discarded feedback "
        f"{b['feedback_discarded']}",
        f"Safety: {s['safety']['overrides']} overrides, {s['safety']['violations']} violations",
    ]
    rb = s["robustness"]
    if rb["fault_label"] != "clean":
        lines.append(f"Sensor fault level {rb['fault_label']}: phantom demand {rb['phantom_demand_events']}, masked "
                     f"demand {rb['masked_demand_seconds']} s in {rb['masked_demand_stretches']} stretches, mean "
                     f"absolute lane count error {rb['mean_abs_lane_count_error']:.3f}")
    if b["fallback_share_of_decisions"] is not None and b["by_source"].get("queue_fallback"):
        reasons = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in b["fallback_reasons"].items())
        lines.append(f"Fallback share {b['fallback_share_of_decisions']:.1%} ({reasons}); "
                     f"if fallback dominates, this run mostly measures the queue rule.")
    for key, group in s["notable"].items():
        if group["total"]:
            ids = ", ".join(i["event_id"] for i in group["items"][:8])
            lines.append(f"Notable {key.replace('_', ' ')} ({group['total']}): {ids}"
                         f"{' ...' if group['total'] > 8 else ''}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------

def _x(t: int, horizon: int) -> float:
    return LEFT + (t / horizon) * PLOT_W if horizon else LEFT


def _bands(events: list[dict], horizon: int) -> dict[str, list[tuple[int, int, str]]]:
    """Half-open [change_time, next_change_time) color intervals per direction from signal changes."""
    bands: dict[str, list[tuple[int, int, str]]] = {"NS": [], "WE": []}
    current: dict[str, tuple[int, str]] = {}
    for e in events:
        if e["type"] != "signal_change":
            continue
        for d, color in e["payload"]["new_colors"].items():
            if d in current and current[d][1] == color:
                continue
            if d in current:
                bands[d].append((current[d][0], e["sim_time_s"], current[d][1]))
            current[d] = (e["sim_time_s"], color)
    end = max([horizon] + [e["sim_time_s"] for e in events])
    for d, (start, color) in current.items():
        bands[d].append((start, end, color))
    return bands


def _svg(trace: RunTrace, horizon: int) -> str:
    obs = [e for e in trace.events if e["type"] == "observation"]
    peak = max([1] + [max(e["payload"]["counts_by_direction"].values()) for e in obs])
    band_top = QUEUE_H + 14
    mark_top = band_top + 2 * (BAND_H + 4) + 6
    height = mark_top + MARK_H + 18
    out = [f'<svg viewBox="0 0 {LEFT + PLOT_W + RIGHT} {height}" role="img" '
           f'aria-label="Queue and signal timeline">']

    def y(count: float) -> float:
        return QUEUE_H - (count / peak) * (QUEUE_H - 10)

    for frac in (0, 0.5, 1):
        gy = y(peak * frac)
        out.append(f'<line class="grid" x1="{LEFT}" x2="{LEFT + PLOT_W}" y1="{gy:.1f}" y2="{gy:.1f}"/>')
        out.append(f'<text x="{LEFT - 6}" y="{gy + 4:.1f}" text-anchor="end">{round(peak * frac)}</text>')
    key = "true_counts_by_direction" if obs and all(e["payload"].get("true_counts_by_direction") for e in obs) \
        else "counts_by_direction"
    for d, color in SERIES.items():
        pts = " ".join(f"{_x(e['sim_time_s'], horizon):.1f},{y(e['payload'][key][d]):.1f}" for e in obs)
        out.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{pts}">'
                   f'<title>{escape(name(d))} waiting vehicles</title></polyline>')

    for row, (d, intervals) in enumerate(_bands(trace.events, horizon).items()):
        top = band_top + row * (BAND_H + 4)
        out.append(f'<text x="{LEFT - 6}" y="{top + 12}" text-anchor="end">{escape(name(d))}</text>')
        for start, end, color in intervals:
            x0, x1 = _x(start, horizon), _x(end, horizon)
            out.append(f'<rect x="{x0:.2f}" y="{top}" width="{max(x1 - x0, 0.5):.2f}" height="{BAND_H}" '
                       f'fill="{LIGHT.get(color, "var(--muted)")}"><title>{escape(name(d))} {escape(color)} '
                       f'[{start}s, {end}s)</title></rect>')

    out.append(f'<text x="{LEFT - 6}" y="{mark_top + 15}" text-anchor="end">events</text>')
    for e in trace.events:
        if e["type"] not in EXPLAINED:
            continue
        cx, cy = _x(e["sim_time_s"], horizon), mark_top + 12
        eid = escape(e["event_id"], quote=True)
        if e["type"] == "decision":
            p = e["payload"]
            color, label = SOURCES.get(p["source"], ("var(--muted)", p["source"]))
            fill = color if not p["retains"] else "none"
            title = escape(f"{label}: {'keep' if p['retains'] else 'switch to'} {name(p['requested'])} at "
                           f"{e['sim_time_s']}s ({e['event_id']})")
            shape = f'<circle cx="{cx:.1f}" cy="{cy}" r="4" fill="{fill}" stroke="{color}" stroke-width="1.2"/>'
        elif e["type"] in ("phantom_demand", "masked_demand"):
            title = escape(f"{e['type'].replace('_', ' ')} (sensing error) at {e['sim_time_s']}s ({e['event_id']})")
            shape = f'<path d="M{cx:.1f},{cy - 5} l5,5 l-5,5 l-5,-5 z" fill="var(--s-explore)"/>'
        elif e["type"] == "operating_action":
            title = escape(f"operating rule (forced switch) at {e['sim_time_s']}s ({e['event_id']})")
            shape = f'<path d="M{cx:.1f},{cy - 6} l6,10 h-12 z" fill="var(--fg)"/>'
        else:
            title = escape(f"safety monitor: {e['type']} at {e['sim_time_s']}s ({e['event_id']})")
            shape = (f'<path d="M{cx - 5:.1f},{cy - 5} l10,10 M{cx + 5:.1f},{cy - 5} l-10,10" '
                     f'stroke="var(--alert)" stroke-width="2"/>')
        out.append(f'<a href="#{eid}"><title>{title}</title>{shape}</a>')

    for t in range(0, horizon + 1, max(1, horizon // 6)):
        out.append(f'<text x="{_x(t, horizon):.1f}" y="{height - 2}" text-anchor="middle">{t}s</text>')
    out.append("</svg>")
    return "\n".join(out)


def _policy_html(view: dict) -> str:
    out = [f'<h3>{escape(view["label"])}</h3><div class="views">']
    for g in view["grids"]:
        if g["elapsed_bin"] == 0:
            continue  # minimum green: no free choices; stated once below instead of an all-grey grid
        caption = (f"{name(g['green'])} green, elapsed bin {g['elapsed_bin']} "
                   f"(rows N/S queue bin, columns W/E queue bin)")
        rows = ["<tr><th></th>" + "".join(f"<th>W/E {w}</th>" for w in range(5)) + "</tr>"]
        for r, row in enumerate(g["cells"]):
            tds = []
            for c in row:
                st = c["status"]
                if st == "constrained":
                    tds.append('<td class="na" title="forced switch at this elapsed time">forced</td>')
                elif st == "unavailable":
                    tds.append('<td class="na">n/a</td>')
                elif st.startswith("fallback"):
                    label = "unseen" if st == "fallback_unseen" else "partial"
                    tds.append(f'<td class="fb" title="evaluation uses the queue-rule fallback">{label}</td>')
                else:
                    mark = {"agrees": "", "disagrees": " &ne;Q", "ambiguous": " ~"}[c["queue_rule"]]
                    cond = "*" if c["conditional"] else ""
                    tip = escape(f"costs N/S {c['costs']['NS']:.2f} ({c['counts']['NS']} updates), W/E "
                                 f"{c['costs']['WE']:.2f} ({c['counts']['WE']} updates); queue rule "
                                 f"{c['queue_rule']}", quote=True)
                    tds.append(f'<td class="g{c["greedy"]}" title="{tip}">{escape(name(c["greedy"]))}{cond}'
                               f'<br>{c["cost_gap"]:.2f}{mark}</td>')
            rows.append(f"<tr><th>N/S {r}</th>{''.join(tds)}</tr>")
        out.append(f'<div class="view"><div class="meta">{escape(caption)}</div>'
                   f'<table class="q">{"".join(rows)}</table></div>')
    out.append("</div>")
    out.append('<p class="meta">Cells show the lower-cost action and the cost gap. "unseen"/"partial": evaluation '
               'would use the queue-rule fallback. "forced": the service limit with waiting vehicles forces a switch. '
               '"*": eligible only if the competing queue is exactly empty. "&ne;Q": disagrees with the queue rule; '
               '"~": equal bins, so the queue rule\'s choice is ambiguous. Elapsed bin 0 (minimum green) never '
               'allows a choice.</p>')
    return "".join(out)


def build_report(run_dir: Path | str) -> Path:
    run_dir = Path(run_dir)
    trace = load_trace(run_dir)
    s = summarize(trace)
    manifest = trace.manifest
    horizon = int(s["run"]["horizon_s"] or max([1] + [e["sim_time_s"] for e in trace.events]))
    run_id = str(manifest.get("run_id", run_dir.name))

    body = [f"<h1>Run report: {escape(run_id)}</h1>",
            f'<p class="meta">Controller: {escape(str(manifest.get("controller")))} &middot; mode '
            f'{escape(str(manifest.get("mode")))} &middot; horizon {horizon}s &middot; completion: '
            f'{escape(str(s["run"]["completion"]))}</p>']

    body.append("<h2>Summary</h2>")
    body.append("<pre>" + escape(render_summary_text(s)) + "</pre>")

    body.append("<h2>Timeline</h2>")
    legend = "".join(f'<span><i class="swatch" style="background:{c}"></i>{escape(label)}</span>'
                     for c, label in SOURCES.values())
    body.append('<p class="legend"><span><i class="swatch" style="background:var(--ns)"></i>N/S waiting</span>'
                '<span><i class="swatch" style="background:var(--we)"></i>W/E waiting</span>'
                '<span><i class="swatch" style="background:var(--green)"></i>green</span>'
                '<span><i class="swatch" style="background:var(--yellow)"></i>yellow</span>'
                '<span><i class="swatch" style="background:var(--red)"></i>red</span></p>'
                f'<p class="legend">{legend}<span>&#9679; switch &middot; &#9675; keep &middot; &#9650; operating '
                f'rule &middot; &#10005; safety monitor &middot; &#9670; sensing-error diagnostic</span></p>'
                '<p class="meta">Waiting-vehicle lines show the simulator\'s true counts.</p>')
    body.append(_svg(trace, horizon))

    body.append("<h2>Learned policy</h2>")
    try:
        views = run_policy_views(trace)
    except ExplainError as exc:
        views = None
        body.append(f'<p class="alert">Policy view unavailable: {escape(str(exc))}</p>')
    if views == []:
        body.append("<p>No learned policy to show: this is a baseline controller.</p>")
    for v in views or []:
        body.append(_policy_html(v))

    body.append("<h2>Notable decisions</h2>")
    any_notable = False
    for key, group in s["notable"].items():
        if not group["total"]:
            continue
        any_notable = True
        links = ", ".join(f'<a href="#{escape(i["event_id"], quote=True)}">{escape(i["event_id"])}</a>'
                          for i in group["items"])
        more = f" (first {NOTABLE_LIMIT} of {group['total']})" if group["total"] > NOTABLE_LIMIT else ""
        body.append(f"<p><b>{escape(key.replace('_', ' '))}</b> ({group['total']}){more}: {links}</p>")
    if not any_notable:
        body.append("<p>None.</p>")

    body.append("<h2>Decisions and events</h2>")
    for e in trace.events:
        if e["type"] not in EXPLAINED:
            continue
        eid = escape(e["event_id"], quote=True)
        try:
            x = explain_event(trace, e["event_id"])
            text, lims, alt, later = x.text, x.limitations, x.contrast, x.later
        except ExplainError as exc:
            text, lims, alt, later = "Explanation unavailable.", [str(exc)], None, None
        extra = (f'<div class="alt">Alternative: {escape(alt)}</div>' if alt else "") + \
                (f'<div class="later">{escape(later)}</div>' if later else "")
        lim_html = "".join(f'<div class="lim">Limitation: {escape(lim)}</div>' for lim in lims)
        body.append(f'<div class="entry" id="{eid}"><h3>{escape(e["type"])} &middot; {eid} &middot; '
                    f'{e["sim_time_s"]}s</h3><div>{escape(text)}</div>{extra}{lim_html}</div>')

    problems = list(trace.problems)
    errors = [e for e in trace.events if e["type"] == "error" and not e["payload"].get("recovered")]
    blocks = [e for e in trace.events if e["type"] == "error" and e["payload"].get("stage") == "unexpected_block"]
    body.append("<h2>Failures and unavailable evidence</h2>")
    if not problems and not errors and not blocks:
        body.append("<p>None recorded.</p>")
    for p in problems:
        body.append(f'<p class="alert">Trace problem: {escape(p)}</p>')
    for e in errors:
        body.append(f'<p class="alert">Error at {e["sim_time_s"]}s ({escape(str(e["payload"].get("kind") or e["payload"].get("stage")))}): '
                    f'{escape(str(e["payload"].get("message")))}</p>')
    for e in blocks:
        p = e["payload"]
        body.append(f'<p class="alert">Unexpected block at {e["sim_time_s"]}s: request '
                    f'{escape(str(p["origin"]["id"]))} for {escape(name(p["target"]))} was blocked '
                    f'({escape(str(p["block_reason"]))}) and discarded.</p>')

    html = ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>Run {escape(run_id)}</title><style>{CSS}</style></head>"
            f"<body><main>{''.join(body)}</main></body></html>\n")
    path = run_dir / REPORT_FILE
    path.write_text(html, encoding="utf-8")
    return path
