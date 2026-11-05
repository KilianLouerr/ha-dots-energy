"""Constants for the Dots Energy integration."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum

DOMAIN = "dots_energy"

AUTH_DOMAIN = "auth.dotsenergy.be"
AUTH_BASE = f"https://{AUTH_DOMAIN}"
CLIENT_ID = "VFklW8ALID2BlM7pMp2MCELFI635nhG2"
AUDIENCE = "https://my.api.dotsenergy.be"
SCOPE = "openid profile email offline_access"
REDIRECT_URI = "https://my.dotsenergy.be"
API_BASE = "https://api.dotsenergy.be"

UPDATE_INTERVAL = timedelta(hours=6)
REQUEST_TIMEOUT = 30

CONF_REFRESH_TOKEN = "refresh_token"
CONF_LOCATION_ID = "location_id"

EVENT_NEW_INVOICE = "dots_energy_new_invoice"

OPT_AFNAME = "afname_entity"
OPT_INJECTIE = "injectie_entity"
OPT_CAPACITEIT = "capaciteit_entity"
OPT_GESTUURD = "gestuurd_entities"
OPT_PRIJS = "prijs_entity"
OPT_PRIJS_INCL_BTW = "prijs_incl_btw"


class Category(StrEnum):
    """Invoice cost categories, in display order."""

    ENERGIE = "energie"
    NETKOSTEN = "netkosten"
    HEFFINGEN = "heffingen"
    VASTE_KOSTEN = "vaste_kosten"
    BTW = "btw"
    INJECTIE = "injectie"
    VERGOEDINGEN = "vergoedingen"
    OVERIG = "overig"
