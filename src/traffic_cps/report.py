"""Static offline HTML run report, minimal timeline (build contract, Section G).

Built only from a run's manifests and event log; never launches SUMO. All
dynamic text is HTML-escaped, graphics are inline SVG, and every marker links
to a unique anchor whose id is the event ID it explains.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from traffic_cps.events import REPORT_FILE
from traffic_cps.explain import ExplainError, explain_event, load_trace, name

PLOT_W, LEFT, RIGHT = 940, 50, 10
QUEUE_H, BAND_H, MARK_H = 170, 16, 26
SERIES = {"NS": "var(--ns)", "WE": "var(--we)"}
LIGHT = {"green": "var(--green)", "yellow": "var(--yellow)", "red": "var(--red)"}
EXPLAINED = ("decision", "operating_action", "safety_override", "safety_violation")

CSS = """
:root { --bg:#ffffff; --fg:#1d232a; --muted:#5d6770; --grid:#e3e7eb; --card:#f6f8fa;
  --ns:#2f6fdd; --we:#c2551f; --green:#2e9d4f; --yellow:#e0b100; --red:#d0d4d8; --alert:#c62828; }
@media (prefers-color-scheme: dark) { :root { --bg:#14181c; --fg:#e6e9ec; --muted:#9aa4ad; --grid:#2a3138;
  --card:#1c2227; --ns:#6c9cff; --we:#ff9a62; --green:#4cc06f; --yellow:#f0c420; --red:#3a4148; --alert:#ff6b6b; } }
body { background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:0; padding:24px 16px; }
main { max-width:1000px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:28px 0 8px; }
.meta { color:var(--muted); margin:0 0 12px; } svg { width:100%; height:auto; display:block; }
svg text { fill:var(--muted); font-size:11px; } .grid { stroke:var(--grid); }
.legend span { display:inline-block; margin-right:14px; color:var(--muted); font-size:13px; }
.swatch { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:middle; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:2px 14px; margin:0; }
dt { color:var(--muted); } dd { margin:0; }
.entry { background:var(--card); border-radius:6px; padding:10px 12px; margin:8px 0; scroll-margin-top:12px; }
.entry:target { outline:2px solid var(--ns); } .entry h3 { font-size:14px; margin:0 0 4px; }
.lim { color:var(--alert); font-size:13px; } .alert { color:var(--alert); }
"""


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


def _svg(trace, horizon: int) -> str:
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
    for d, color in SERIES.items():
        pts = " ".join(f"{_x(e['sim_time_s'], horizon):.1f},{y(e['payload']['counts_by_direction'][d]):.1f}"
                       for e in obs)
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
        label = escape(f"{e['type']} {e['event_id']} at {e['sim_time_s']}s")
        if e["type"] == "decision":
            p = e["payload"]
            fill = SERIES[p["requested"]] if not p["retains"] else "none"
            shape = (f'<circle cx="{cx:.1f}" cy="{cy}" r="4" fill="{fill}" stroke="{SERIES[p["requested"]]}" '
                     f'stroke-width="1.2"/>')
        elif e["type"] == "operating_action":
            shape = (f'<path d="M{cx:.1f},{cy - 6} l6,10 h-12 z" fill="var(--fg)"/>')
        else:
            shape = (f'<path d="M{cx - 5:.1f},{cy - 5} l10,10 M{cx + 5:.1f},{cy - 5} l-10,10" '
                     f'stroke="var(--alert)" stroke-width="2"/>')
        out.append(f'<a href="#{eid}"><title>{label}</title>{shape}</a>')

    for t in range(0, horizon + 1, max(1, horizon // 6)):
        out.append(f'<text x="{_x(t, horizon):.1f}" y="{height - 2}" text-anchor="middle">{t}s</text>')
    out.append("</svg>")
    return "\n".join(out)


def build_report(run_dir: Path | str) -> Path:
    run_dir = Path(run_dir)
    trace = load_trace(run_dir)
    manifest = trace.manifest
    horizon = int(manifest.get("horizon_s") or max([1] + [e["sim_time_s"] for e in trace.events]))
    end = next((e for e in reversed(trace.events) if e["type"] == "run_end"), None)
    completion = end["payload"]["completion"] if end else "unknown (no run_end record)"
    counters = end["payload"]["counters"] if end else {}

    body = [f"<h1>Run report: {escape(str(manifest.get('run_id', run_dir.name)))}</h1>",
            f'<p class="meta">Controller: {escape(str(manifest.get("controller")))} &middot; horizon '
            f"{horizon}s &middot; completion: {escape(str(completion))}</p>"]
    if counters:
        body.append("<dl>" + "".join(f"<dt>{escape(k.replace('_', ' '))}</dt><dd>{v}</dd>"
                                     for k, v in counters.items()) + "</dl>")

    body.append("<h2>Timeline</h2>")
    body.append('<p class="legend"><span><i class="swatch" style="background:var(--ns)"></i>N/S waiting</span>'
                '<span><i class="swatch" style="background:var(--we)"></i>W/E waiting</span>'
                '<span><i class="swatch" style="background:var(--green)"></i>green</span>'
                '<span><i class="swatch" style="background:var(--yellow)"></i>yellow</span>'
                '<span><i class="swatch" style="background:var(--red)"></i>red</span>'
                "<span>&#9679; switch request &middot; &#9675; keep green &middot; &#9650; operating rule "
                "&middot; &#10005; safety</span></p>")
    body.append(_svg(trace, horizon))

    body.append("<h2>Decisions and events</h2>")
    for e in trace.events:
        if e["type"] not in EXPLAINED:
            continue
        eid = escape(e["event_id"], quote=True)
        try:
            x = explain_event(trace, e["event_id"])
            text, lims = x.text, x.limitations
        except ExplainError as exc:
            text, lims = "Explanation unavailable.", [str(exc)]
        lim_html = "".join(f'<div class="lim">Limitation: {escape(lim)}</div>' for lim in lims)
        body.append(f'<div class="entry" id="{eid}"><h3>{escape(e["type"])} &middot; {eid} &middot; '
                    f'{e["sim_time_s"]}s</h3><div>{escape(text)}</div>{lim_html}</div>')

    problems = list(trace.problems)
    errors = [e for e in trace.events if e["type"] == "error" and not e["payload"].get("recovered")]
    body.append("<h2>Failures and unavailable evidence</h2>")
    if not problems and not errors:
        body.append("<p>None recorded.</p>")
    for p in problems:
        body.append(f'<p class="alert">Trace problem: {escape(p)}</p>')
    for e in errors:
        body.append(f'<p class="alert">Error at {e["sim_time_s"]}s ({escape(str(e["payload"].get("kind")))}): '
                    f'{escape(str(e["payload"].get("message")))}</p>')

    html = ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>Run {escape(str(manifest.get('run_id', run_dir.name)))}</title><style>{CSS}</style></head>"
            f"<body><main>{''.join(body)}</main></body></html>\n")
    path = run_dir / REPORT_FILE
    path.write_text(html, encoding="utf-8")
    return path
