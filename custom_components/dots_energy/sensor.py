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
    SensorStateClass,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change

from . import DotsConfigEntry
from .categories import Breakdown, categorize_line
from .const import OPT_PRIJS
from .coordinator import DotsData
from .entity import DotsEntity
from .forecast import DotsForecastCoordinator, ForecastData
from .kpis import invoice_label, newest_first
from .models import InvoiceSummary
from .tariff import Bill, UnitPrices

EUR = "EUR"
EUR_PER_KWH = "EUR/kWh"
QUARTER_MINUTES = (0, 15, 30, 45)


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
    coordinator = entry.runtime_data.invoices
    async_add_entities(DotsSensor(coordinator, description) for description in SENSORS)
    forecast = entry.runtime_data.forecast
    async_add_entities(
        DotsForecastSensor(forecast, description) for description in FORECAST_SENSORS
    )
    if entry.options.get(OPT_PRIJS):
        async_add_entities(
            DotsUnitPriceSensor(forecast, description) for description in UNIT_PRICE_SENSORS
        )


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


def _money(value: Decimal) -> float:
    return round(float(value), 2)


def _bill_categories(bill: Bill) -> dict[str, float]:
    return {c.value: _money(a) for c, a in bill.by_category().items()}


def _round_opt(value: Decimal | None, digits: int) -> float | None:
    return None if value is None else round(float(value), digits)


def _common(data: ForecastData, bill: Bill) -> dict[str, Any]:
    return {
        "maand": data.month,
        "categorieen": _bill_categories(bill),
        "kwaliteit": data.quality,
        "ontbreekt": sorted(data.forecast.missing),
        "berekend_op": data.computed_at.isoformat(),
    }


def _series(series: tuple[tuple[int, Decimal], ...]) -> list[dict[str, Any]]:
    return [{"dag": day, "totaal": _money(total)} for day, total in series]


def _expected_attrs(data: ForecastData) -> dict[str, Any]:
    bill = data.forecast.expected
    return _common(data, bill) | {
        "regels": [
            {
                "omschrijving": line.description,
                "categorie": line.category.value,
                "hoeveelheid": _round_opt(line.quantity, 3),
                "eenheidsprijs": _round_opt(line.unit_price, 4),
                "bedrag": _money(line.amount),
            }
            for line in bill.lines
        ],
        "vorige_factuur": (
            _money(data.previous_invoice_total) if data.previous_invoice_total is not None else None
        ),
        "voorspellingen": [
            {
                "maand": p.month,
                "totaal": _money(p.total),
                "categorieen": {k: _money(v) for k, v in p.categories.items()},
                "berekend_op": p.computed_at,
            }
            for p in data.past_forecasts
        ],
    }


def _so_far_attrs(data: ForecastData) -> dict[str, Any]:
    f = data.forecast
    return _common(data, f.so_far) | {
        "dag": f.day,
        "dagen_in_maand": f.days_in_month,
        "dagreeks": _series(f.daily),
        "vorige_maand": data.previous_month,
        "dagreeks_vorige_maand": _series(data.previous_daily),
    }


@dataclass(frozen=True, kw_only=True)
class DotsForecastSensorDescription(SensorEntityDescription):
    """Describes a forecast sensor."""

    value_fn: Callable[[ForecastData], float]
    attrs_fn: Callable[[ForecastData], dict[str, Any]]


FORECAST_SENSORS: tuple[DotsForecastSensorDescription, ...] = (
    DotsForecastSensorDescription(
        key="verwachte_factuur",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=EUR,
        suggested_display_precision=2,
        value_fn=lambda d: _money(d.forecast.expected.total),
        attrs_fn=_expected_attrs,
    ),
    DotsForecastSensorDescription(
        key="factuur_tot_nu_toe",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=EUR,
        suggested_display_precision=2,
        value_fn=lambda d: _money(d.forecast.so_far.total),
        attrs_fn=_so_far_attrs,
    ),
)


class DotsForecastSensor(DotsEntity, SensorEntity):
    """Forecast of the running month's invoice."""

    entity_description: DotsForecastSensorDescription
    _unrecorded_attributes = frozenset(
        {"regels", "categorieen", "dagreeks", "dagreeks_vorige_maand", "voorspellingen"}
    )

    def __init__(
        self, coordinator: DotsForecastCoordinator, description: DotsForecastSensorDescription
    ) -> None:
        super().__init__(coordinator, "sensor", description.key)  # type: ignore[arg-type]
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Available while the last forecast succeeded and produced a result."""
        return self.coordinator.last_update_success and self.coordinator.data is not None

    @property
    def native_value(self) -> float | None:
        """Total of the bill."""
        data = self.coordinator.data
        return self.entity_description.value_fn(data) if data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Breakdown and series."""
        data = self.coordinator.data
        return self.entity_description.attrs_fn(data) if data else {}


def _rounded(value: Decimal | None, digits: int) -> float | None:
    return None if value is None else round(float(value), digits)


@dataclass(frozen=True, kw_only=True)
class DotsUnitPriceSensorDescription(SensorEntityDescription):
    """Describes a price-per-kWh-now sensor."""

    value_fn: Callable[[UnitPrices], Decimal | None]
    attrs_fn: Callable[[UnitPrices, DotsForecastCoordinator], dict[str, Any]]


def _afname_attrs(p: UnitPrices, c: DotsForecastCoordinator) -> dict[str, Any]:
    markup = c.params.energy_markup if c.params else None
    return {
        "marktprijs": _rounded(p.market, 5),
        "opslag": _rounded(markup, 4),
        "opslag_geleerd": markup is not None,
        "toeslagen": _rounded(p.surcharges_in, 5),
        "btw": _rounded(c.rules.vat_rate, 4) if c.rules else None,
    }


def _injectie_attrs(p: UnitPrices, c: DotsForecastCoordinator) -> dict[str, Any]:
    markup = c.params.injection_markup if c.params else None
    return {
        "marktprijs": _rounded(p.market, 5),
        "opslag": _rounded(markup, 4),
        "opslag_geleerd": markup is not None,
        "toeslagen": _rounded(p.surcharges_out, 5),
    }


UNIT_PRICE_SENSORS: tuple[DotsUnitPriceSensorDescription, ...] = (
    DotsUnitPriceSensorDescription(
        key="afnameprijs_nu",
        native_unit_of_measurement=EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda p: p.afname,
        attrs_fn=_afname_attrs,
    ),
    DotsUnitPriceSensorDescription(
        key="injectieprijs_nu",
        native_unit_of_measurement=EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda p: p.injectie,
        attrs_fn=_injectie_attrs,
    ),
)


class DotsUnitPriceSensor(DotsEntity, SensorEntity):
    """Price of one kWh now: market price plus what the last invoices add.

    Unknown until the markup on the market price is learned from an invoice month.
    """

    entity_description: DotsUnitPriceSensorDescription

    def __init__(
        self, coordinator: DotsForecastCoordinator, description: DotsUnitPriceSensorDescription
    ) -> None:
        super().__init__(coordinator, "sensor", description.key)  # type: ignore[arg-type]
        self.entity_description = description

    @property
    def _forecast(self) -> DotsForecastCoordinator:
        return self.coordinator  # type: ignore[return-value]

    async def async_added_to_hass(self) -> None:
        """Follow the market price: on every change and at every quarter hour."""
        await super().async_added_to_hass()
        price_id = self._forecast.config_entry.options[OPT_PRIJS]

        @callback
        def _price_changed(event: Event[EventStateChangedData]) -> None:
            self.async_write_ha_state()

        @callback
        def _quarter(now: datetime) -> None:
            self.async_write_ha_state()

        self.async_on_remove(async_track_state_change_event(self.hass, price_id, _price_changed))
        self.async_on_remove(
            async_track_time_change(self.hass, _quarter, minute=QUARTER_MINUTES, second=1)
        )

    @property
    def available(self) -> bool:
        """Available once tariff rules were derived from the invoices."""
        return self._forecast.rules is not None

    @property
    def native_value(self) -> float | None:
        """EUR per kWh, None while the markup or the market price is unknown."""
        prices = self._forecast.unit_prices_now()
        return None if prices is None else _rounded(self.entity_description.value_fn(prices), 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Market price, markup and surcharges behind the value."""
        prices = self._forecast.unit_prices_now()
        if prices is None:
            return {}
        return self.entity_description.attrs_fn(prices, self._forecast)
