"""Hourly forecast of the running month's invoice."""

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
    Forecast,
    HourlySeries,
    MonthContext,
    MonthMeasuredDaily,
    cumulative_series,
    forecast,
    usage_until,
    usage_until_by_series,
)
from .sources import async_hourly_changes, async_price_history, capacity_kw, current_price
from .tariff import Bill, FitParams, TariffRules, derive_rules

if TYPE_CHECKING:
    from .coordinator import DotsCoordinator, DotsData

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
KEEP_MONTHS = 13
UPDATE_MINUTE = 12
SAVE_DELAY = 10
PRICE_HISTORY_WINDOW = timedelta(hours=48)

Series = tuple[tuple[int, Decimal], ...]


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
        self._measured: dict[str, MonthMeasured] = {}
        self._seen_invoices: datetime | None = None

    @property
    def stored(self) -> dict[str, Any]:
        """Persisted state: prices, capacity per month, snapshots, previous month curve."""
        return self._stored or {}

    async def async_start(self) -> CALLBACK_TYPE:
        """Refresh now, then every hour and when the invoices change."""
        invoices = self._invoices
        self._seen_invoices = invoices.data.last_success if invoices.data else None
        await self.async_refresh()

        @callback
        def _hourly(now: datetime) -> None:
            self.hass.async_create_task(self.async_refresh())

        @callback
        def _invoices_changed() -> None:
            stamp = invoices.data.last_success if invoices.data else None
            if stamp != self._seen_invoices:
                self._seen_invoices = stamp
                self.hass.async_create_task(self.async_request_refresh())

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
        stored["prices"] = {h.isoformat(): str(p) for h, p in sorted(self._prices.items())}
        return stored

    async def _async_load(self) -> dict[str, Any]:
        if self._stored is None:
            loaded = await self._store.async_load() or {}
            self._stored = {
                "prices": dict(loaded.get("prices", {})),
                "price_backfill": loaded.get("price_backfill"),
                "capacity": dict(loaded.get("capacity", {})),
                "snapshots": dict(loaded.get("snapshots", {})),
                "previous_daily": dict(loaded.get("previous_daily", {})),
            }
            self._prices = {
                datetime.fromisoformat(h): Decimal(p) for h, p in self._stored["prices"].items()
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
        try:
            await self._async_update_prices(options, rules.vat_rate, prev_start, now)
            capacity = capacity_kw(self.hass, options.get(OPT_CAPACITEIT))
            if capacity is not None:
                stored["capacity"][month_key(month_start)] = str(capacity)
            prev_ctx = await self._async_context(options, prev_start, month_start, None)
            params = await self._async_fit(options, details, data, month_start, prev_ctx)
            ctx = await self._async_context(
                options, month_start, now, self._daily_average(prev_ctx)
            )
        except Exception as err:  # recorder failures come in many types
            raise UpdateFailed(f"Voorspelling niet berekend: {err}") from err
        result = forecast(ctx, rules, params)
        previous_daily = self._previous_series(prev_ctx, rules, params, now)
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
        )

    async def _async_update_prices(
        self, options: Mapping[str, Any], vat_rate: Decimal, prev_start: datetime, now: datetime
    ) -> None:
        price_id = options.get(OPT_PRIJS)
        if not price_id:
            return
        incl_vat = bool(options.get(OPT_PRIJS_INCL_BTW, False))
        open_hour = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        # Closed hours: time-weighted average of the recorded state history, so
        # quarter-hour prices are averaged and a missed run is recovered later.
        start = open_hour - PRICE_HISTORY_WINDOW
        if self.stored.get("price_backfill") != price_id:
            start = min(start, prev_start.astimezone(UTC))
        history = await async_price_history(
            self.hass, price_id, start, open_hour, incl_vat, vat_rate
        )
        self._prices.update(history)
        self.stored["price_backfill"] = price_id
        # Open and future hours: what the sensor knows now.
        for hour, price in current_price(self.hass, price_id, incl_vat, vat_rate).items():
            if hour >= open_hour:
                self._prices[hour] = price
            else:
                self._prices.setdefault(hour, price)

    async def _async_series(
        self, entity_ids: list[str], start: datetime, end: datetime
    ) -> HourlySeries | None:
        """Hourly changes summed over entities; an hour counts only if every entity has it."""
        if not entity_ids:
            return None
        parts = [
            (await async_hourly_changes(self.hass, entity_id, start, end)).values
            for entity_id in entity_ids
        ]
        common = set(parts[0]).intersection(*parts[1:])
        return HourlySeries(values={h: sum((p[h] for p in parts), Decimal(0)) for h in common})

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
            afname=afname or HourlySeries(values={}),
            injectie=await self._async_series([injectie] if injectie else [], start, end),
            gestuurd=await self._async_series(list(options.get(OPT_GESTUURD) or []), start, end),
            prices=self._prices,
            capacity_kw=Decimal(capacity) if capacity is not None else None,
            previous=previous,
        )

    @staticmethod
    def _daily_average(ctx: MonthContext) -> MonthMeasuredDaily | None:
        """Average kWh per day of a whole (previous) month; None without data."""
        if not ctx.afname.values:
            return None
        usage, _ = usage_until(ctx, ctx.now)
        days = Decimal(usage.days_in_month)
        return MonthMeasuredDaily(
            kwh_in_per_day=usage.kwh_in / days,
            kwh_out_per_day=usage.kwh_out / days,
            steered_per_day=usage.steered_kwh / days if usage.steered_kwh is not None else None,
        )

    async def _async_fit(
        self,
        options: Mapping[str, Any],
        details: list[InvoiceDetail],
        data: DotsData,
        month_start: datetime,
        prev_ctx: MonthContext,
    ) -> FitParams:
        oldest = shift_month(month_start, -KEEP_MONTHS)
        prev_key = month_key(prev_ctx.month_start)
        measured: dict[str, MonthMeasured] = {}
        for detail in details:
            if is_credit_note(detail.summary):
                continue
            period = detail.summary.period_start
            start = datetime(period.year, period.month, 1, tzinfo=month_start.tzinfo)
            key = month_key(start)
            if start >= month_start or start < oldest or key in measured:
                continue
            if key in self._measured:
                measured[key] = self._measured[key]
                continue
            ctx = (
                prev_ctx
                if key == prev_key
                else await self._async_context(options, start, shift_month(start, 1), None)
            )
            usage, invalid = usage_until_by_series(ctx, ctx.now)
            measured[key] = MonthMeasured(
                month=key,
                usage=usage,
                invalid_hours=invalid["afname"],
                invalid_out=invalid["injectie"],
                invalid_steered=invalid["gestuurd"],
            )
            if key != prev_key:
                self._measured[key] = measured[key]
        return fit_params(details, data.breakdowns, measured)

    def _previous_series(
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
            series = cumulative_series(ctx, rules, params, last_day=days)
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
        cutoff = shift_month(month_start, -KEEP_MONTHS)
        cutoff_utc = cutoff.astimezone(UTC)
        self._prices = {h: p for h, p in self._prices.items() if h >= cutoff_utc}
        cutoff_key = month_key(cutoff)
        for bucket in ("snapshots", "capacity"):
            stored = self.stored[bucket]
            for key in [k for k in stored if k < cutoff_key]:
                stored.pop(key)


def forecast_diagnostics(coordinator: DotsForecastCoordinator) -> dict[str, Any]:
    """Forecast part of the diagnostics, without entity ids."""
    stored = coordinator.stored
    result: dict[str, Any] = {
        "last_update_success": coordinator.last_update_success,
        "sources_configured": sorted(k for k, v in coordinator.config_entry.options.items() if v),
        "stored_price_hours": len(coordinator._prices),
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
