"""Tests for the Dots REST client."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import aiohttp
import pytest

from custom_components.dots_energy.api import ApiAuthError, ApiError, DotsApiClient
from custom_components.dots_energy.auth import DotsAuth

from .auth_mocks import TOKEN, sequence, token_payload
from .conftest import load_fixture

API = "https://api.dotsenergy.be"


@pytest.fixture
async def client(aioclient_mock):
    session = aioclient_mock.create_session(asyncio.get_running_loop())
    auth = DotsAuth(
        session, email=None, password=None, refresh_token="RT", on_tokens=lambda t: None
    )
    yield DotsApiClient(session, auth)
    await session.close()


def _bearers(aioclient_mock) -> list[str]:
    return [
        (headers or {}).get("Authorization")
        for method, url, _, headers in aioclient_mock.mock_calls
        if url.host == "api.dotsenergy.be"
    ]


async def test_get_invoices_parses(aioclient_mock, client) -> None:
    aioclient_mock.post(TOKEN, json=token_payload("AT-1", "RT-1"))
    aioclient_mock.get(f"{API}/v1/my/locations/7/invoices", json=load_fixture("invoice_list.json"))

    invoices = await client.async_get_invoices(7)

    assert [i.number for i in invoices] == ["TST00000101", "TST00000102", "TST00000103"]
    assert invoices[1].total == Decimal("88.38")
    assert _bearers(aioclient_mock) == ["Bearer AT-1"]


async def test_get_invoice_detail_and_contract(aioclient_mock, client) -> None:
    aioclient_mock.post(TOKEN, json=token_payload("AT-1", "RT-1"))
    aioclient_mock.get(f"{API}/v1/my/invoices/102", json=load_fixture("invoice_detail.json"))
    aioclient_mock.get(
        f"{API}/v1/my/locations/7/contracts/active?energyType=electricity",
        json=load_fixture("contract.json"),
    )

    detail = await client.async_get_invoice(102)
    contract = await client.async_get_active_contract(7)

    assert detail.summary.id == 102
    assert len(detail.lines) == 15
    assert contract == {
        "pricingType": "Dynamic",
        "productName": "TEST_PRODUCT",
        "startDate": "2026-01-01",
        "endDate": "2027-01-01",
        "settlementMethod": "monthly",
    }


async def test_get_locations_keeps_id_and_name_only(aioclient_mock, client) -> None:
    aioclient_mock.post(TOKEN, json=token_payload("AT-1", "RT-1"))
    aioclient_mock.get(
        f"{API}/v1/my/locations",
        json=[
            {
                "id": 7,
                "name": "Thuis",
                "street": "Teststraat",
                "houseNumber": "1",
                "ean": "541000000000000000",
            }
        ],
    )

    assert await client.async_get_locations() == [{"id": 7, "name": "Thuis"}]


async def test_401_triggers_one_refresh_and_retry(aioclient_mock, client) -> None:
    aioclient_mock.post(
        TOKEN,
        side_effect=sequence(
            {"json": token_payload("AT-1", "RT-1")},
            {"json": token_payload("AT-2", "RT-2")},
        ),
    )
    aioclient_mock.get(
        f"{API}/v1/my/locations/7/invoices",
        side_effect=sequence(
            {"status": 401, "json": {}}, {"json": load_fixture("invoice_list.json")}
        ),
    )

    invoices = await client.async_get_invoices(7)

    assert len(invoices) == 3
    assert _bearers(aioclient_mock) == ["Bearer AT-1", "Bearer AT-2"]
    token_calls = [c for c in aioclient_mock.mock_calls if str(c[1]) == TOKEN]
    assert len(token_calls) == 2


async def test_second_401_raises_api_auth_error(aioclient_mock, client) -> None:
    aioclient_mock.post(TOKEN, json=token_payload("AT-1", "RT-1"))
    aioclient_mock.get(f"{API}/v1/my/locations/7/invoices", status=401, json={})
    with pytest.raises(ApiAuthError):
        await client.async_get_invoices(7)


async def test_500_raises_api_error(aioclient_mock, client) -> None:
    aioclient_mock.post(TOKEN, json=token_payload("AT-1", "RT-1"))
    aioclient_mock.get(f"{API}/v1/my/invoices/102", status=500, text="boom")
    with pytest.raises(ApiError) as err:
        await client.async_get_invoice(102)
    assert not isinstance(err.value, ApiAuthError)


async def test_timeout_raises_api_error(aioclient_mock, client) -> None:
    aioclient_mock.post(TOKEN, json=token_payload("AT-1", "RT-1"))
    aioclient_mock.get(f"{API}/v1/my/invoices/102", exc=TimeoutError())
    with pytest.raises(ApiError):
        await client.async_get_invoice(102)


async def test_connection_error_raises_api_error(aioclient_mock, client) -> None:
    aioclient_mock.post(TOKEN, json=token_payload("AT-1", "RT-1"))
    aioclient_mock.get(f"{API}/v1/my/invoices/102", exc=aiohttp.ClientConnectionError())
    with pytest.raises(ApiError):
        await client.async_get_invoice(102)
