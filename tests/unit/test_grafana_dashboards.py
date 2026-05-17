"""Sanity tests for the committed Grafana dashboards.

These don't run Grafana — they just guard against the cheap-but-painful
failures: malformed JSON, dashboards that reference metrics we don't emit,
or a datasource UID drift between dashboards and provisioning.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pronaos.observability import metrics as m

DASHBOARD_DIR = (
    Path(__file__).resolve().parents[2]
    / "observability"
    / "grafana"
    / "provisioning"
    / "dashboards"
)


def _dashboard_files() -> list[Path]:
    return sorted(DASHBOARD_DIR.glob("*.json"))


# --------------------------------------------------------------------------- #
# JSON validity                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", _dashboard_files(), ids=lambda p: p.name)
def test_dashboard_json_parses(path: Path) -> None:
    """Each committed dashboard JSON must parse — catches a trailing comma
    before docker compose mounts it."""
    json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", _dashboard_files(), ids=lambda p: p.name)
def test_dashboard_has_uid_and_title(path: Path) -> None:
    """Grafana refuses to load a dashboard without a unique UID. ``title``
    is the human handle in the sidebar."""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("uid"), f"{path.name}: missing 'uid'"
    assert data.get("title"), f"{path.name}: missing 'title'"
    assert isinstance(data.get("panels"), list), f"{path.name}: panels not a list"
    assert data["panels"], f"{path.name}: dashboard has no panels"


# --------------------------------------------------------------------------- #
# Datasource UID matches provisioning                                         #
# --------------------------------------------------------------------------- #


_DATASOURCE_FILE = (
    Path(__file__).resolve().parents[2]
    / "observability"
    / "grafana"
    / "provisioning"
    / "datasources"
    / "datasources.yaml"
)


def test_datasource_uid_matches_dashboards() -> None:
    """Every panel's ``datasource.uid`` must match a UID declared in
    provisioning/datasources/datasources.yaml. Without this guard a
    silent drift here breaks every panel on first load with a generic
    'datasource not found' error that's painful to debug."""
    # Cheap hand-roll instead of pulling PyYAML — the file is tiny and
    # we only need the ``uid:`` lines.
    ds_text = _DATASOURCE_FILE.read_text(encoding="utf-8")
    declared_uids = {
        line.split(":", 1)[1].strip()
        for line in ds_text.splitlines()
        if line.strip().startswith("uid:")
    }
    assert declared_uids, "no datasource UIDs declared in provisioning"

    for path in _dashboard_files():
        data = json.loads(path.read_text(encoding="utf-8"))
        for panel in data["panels"]:
            ds = panel.get("datasource")
            if isinstance(ds, dict) and "uid" in ds:
                assert ds["uid"] in declared_uids, (
                    f"{path.name} panel '{panel.get('title')}' references "
                    f"datasource uid {ds['uid']!r} not in {declared_uids}"
                )


# --------------------------------------------------------------------------- #
# Metric names dashboards rely on must actually be registered                  #
# --------------------------------------------------------------------------- #


def _all_metric_names() -> set[str]:
    """Names of every metric we register at import time."""
    # CollectorRegistry exposes the metrics via _names_to_collectors;
    # using the public collect() returns Metric objects with .name.
    names: set[str] = set()
    for metric in m.REGISTRY.collect():
        # _total and _bucket / _sum / _count are suffixes the client adds
        # at exposition time. The Metric.name is the base.
        names.add(metric.name)
        # Also include the suffixed forms PromQL queries actually reference.
        names.add(f"{metric.name}_total")
        names.add(f"{metric.name}_bucket")
        names.add(f"{metric.name}_sum")
        names.add(f"{metric.name}_count")
    return names


@pytest.mark.parametrize("path", _dashboard_files(), ids=lambda p: p.name)
def test_dashboard_queries_reference_only_known_metrics(path: Path) -> None:
    """Every ``pronaos_*`` symbol that appears in a panel query must be
    a metric we actually register. Otherwise dashboards ship empty and
    "it's broken" is the recruiter's first reaction."""
    import re

    data = json.loads(path.read_text(encoding="utf-8"))
    known = _all_metric_names()
    pattern = re.compile(r"pronaos_[a-z_]+")
    missing: list[str] = []
    for panel in data["panels"]:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            for ref in pattern.findall(expr):
                if ref not in known:
                    missing.append(f"{panel.get('title')!r}: {ref}")
    assert not missing, (
        f"{path.name} references metrics not registered: {missing}.\n"
        f"Known: {sorted(n for n in known if n.startswith('pronaos_'))}"
    )
