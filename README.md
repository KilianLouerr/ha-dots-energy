# Dots Energy voor Home Assistant

Onofficiële integratie die je facturen van [Dots Energy](https://dotsenergy.be) ophaalt en
in Home Assistant beschikbaar maakt: het bedrag per factuur, de opsplitsing in energie,
netkosten, heffingen, vaste kosten, btw, injectie en vergoedingen, en maandcijfers in de
statistieken van Home Assistant.

De integratie leest alleen. Ze betaalt, wijzigt of vraagt niets aan bij Dots.

> Dit project is niet verbonden aan Dots Energy. De Dots-API is niet publiek gedocumenteerd
> en kan zonder verwittiging veranderen.

## Installatie via HACS

1. HACS → menu rechtsboven → **Aangepaste repositories**.
2. Voeg `https://github.com/KilianLouerr/ha-dots-energy` toe als type **Integratie**.
3. Installeer **Dots Energy** en herstart Home Assistant.
4. **Instellingen → Apparaten en diensten → Integratie toevoegen → Dots Energy**.

## Aanmelden

Vul het e-mailadres en wachtwoord in waarmee je inlogt op het Dots-klantenportaal. De
integratie meldt zich aan zoals het portaal dat doet en blijft daarna zelf aangemeld.

Vraagt Dots bij het aanmelden om een captcha, dan kan de integratie niet automatisch
inloggen. Kies dan **Refresh token gebruiken**:

1. Open een **privévenster** en log in op `https://my.dotsenergy.be`.
2. Open de ontwikkelaarstools (F12) → **Application** → **Session Storage** →
   `https://my.dotsenergy.be` → de sleutel die begint met `oidc.user:`.
3. Kopieer de waarde van `refresh_token` en plak die in Home Assistant.
4. Sluit het privévenster en gebruik die sessie niet meer. Log in je gewone browser
   gewoon verder in zoals altijd.

Gebruik nooit het token van je gewone browsersessie: Dots ververst tokens bij elk gebruik,
en twee gebruikers van hetzelfde token kunnen elkaar afmelden.

## Entiteiten

| Entiteit | Inhoud |
|---|---|
| `sensor.dots_energy_laatste_factuur` | bedrag van de laatste factuur, met status en categorieën |
| `sensor.dots_energy_totaal_dit_jaar` | som van de facturen van dit kalenderjaar |
| `sensor.dots_energy_gemiddeld_per_maand` | gemiddelde van de laatste 12 maanden |
| `sensor.dots_energy_all_in_prijs` | factuurtotaal gedeeld door de afgenomen kWh |
| `sensor.dots_energy_openstaand` | som van facturen die nog niet betaald zijn |
| `sensor.dots_energy_gekozen_factuur` | de factuur gekozen in de keuzelijst, met alle regels |
| `sensor.dots_energy_maandoverzicht` | bedragen per maand en categorie (voor grafieken) |
| `sensor.dots_energy_laatst_bijgewerkt` | tijdstip van de laatste geslaagde synchronisatie |
| `select.dots_energy_factuur` | keuzelijst van alle facturen |
| `event.dots_energy_nieuwe_factuur` | gebeurtenis bij een nieuwe factuur |

De maandbedragen worden ook als externe statistieken `dots_energy:<categorie>` bewaard.

Voorbeelden van een dashboard en een melding staan in [`examples/`](examples/).
