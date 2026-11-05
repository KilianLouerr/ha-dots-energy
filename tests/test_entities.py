"""End-to-end tests: integration set up against mocked Dots endpoints."""

from __future__ import annotations

import json

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, STATE_UNAVAILABLE
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dots_energy.const import CONF_LOCATION_ID, CONF_REFRESH_TOKEN, DOMAIN
from custom_components.dots_energy.coordinator import new_invoice_signal
from custom_components.dots_energy.diagnostics import async_get_config_entry_diagnostics

from .auth_mocks import TOKEN, token_payload
from .conftest import load_fixture
from .fakes import detail_for

API = "https://api.dotsenergy.be"
EMAIL = "someone@example.invalid"
PASSWORD = "s3cret-Passw0rd!"


def _mock_api(aioclient_mock, refresh: str = "RT-1") -> None:
    invoices = load_fixture("invoice_list.json")
    aioclient_mock.post(TOKEN, json=token_payload("AT-1", refresh))
    aioclient_mock.get(f"{API}/v1/my/locations/7/invoices", json=invoices)
    aioclient_mock.get(
        f"{API}/v1/my/locations/7/contracts/active?energyType=electricity",
        json=load_fixture("contract.json"),
    )
    aioclient_mock.get(f"{API}/v1/my/invoices/102", json=load_fixture("invoice_detail.json"))
    for invoice in invoices:
        if invoice["id"] != 102:
            aioclient_mock.get(f"{API}/v1/my/invoices/{invoice['id']}", json=detail_for(invoice))


@pytest.fixture
async def entry(hass, aioclient_mock, freezer):
    freezer.move_to("2026-03-30 12:00:00+02:00")
    _mock_api(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|user-1",
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_REFRESH_TOKEN: "RT-0",
            CONF_LOCATION_ID: 7,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_and_unload(hass, entry) -> None:
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_rotated_refresh_token_is_persisted(hass, entry) -> None:
    assert entry.data[CONF_REFRESH_TOKEN] == "RT-1"


async def test_sensors_values_from_fixture(hass, entry) -> None:
    def state(entity_id):
        return hass.states.get(entity_id)

    last = state("sensor.dots_energy_laatste_factuur")
    assert float(last.state) == 88.38
    assert last.attributes["unit_of_measurement"] == "EUR"
    assert last.attributes["factuurnummer"] == "TST00000102"
    assert last.attributes["status"] == "pending"
    assert last.attributes["periode_start"] == "2026-02-01"
    assert last.attributes["categorieen"]["netkosten"] == 39.5
    assert float(state("sensor.dots_energy_totaal_dit_jaar").state) == 155.88
    assert float(state("sensor.dots_energy_gemiddeld_per_maand").state) == 77.94
    assert float(state("sensor.dots_energy_all_in_prijs").state) == 0.354
    assert state("sensor.dots_energy_all_in_prijs").attributes["unit_of_measurement"] == "EUR/kWh"
    assert float(state("sensor.dots_energy_openstaand").state) == 88.38

    chosen = state("sensor.dots_energy_gekozen_factuur")
    assert float(chosen.state) == 88.38
    assert len(chosen.attributes["regels"]) == 15
    assert chosen.attributes["regels"][0] == {
        "omschrijving": "Elektriciteit totale uren",
        "categorie": "energie",
        "hoeveelheid": 250.0,
        "eenheidsprijs": 0.12,
        "bedrag": 30.0,
    }
    assert chosen.attributes["regels"][13]["categorie"] == "afronding"
    assert chosen.attributes["afwijking"] == 0.0

    overview = state("sensor.dots_energy_maandoverzicht")
    assert overview.state == "2"
    assert [m["maand"] for m in overview.attributes["maanden"]] == ["2026-01", "2026-02"]
    assert [f["id"] for f in overview.attributes["facturen"]] == [103, 102, 101]
    assert overview.attributes["facturen"][1] == {
        "id": 102,
        "label": "2026-02 · TST00000102",
        "periode": "2026-02",
        "totaal": 88.38,
        "status": "pending",
    }
    assert state("sensor.dots_energy_laatst_bijgewerkt").state.startswith("2026-03-30")


async def test_select_changes_selected_invoice_lines(hass, entry, aioclient_mock) -> None:
    select = hass.states.get("select.dots_energy_factuur")
    assert select.attributes["options"] == [
        "2026-02 · TST00000103",
        "2026-02 · TST00000102",
        "2026-01 · TST00000101",
    ]
    assert select.state == "2026-02 · TST00000102"
    calls = aioclient_mock.call_count

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.dots_energy_factuur", "option": "2026-01 · TST00000101"},
        blocking=True,
    )

    chosen = hass.states.get("sensor.dots_energy_gekozen_factuur")
    assert float(chosen.state) == 80.0
    assert chosen.attributes["factuurnummer"] == "TST00000101"
    assert len(chosen.attributes["regels"]) == 1
    assert hass.states.get("select.dots_energy_factuur").state == "2026-01 · TST00000101"
    assert aioclient_mock.call_count == calls


async def test_event_entity_receives_new_invoice(hass, entry) -> None:
    payload = {
        "invoice_number": "TST00000104",
        "period": "2026-03",
        "amount": 90.0,
        "previous_amount": 88.38,
        "difference": 1.62,
    }
    async_dispatcher_send(hass, new_invoice_signal(entry.entry_id), payload)
    await hass.async_block_till_done()

    event = hass.states.get("event.dots_energy_nieuwe_factuur")
    assert event.state.startswith("2026-03-30")
    assert event.attributes["event_type"] == "nieuwe_factuur"
    assert event.attributes["amount"] == 90.0
    assert event.attributes["difference"] == 1.62


async def test_entities_stay_available_on_update_failure(hass, entry, aioclient_mock) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.post(TOKEN, json=token_payload("AT-2", "RT-2"))
    aioclient_mock.get(f"{API}/v1/my/locations/7/invoices", status=500, text="boom")

    coordinator = entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    last = hass.states.get("sensor.dots_energy_laatste_factuur")
    assert last.state != STATE_UNAVAILABLE
    assert float(last.state) == 88.38
    assert hass.states.get("sensor.dots_energy_laatst_bijgewerkt").state.startswith("2026-03-30")


async def test_diagnostics_redacts_secrets(hass, entry) -> None:
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(diagnostics, default=str)

    for secret in (EMAIL, PASSWORD, "RT-0", "RT-1", "AT-1", "TST0000"):
        assert secret not in dumped
    assert diagnostics["data"]["invoices"][0]["index"] == 0
    assert diagnostics["data"]["kpis"]["outstanding"] == 88.38
