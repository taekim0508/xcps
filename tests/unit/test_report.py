"""Section G (report): offline HTML, escaped content, markers link to real event anchors."""

import json
import re

from traffic_cps.report import build_report
from tests.unit.runs import switch_run


def test_contract_G_every_marker_links_to_a_unique_existing_anchor(tmp_path):
    run = switch_run(tmp_path)
    html = build_report(run).read_text()
    hrefs = re.findall(r'href="#([^"]+)"', html)
    ids = re.findall(r'\sid="([^"]+)"', html)
    assert hrefs, "report has no markers"
    assert len(ids) == len(set(ids))
    assert set(hrefs) <= set(ids)
    assert "dec-10" in hrefs
    event_ids = {json.loads(line)["event_id"] for line in (run / "events.jsonl").read_text().splitlines()}
    assert set(hrefs) <= event_ids


def test_contract_G_report_is_offline_and_escaped(tmp_path):
    run = switch_run(tmp_path)
    manifest = json.loads((run / "run.manifest.json").read_text())
    manifest["controller"] = "<script>alert(1)</script>"
    (run / "run.manifest.json").write_text(json.dumps(manifest))
    html = build_report(run).read_text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert not re.search(r'(src|href)="(https?:)?//', html)


def test_contract_G_report_shows_request_and_actual_execution(tmp_path):
    html = build_report(switch_run(tmp_path)).read_text()
    assert "requested W/E green" in html
    assert "W/E became green at 12s" in html
    assert "<svg" in html
    assert "Q-table" not in html  # baselines show no fake Q-table grid


def test_contract_G_baseline_report_has_no_policy_grid(tmp_path):
    html = build_report(switch_run(tmp_path)).read_text()
    assert '<table class="q"' not in html and "No learned policy to show" in html
