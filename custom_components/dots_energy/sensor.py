"""Sensors for Dots Energy invoices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DotsConfigEntry
from .categories import Breakdown, categorize_line
from .coordinator import DotsData
from .entity import DotsEntity
from .kpis import invoice_label, newest_first
from .models import InvoiceSummary

EUR = "EUR"


def _num(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _categories(b: Breakdown | None) -> dict[str, float]:
    return {} if b is None else {c.value: float(a) for c, a in b.amounts.items()}


def _invoice_attrs(data: DotsData, s: InvoiceSummary | None) -> dict[str, Any]:
    if s is None:
        return {}
    return {
        "factuurnummer": s.number,
        "periode_start": s.period_start.isoformat(),
        "periode_einde": s.period_end.isoformat(),
        "status": s.status,
        "vervaldatum": s.due_date.isoformat() if s.due_date else None,
        "categorieen": _categories(data.breakdowns.get(s.id)),
    }


def _selected(data: DotsData, selected_id: int | None) -> InvoiceSummary | None:
    return data.by_id.get(selected_id) if selected_id is not None else None


@dataclass(frozen=True, kw_only=True)
class DotsSensorDescription(SensorEntityDescription):
    """Describes a Dots sensor."""

    value_fn: Callable[[DotsData, int | None], Any]
    attrs_fn: Callable[[DotsData, int | None], dict[str, Any]] = lambda data, sel: {}


def _chosen_attrs(data: DotsData, selected_id: int | None) -> dict[str, Any]:
    s = _selected(data, selected_id)
    if s is None:
        return {}
    detail = data.details.get(s.id)
    lines = []
    if detail is not None:
        for line in detail.lines:
            category = categorize_line(line)
            lines.append(
                {
                    "omschrijving": line.description,
                    "categorie": category.value if category else "afronding",
                    "hoeveelheid": _num(line.quantity),
                    "eenheidsprijs": _num(line.unit_price),
                    "bedrag": float(line.amount),
                }
            )
    b = data.breakdowns.get(s.id)
    return {
        "factuurnummer": s.number,
        "periode": s.period_start.strftime("%Y-%m"),
        "status": s.status,
        "regels": lines,
        "categorieen": _categories(b),
        "afwijking": float(b.deviation) if b else 0.0,
    }


def _overview_attrs(data: DotsData, selected_id: int | None) -> dict[str, Any]:
    return {
        "maanden": data.monthly,
        "facturen": [
            {
                "id": s.id,
                "label": invoice_label(s),
                "periode": s.period_start.strftime("%Y-%m"),
                "totaal": float(s.total),
                "status": s.status,
            }
            for s in newest_first(data.summaries)
        ],
    }


def _last_success(data: DotsData, selected_id: int | None) -> datetime:
    return data.last_success


SENSORS: tuple[DotsSensorDescription, ...] = (
    DotsSensorDescription(
        key="laatste_factuur",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=EUR,
        suggested_display_precision=2,
        value_fn=lambda d, sel: _num(d.kpis.last_invoice.total) if d.kpis.last_invoice else None,
        attrs_fn=lambda d, sel: _invoice_attrs(d, d.kpis.last_invoice),
    ),
    DotsSensorDescription(
        key="totaal_dit_jaar",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=EUR,
        suggested_display_precision=2,
        value_fn=lambda d, sel: _num(d.kpis.total_this_year),
    ),
    DotsSensorDescription(
        key="gemiddeld_per_maand",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=EUR,
        suggested_display_precision=2,
        value_fn=lambda d, sel: _num(d.kpis.average_per_month),
    ),
    DotsSensorDescription(
        key="all_in_prijs",
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=3,
        value_fn=lambda d, sel: _num(d.kpis.all_in_price),
    ),
    DotsSensorDescription(
        key="openstaand",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=EUR,
        suggested_display_precision=2,
        value_fn=lambda d, sel: _num(d.kpis.outstanding),
    ),
    DotsSensorDescription(
        key="gekozen_factuur",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=EUR,
        suggested_display_precision=2,
        value_fn=lambda d, sel: _num(s.total) if (s := _selected(d, sel)) else None,
        attrs_fn=_chosen_attrs,
    ),
    DotsSensorDescription(
        key="maandoverzicht",
        value_fn=lambda d, sel: len(d.monthly),
        attrs_fn=_overview_attrs,
    ),
    DotsSensorDescription(
        key="laatst_bijgewerkt",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_last_success,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DotsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Dots sensors."""
    coordinator = entry.runtime_data
    async_add_entities(DotsSensor(coordinator, description) for description in SENSORS)


class DotsSensor(DotsEntity, SensorEntity):
    """A Dots invoice sensor."""

    entity_description: DotsSensorDescription
    _unrecorded_attributes = frozenset({"maanden", "facturen", "regels", "categorieen"})

    def __init__(self, coordinator, description: DotsSensorDescription) -> None:
        super().__init__(coordinator, "sensor", description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(
            self.coordinator.data, self.coordinator.selected_invoice_id
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the sensor attributes."""
        return self.entity_description.attrs_fn(
            self.coordinator.data, self.coordinator.selected_invoice_id
        )
