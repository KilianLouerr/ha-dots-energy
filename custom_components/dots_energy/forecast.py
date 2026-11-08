"""Hourly forecast of the running month's invoice, on quarter-hour prices."""

from __future__ import annotations

import calendar
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    OPT_AFNAME,
    OPT_CAPACITEIT,
    OPT_GESTUURD,
    OPT_INJECTIE,
    OPT_PRIJS,
    OPT_PRIJS_INCL_BTW,
)
from .fit import MonthMeasured, fit_params
from .kpis import is_credit_note
from .models import InvoiceDetail
from .projection import (
    SLOT,
    SLOTS_PER_DAY,
    Forecast,
    MonthContext,
    MonthMeasuredDaily,
    SlotSeries,
    cumulative_series,
    forecast,
    merge_quarters,
    usage_until_by_series,
)
from .sources import (
    async_hourly_changes,
    async_price_history,
    async_quarter_changes,
    capacity_kw,
    current_price,
    raw_prices,
    slot_floor,
)
from .tariff import Bill, FitParams, MonthUsage, TariffRules, derive_rules

if TYPE_CHECKING:
    from .coordinator import DotsCoordinator, DotsData

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
KEEP_MONTHS = 13
UPDATE_MINUTE = 12
SAVE_DELAY = 10
RECENT_WINDOW = timedelta(hours=48)
VALUE_PRECISION = Decimal("0.000001")

Series = tuple[tuple[int, Decimal], ...]


@dataclass(frozen=True)
class PastForecast:
    """The last forecast made for a month that has ended."""

    month: str
    total: Decimal
    categories: dict[str, Decimal]
    computed_at: str | None


@dataclass
class ForecastData:
    """What the forecast sensors show."""

    month: str
    forecast: Forecast
    previous_daily: Series
    previous_month: str
    previous_invoice_total: Decimal | None
    quality: Literal["volledig", "ruw"]
    computed_at: datetime
    past_forecasts: tuple[PastForecast, ...] = ()


def month_key(moment: datetime | date) -> str:
    """'YYYY-MM' of a moment."""
    return moment.strftime("%Y-%m")


def month_start_of(moment: datetime) -> datetime:
    """Local midnight on the first day of the moment's month."""
    return dt_util.as_local(moment).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def shift_month(month_start: datetime, months: int) -> datetime:
    """Local start of the month `months` away from `month_start`."""
    index = month_start.year * 12 + month_start.month - 1 + months
    naive = month_start.replace(tzinfo=None, year=index // 12, month=index % 12 + 1, day=1)
    return naive.replace(tzinfo=month_start.tzinfo)


def _categories(bill: Bill) -> dict[str, str]:
    return {c.value: str(a) for c, a in bill.by_category().items()}


# --- compact storage: one list of 96 quarter values per UTC day -----------------


def encode_days(values: Mapping[datetime, Decimal]) -> dict[str, list[str | None]]:
    """{UTC day: [96 values or None]} with values rounded to 6 decimals."""
    days: dict[str, list[str | None]] = {}
    for slot, value in values.items():
        utc = slot.astimezone(UTC)
        row = days.setdefault(utc.date().isoformat(), [None] * SLOTS_PER_DAY)
        row[(utc.hour * 60 + utc.minute) // 15] = str(value.quantize(VALUE_PRECISION).normalize())
    return dict(sorted(days.items()))


def decode_days(days: Mapping[str, list[str | None]]) -> dict[datetime, Decimal]:
    """Inverse of encode_days."""
    values: dict[datetime, Decimal] = {}
    for day, row in days.items():
        base = datetime.fromisoformat(day).replace(tzinfo=UTC)
        for index, value in enumerate(row):
            if value is not None:
                values[base + SLOT * index] = Decimal(value)
    return values


def _decode_prices(stored: Mapping[str, Any]) -> dict[datetime, Decimal]:
    """Prices from the Store; v0.2.0 kept one price per hour, keyed by ISO time."""
    if any("T" in key for key in stored):
        hourly = {datetime.fromisoformat(k): Decimal(v) for k, v in stored.items()}
        return spread_prices(hourly)
    return decode_days(stored)


def spread_prices(hourly: Mapping[datetime, Decimal]) -> dict[datetime, Decimal]:
    """An hourly price applies to each of its four quarters."""
    return {hour + SLOT * index: price for hour, price in hourly.items() for index in range(4)}


def past_forecasts(snapshots: Mapping[str, Any], current: str) -> tuple[PastForecast, ...]:
    """The last stored forecast of every month before `current`, newest first."""
    result = []
    for month in sorted((m for m in snapshots if m < current), reverse=True):
        last = snapshots[month].get("laatste")
        if not last:
            continue
        result.append(
            PastForecast(
                month=month,
                total=Decimal(last["totaal"]),
                categories={k: Decimal(v) for k, v in last.get("categorieen", {}).items()},
                computed_at=last.get("berekend_op"),
            )
        )
    return tuple(result)


def _usage_to_dict(m: MonthMeasured) -> dict[str, Any]:
    u = m.usage

    def opt(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    return {
        "days": str(u.days),
        "days_in_month": u.days_in_month,
        "kwh_in": str(u.kwh_in),
        "kwh_out": opt(u.kwh_out),
        "priced_in": [str(v) for v in u.priced_in],
        "priced_out": [str(v) for v in u.priced_out],
        "capacity_kw": opt(u.capacity_kw),
        "steered_kwh": opt(u.steered_kwh),
        "invalid_hours": m.invalid_hours,
        "invalid_out": m.invalid_out,
        "invalid_steered": m.invalid_steered,
    }


def _usage_from_dict(month: str, d: Mapping[str, Any]) -> MonthMeasured:
    def opt(value: str | None) -> Decimal | None:
        return None if value is None else Decimal(value)

    usage = MonthUsage(
        days=Decimal(d["days"]),
        days_in_month=int(d["days_in_month"]),
        kwh_in=Decimal(d["kwh_in"]),
        kwh_out=opt(d["kwh_out"]),
        priced_in=(Decimal(d["priced_in"][0]), Decimal(d["priced_in"][1])),
        priced_out=(Decimal(d["priced_out"][0]), Decimal(d["priced_out"][1])),
        capacity_kw=opt(d["capacity_kw"]),
        steered_kwh=opt(d["steered_kwh"]),
    )
    return MonthMeasured(
        month=month,
        usage=usage,
        invalid_hours=int(d["invalid_hours"]),
        invalid_out=int(d["invalid_out"]),
        invalid_steered=int(d["invalid_steered"]),
    )


def _measured(ctx: MonthContext) -> MonthMeasured:
    """A whole month measured (runs in the executor)."""
    usage, invalid = usage_until_by_series(ctx, ctx.now)
    return MonthMeasured(
        month=month_key(ctx.month_start),
        usage=usage,
        invalid_hours=invalid["afname"],
        invalid_out=invalid["injectie"],
        invalid_steered=invalid["gestuurd"],
    )


def _daily_average(measured: MonthMeasured, has_data: bool) -> MonthMeasuredDaily | None:
    if not has_data:
        return None
    usage = measured.usage
    days = Decimal(usage.days_in_month)
    return MonthMeasuredDaily(
        kwh_in_per_day=usage.kwh_in / days,
        kwh_out_per_day=(usage.kwh_out or Decimal(0)) / days,
        steered_per_day=usage.steered_kwh / days if usage.steered_kwh is not None else None,
    )


class DotsForecastCoordinator(DataUpdateCoordinator[ForecastData | None]):
    """Recomputes the forecast every hour and whenever the invoices change."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, invoices: DotsCoordinator) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN}_forecast", update_interval=None
        )
        self._invoices = invoices
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.forecast"
        )
        self._stored: dict[str, Any] | None = None
        self._prices: dict[datetime, Decimal] = {}
        self._quarters: dict[str, dict[datetime, Decimal]] = {}
        self._seen_invoices: datetime | None = None

    @property
    def stored(self) -> dict[str, Any]:
        """Persisted state: capacity, month totals, snapshots, previous month curve."""
        return self._stored or {}

    def price_at(self, slot: datetime) -> Decimal | None:
        """Stored market price (without VAT) of a quarter hour."""
        return self._prices.get(slot.astimezone(UTC))

    def quarter_at(self, entity_id: str, slot: datetime) -> Decimal | None:
        """Stored quarter-hour kWh of a source entity."""
        return self._quarters.get(entity_id, {}).get(slot.astimezone(UTC))

    async def async_start(self) -> CALLBACK_TYPE:
        """Refresh now, then every hour and when the invoices change."""
        invoices = self._invoices
        self._seen_invoices = invoices.data.last_success if invoices.data else None
        await self.async_refresh()

        @callback
        def _hourly(now: datetime) -> None:
            self.config_entry.async_create_task(self.hass, self.async_refresh())

        @callback
        def _invoices_changed() -> None:
            stamp = invoices.data.last_success if invoices.data else None
            if stamp != self._seen_invoices:
                self._seen_invoices = stamp
                self.config_entry.async_create_task(self.hass, self.async_request_refresh())

        unsubs = [
            async_track_time_change(self.hass, _hourly, minute=UPDATE_MINUTE, second=0),
            invoices.async_add_listener(_invoices_changed),
        ]

        @callback
        def _stop() -> None:
            for unsub in unsubs:
                unsub()

        return _stop

    async def async_flush(self) -> None:
        """Persist the Store now."""
        if self._stored is not None:
            await self._store.async_save(self._data_to_save())

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        stored = self.stored
        stored["prices"] = encode_days(self._prices)
        stored["quarters"] = {eid: encode_days(v) for eid, v in self._quarters.items()}
        return stored

    async def _async_load(self) -> dict[str, Any]:
        if self._stored is None:
            loaded = await self._store.async_load() or {}
            self._stored = {
                "price_backfill": loaded.get("price_backfill"),
                "quarter_backfill": list(loaded.get("quarter_backfill", [])),
                "measured": dict(loaded.get("measured", {})),
                "sources": loaded.get("sources"),
                "capacity": dict(loaded.get("capacity", {})),
                "snapshots": dict(loaded.get("snapshots", {})),
                "previous_daily": dict(loaded.get("previous_daily", {})),
            }
            stored_prices = loaded.get("prices", {})
            if any("T" in key for key in stored_prices):
                # v0.2.0 kept hourly averages: fetch the recorded history again per quarter.
                self._stored["price_backfill"] = None
            self._prices = _decode_prices(stored_prices)
            self._quarters = {
                eid: decode_days(days) for eid, days in loaded.get("quarters", {}).items()
            }
        return self._stored

    async def _async_update_data(self) -> ForecastData | None:
        options = self.config_entry.options
        data: DotsData | None = self._invoices.data
        if (
            not options.get(OPT_AFNAME)
            or data is None
            or "recorder" not in self.hass.config.components
        ):
            return None
        details = list(data.details.values())
        rules = derive_rules(details, data.breakdowns)
        if rules is None:
            return None
        stored = await self._async_load()
        now = dt_util.now()
        month_start = month_start_of(now)
        prev_start = shift_month(month_start, -1)
        run = self.hass.async_add_executor_job
        try:
            self._check_sources(options)
            await self._async_update_prices(options, rules.vat_rate, prev_start, now)
            await self._async_update_quarters(options, prev_start, now)
            capacity = capacity_kw(self.hass, options.get(OPT_CAPACITEIT))
            if capacity is not None:
                stored["capacity"][month_key(month_start)] = str(capacity)
            prev_ctx = await self._async_context(options, prev_start, month_start, None)
            prev_measured = await run(_measured, prev_ctx)
            stored["measured"][prev_measured.month] = _usage_to_dict(prev_measured)
            params = await self._async_fit(options, details, data, month_start, prev_measured)
            previous = _daily_average(prev_measured, bool(prev_ctx.afname.values))
            ctx = await self._async_context(options, month_start, now, previous)
            result = await run(forecast, ctx, rules, params)
            previous_daily = await self._async_previous_series(prev_ctx, rules, params, now)
        except Exception as err:  # recorder failures come in many types
            raise UpdateFailed(f"Voorspelling niet berekend: {err}") from err
        self._snapshot(month_key(month_start), now, result.expected)
        self._prune(month_start)
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)
        last = data.kpis.last_invoice
        return ForecastData(
            month=month_key(month_start),
            forecast=result,
            previous_daily=previous_daily,
            previous_month=month_key(prev_start),
            previous_invoice_total=last.total if last else None,
            quality="ruw" if result.missing else "volledig",
            computed_at=dt_util.utcnow(),
            past_forecasts=past_forecasts(stored["snapshots"], month_key(month_start)),
        )

    def _check_sources(self, options: Mapping[str, Any]) -> None:
        """Month totals and prices are only valid for the sources they were measured with."""
        sources = {
            "meting": [
                options.get(OPT_AFNAME),
                options.get(OPT_INJECTIE),
                sorted(options.get(OPT_GESTUURD) or []),
            ],
            "prijs": [options.get(OPT_PRIJS), bool(options.get(OPT_PRIJS_INCL_BTW, False))],
        }
        previous = self.stored.get("sources")
        if previous is not None and previous != sources:
            self.stored["measured"] = {}
            self.stored["previous_daily"] = {}
            if previous.get("prijs") != sources["prijs"]:
                self._prices = {}
                self.stored["price_backfill"] = None
        self.stored["sources"] = sources

    def _recent_start(self, done: bool, prev_start: datetime, now: datetime) -> datetime:
        """From the previous month on the first run for a source, else the last 48 hours."""
        start = slot_floor(now) - RECENT_WINDOW
        return start if done else min(start, prev_start.astimezone(UTC))

    async def _async_update_prices(
        self, options: Mapping[str, Any], vat_rate: Decimal, prev_start: datetime, now: datetime
    ) -> None:
        price_id = options.get(OPT_PRIJS)
        if not price_id:
            return
        incl_vat = bool(options.get(OPT_PRIJS_INCL_BTW, False))
        open_slot = slot_floor(now)
        # Closed quarters: time-weighted average of the recorded state history, so a
        # missed run is recovered later.
        start = self._recent_start(self.stored.get("price_backfill") == price_id, prev_start, now)
        history = await async_price_history(
            self.hass, price_id, start, open_slot, incl_vat, vat_rate
        )
        self._prices.update(history)
        self.stored["price_backfill"] = price_id
        # Published quarter prices are exact: they win over the averaged state history.
        self._prices.update(raw_prices(self.hass, price_id, incl_vat, vat_rate))
        for slot, price in current_price(self.hass, price_id, incl_vat, vat_rate).items():
            if slot >= open_slot:
                self._prices.setdefault(slot, price)

    @staticmethod
    def _sources(options: Mapping[str, Any]) -> list[str]:
        ids = [options[OPT_AFNAME]]
        if options.get(OPT_INJECTIE):
            ids.append(options[OPT_INJECTIE])
        ids.extend(options.get(OPT_GESTUURD) or [])
        return ids

    async def _async_update_quarters(
        self, options: Mapping[str, Any], prev_start: datetime, now: datetime
    ) -> None:
        """Keep quarter-hour kWh from the short-lived 5-minute statistics."""
        done = self.stored["quarter_backfill"]
        for entity_id in self._sources(options):
            start = self._recent_start(entity_id in done, prev_start, now)
            quarters = await async_quarter_changes(self.hass, entity_id, start, slot_floor(now))
            self._quarters.setdefault(entity_id, {}).update(quarters)
            if entity_id not in done:
                done.append(entity_id)

    async def _async_series(
        self, entity_ids: list[str], start: datetime, end: datetime
    ) -> SlotSeries | None:
        """Quarter-hour kWh summed over entities; a quarter counts only if all have it."""
        if not entity_ids:
            return None
        lo, hi = start.astimezone(UTC), end.astimezone(UTC)
        parts = []
        for entity_id in entity_ids:
            hourly = await async_hourly_changes(self.hass, entity_id, start, end)
            quarters = {q: v for q, v in self._quarters.get(entity_id, {}).items() if lo <= q < hi}
            parts.append(merge_quarters(hourly, quarters))
        common = set(parts[0]).intersection(*parts[1:])
        return SlotSeries(values={q: sum((p[q] for p in parts), Decimal(0)) for q in common})

    async def _async_context(
        self,
        options: Mapping[str, Any],
        start: datetime,
        now: datetime,
        previous: MonthMeasuredDaily | None,
    ) -> MonthContext:
        end = min(now, shift_month(start, 1))
        injectie = options.get(OPT_INJECTIE)
        capacity = self.stored["capacity"].get(month_key(start))
        afname = await self._async_series([options[OPT_AFNAME]], start, end)
        return MonthContext(
            month_start=start,
            now=end,
            afname=afname or SlotSeries(values={}),
            injectie=await self._async_series([injectie] if injectie else [], start, end),
            gestuurd=await self._async_series(list(options.get(OPT_GESTUURD) or []), start, end),
            prices=dict(self._prices),
            capacity_kw=Decimal(capacity) if capacity is not None else None,
            previous=previous,
        )

    async def _async_fit(
        self,
        options: Mapping[str, Any],
        details: list[InvoiceDetail],
        data: DotsData,
        month_start: datetime,
        prev_measured: MonthMeasured,
    ) -> FitParams:
        """Fit on invoiced months; complete older months are kept as totals in the Store."""
        oldest = shift_month(month_start, -KEEP_MONTHS)
        prev_key = prev_measured.month
        kept = self.stored["measured"]
        measured: dict[str, MonthMeasured] = {}
        for detail in details:
            if is_credit_note(detail.summary):
                continue
            period = detail.summary.period_start
            start = datetime(period.year, period.month, 1, tzinfo=month_start.tzinfo)
            key = month_key(start)
            if start >= month_start or start < oldest or key in measured:
                continue
            if key == prev_key:
                measured[key] = prev_measured
            elif key in kept:
                measured[key] = _usage_from_dict(key, kept[key])
            else:
                ctx = await self._async_context(options, start, shift_month(start, 1), None)
                measured[key] = await self.hass.async_add_executor_job(_measured, ctx)
                kept[key] = _usage_to_dict(measured[key])
        return fit_params(details, data.breakdowns, measured)

    async def _async_previous_series(
        self, ctx: MonthContext, rules: TariffRules, params: FitParams, now: datetime
    ) -> Series:
        cached = self.stored["previous_daily"]
        key = month_key(ctx.month_start)
        today = dt_util.as_local(now).date().isoformat()
        if cached.get("month") == key and cached.get("computed_on") == today:
            return tuple((int(day), Decimal(total)) for day, total in cached["series"])
        series: Series = ()
        if ctx.afname.values:
            days = calendar.monthrange(ctx.month_start.year, ctx.month_start.month)[1]
            series = await self.hass.async_add_executor_job(
                cumulative_series, ctx, rules, params, days
            )
        self.stored["previous_daily"] = {
            "month": key,
            "computed_on": today,
            "series": [[day, str(total)] for day, total in series],
        }
        return series

    def _snapshot(self, month: str, now: datetime, expected: Bill) -> None:
        snapshots = self.stored["snapshots"].setdefault(month, {})
        entry = {"totaal": str(expected.total), "categorieen": _categories(expected)}
        snapshots.setdefault(dt_util.as_local(now).date().isoformat(), entry)
        snapshots["laatste"] = entry | {"berekend_op": dt_util.utcnow().isoformat()}

    def _prune(self, month_start: datetime) -> None:
        """Quarters and prices: previous and running month only; totals: 13 months."""
        keep_from = shift_month(month_start, -1).astimezone(UTC)
        self._prices = {q: p for q, p in self._prices.items() if q >= keep_from}
        self._quarters = {
            eid: {q: v for q, v in values.items() if q >= keep_from}
            for eid, values in self._quarters.items()
        }
        cutoff_key = month_key(shift_month(month_start, -KEEP_MONTHS))
        for bucket in ("snapshots", "capacity", "measured"):
            stored = self.stored[bucket]
            for key in [k for k in stored if k < cutoff_key]:
                stored.pop(key)


def forecast_diagnostics(coordinator: DotsForecastCoordinator) -> dict[str, Any]:
    """Forecast part of the diagnostics, without entity ids."""
    stored = coordinator.stored
    result: dict[str, Any] = {
        "last_update_success": coordinator.last_update_success,
        "sources_configured": sorted(k for k, v in coordinator.config_entry.options.items() if v),
        "stored_price_quarters": len(coordinator._prices),
        "stored_quarter_values": sum(len(v) for v in coordinator._quarters.values()),
        "measured_months": sorted(stored.get("measured", {})),
        "capacity_months": sorted(stored.get("capacity", {})),
        "snapshot_months": sorted(stored.get("snapshots", {})),
    }
    data = coordinator.data
    if data is not None:
        f = data.forecast
        result |= {
            "month": data.month,
            "quality": data.quality,
            "missing": sorted(f.missing),
            "so_far": {c.value: float(a) for c, a in f.so_far.by_category().items()},
            "expected": {c.value: float(a) for c, a in f.expected.by_category().items()},
        }
    return result
