"""Read-only client for the Dots Energy customer API."""

from __future__ import annotations

from typing import Any

import aiohttp

from .auth import DotsAuth
from .const import API_BASE, REQUEST_TIMEOUT
from .models import (
    PARSE_ERRORS,
    InvoiceDetail,
    InvoiceSummary,
    parse_invoice_detail,
    parse_invoice_list,
)

_CONTRACT_FIELDS = ("pricingType", "productName", "startDate", "endDate", "settlementMethod")


class ApiError(Exception):
    """The Dots API could not be reached or returned an error."""


class ApiAuthError(ApiError):
    """The Dots API kept rejecting the access token."""


class DotsApiClient:
    """Thin wrapper around the endpoints the integration needs."""

    def __init__(self, session: aiohttp.ClientSession, auth: DotsAuth) -> None:
        self._session = session
        self._auth = auth

    async def _get(self, path: str) -> Any:
        token = await self._auth.async_get_access_token()
        for attempt in range(2):
            try:
                async with self._session.get(
                    f"{API_BASE}{path}",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 401:
                        if attempt == 0:
                            token = await self._auth.async_get_access_token(force_refresh=True)
                            continue
                        raise ApiAuthError("Dots API rejected the access token")
                    if resp.status != 200:
                        raise ApiError(f"Dots API returned HTTP {resp.status} for {path}")
                    try:
                        return await resp.json(content_type=None)
                    except ValueError as err:
                        raise ApiError(f"Dots API returned no JSON for {path}") from err
            except (aiohttp.ClientError, TimeoutError) as err:
                raise ApiError(f"Dots API unreachable: {type(err).__name__}") from err
        raise ApiAuthError("Dots API rejected the access token")

    async def async_get_locations(self) -> list[dict[str, Any]]:
        """Return delivery locations as {id, name}; address fields are dropped."""
        data = await self._get("/v1/my/locations")
        return [
            {"id": int(item["id"]), "name": str(item.get("name") or f"Locatie {item['id']}")}
            for item in data
        ]

    async def async_get_invoices(self, location_id: int) -> list[InvoiceSummary]:
        """Return all invoices of a location."""
        data = await self._get(f"/v1/my/locations/{location_id}/invoices")
        if not isinstance(data, list):
            raise ApiError("Dots invoice list has an unexpected format")
        return parse_invoice_list(data)

    async def async_get_invoice(self, invoice_id: int) -> InvoiceDetail:
        """Return one invoice with its lines."""
        data = await self._get(f"/v1/my/invoices/{invoice_id}")
        try:
            return parse_invoice_detail(data)
        except PARSE_ERRORS as err:
            raise ApiError(
                f"Dots invoice detail has an unexpected format ({type(err).__name__})"
            ) from err

    async def async_get_active_contract(self, location_id: int) -> dict[str, Any]:
        """Return a non-sensitive subset of the active electricity contract."""
        data = await self._get(
            f"/v1/my/locations/{location_id}/contracts/active?energyType=electricity"
        )
        if not isinstance(data, dict):
            raise ApiError("Dots contract has an unexpected format")
        return {field: data.get(field) for field in _CONTRACT_FIELDS}
