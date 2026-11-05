"""Consistency checks between manifest, HACS metadata and constants."""

import json
from pathlib import Path

from custom_components.dots_energy.const import DOMAIN, Category

ROOT = Path(__file__).parent.parent


def test_manifest_and_hacs_consistent() -> None:
    manifest = json.loads(
        (ROOT / "custom_components" / "dots_energy" / "manifest.json").read_text()
    )
    hacs = json.loads((ROOT / "hacs.json").read_text())

    assert manifest["domain"] == DOMAIN == "dots_energy"
    assert manifest["config_flow"] is True
    assert manifest["after_dependencies"] == ["recorder"]
    assert "dependencies" not in manifest
    assert hacs["homeassistant"] == "2026.10.0"
    assert [c.value for c in Category] == [
        "energie",
        "netkosten",
        "heffingen",
        "vaste_kosten",
        "btw",
        "injectie",
        "vergoedingen",
        "overig",
    ]
