"""Write monthly invoice amounts as external recorder statistics."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN, Category

_LABELS = {
    Category.ENERGIE.value: "Energie",
    Category.NETKOSTEN.value: "Netkosten",
    Category.HEFFINGEN.value: "Heffingen",
    Category.VASTE_KOSTEN.value: "Vaste kosten",
    Category.BTW.value: "Btw",
    Category.INJECTIE.value: "Injectie",
    Category.VERGOEDINGEN.value: "Vergoedingen",
    Category.OVERIG.value: "Overig",
    "totaal": "Totaal",
}


async def async_import_statistics(hass: HomeAssistant, monthly: list[dict[str, Any]]) -> None:
    """Rewrite the full monthly series per category (and the total)."""
    if not monthly:
        return
    tz = dt_util.get_default_time_zone()
    for key, label in _LABELS.items():
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Dots {label}",
            source=DOMAIN,
            statistic_id=f"{DOMAIN}:{key}",
            unit_class=None,
            unit_of_measurement="EUR",
        )
        cumulative = Decimal(0)
        rows: list[StatisticData] = []
        for month in monthly:
            year, month_number = (int(part) for part in month["maand"].split("-"))
            amount = Decimal(str(month[key]))
            cumulative += amount
            rows.append(
                StatisticData(
                    start=datetime(year, month_number, 1, tzinfo=tz),
                    state=float(amount),
                    sum=float(cumulative),
                )
            )
        async_add_external_statistics(hass, metadata, rows)
