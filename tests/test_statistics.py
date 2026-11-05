"""Tests for the external statistics import."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.util import dt as dt_util

from custom_components.dots_energy.statistics import async_import_statistics

MONTHLY = [
    {"maand": "2026-01", "totaal": 80.0, "energie": 80.0, "netkosten": 0.0, "heffingen": 0.0,
     "vaste_kosten": 0.0, "btw": 0.0, "injectie": 0.0, "vergoedingen": 0.0, "overig": 0.0},
    {"maand": "2026-02", "totaal": 75.88, "energie": 35.65, "netkosten": 39.5, "heffingen": 11.5,
     "vaste_kosten": 9.96, "btw": 5.77, "injectie": -10.0, "vergoedingen": -4.0, "overig": -12.5},
]


async def test_statistics_rows_and_cumulative_sum(hass) -> None:
    await hass.config.async_set_time_zone("Europe/Brussels")
    with patch(
        "custom_components.dots_energy.statistics.async_add_external_statistics"
    ) as add:
        await async_import_statistics(hass, MONTHLY)

    calls = {c.args[1]["statistic_id"]: (c.args[1], c.args[2]) for c in add.call_args_list}
    assert sorted(calls) == sorted(
        f"dots_energy:{k}"
        for k in ("energie", "netkosten", "heffingen", "vaste_kosten", "btw", "injectie",
                  "vergoedingen", "overig", "totaal")
    )
    meta, rows = calls["dots_energy:energie"]
    assert meta["source"] == "dots_energy"
    assert meta["unit_of_measurement"] == "EUR"
    assert meta["has_sum"] is True
    assert meta["mean_type"] == StatisticMeanType.NONE
    assert meta["name"] == "Dots Energie"
    tz = dt_util.get_default_time_zone()
    assert [r["start"] for r in rows] == [
        datetime(2026, 1, 1, tzinfo=tz),
        datetime(2026, 2, 1, tzinfo=tz),
    ]
    assert [r["state"] for r in rows] == [80.0, 35.65]
    assert [r["sum"] for r in rows] == [80.0, 115.65]
    _, total_rows = calls["dots_energy:totaal"]
    assert [r["sum"] for r in total_rows] == [80.0, 155.88]


async def test_statistics_empty_does_nothing(hass) -> None:
    with patch(
        "custom_components.dots_energy.statistics.async_add_external_statistics"
    ) as add:
        await async_import_statistics(hass, [])
    add.assert_not_called()
