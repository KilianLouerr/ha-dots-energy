"""Diagnostics without personal data."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import DotsConfigEntry
from .const import CONF_LOCATION_ID, CONF_REFRESH_TOKEN
from .forecast import forecast_diagnostics
from .kpis import newest_first

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, CONF_REFRESH_TOKEN, CONF_LOCATION_ID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DotsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics; invoice numbers are replaced by an index."""
    coordinator = entry.runtime_data.invoices
    data = coordinator.data
    result: dict[str, Any] = {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "forecast": forecast_diagnostics(entry.runtime_data.forecast),
    }
    if data is None:
        return result
    result["data"] = {
        "invoices": [
            {
                "index": index,
                "period": s.period_start.strftime("%Y-%m"),
                "invoice_type": s.invoice_type,
                "status": s.status,
                "total": float(s.total),
                "has_detail": s.id in data.details,
                "deviation": float(data.breakdowns[s.id].deviation),
                "categories": {c.value: float(a) for c, a in data.breakdowns[s.id].amounts.items()},
            }
            for index, s in enumerate(newest_first(data.summaries))
        ],
        "kpis": {
            "total_this_year": float(data.kpis.total_this_year),
            "average_per_month": (
                float(data.kpis.average_per_month) if data.kpis.average_per_month else None
            ),
            "all_in_price": float(data.kpis.all_in_price) if data.kpis.all_in_price else None,
            "outstanding": float(data.kpis.outstanding),
        },
        "contract": data.contract,
        "last_success": data.last_success.isoformat(),
    }
    return result
