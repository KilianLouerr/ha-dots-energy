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
| `sensor.dots_energy_factuur_tot_nu_toe` | wat de factuur van deze maand nu al zou zijn, per categorie en per dag (zie Voorspelling) |
| `sensor.dots_energy_verwachte_factuur` | verwachte factuur voor de hele maand, per categorie en per regel (zie Voorspelling) |
| `select.dots_energy_factuur` | keuzelijst van alle facturen |
| `event.dots_energy_nieuwe_factuur` | gebeurtenis bij een nieuwe factuur |

De maandbedragen worden ook als externe statistieken `dots_energy:<categorie>` bewaard.

## Voorspelling

De integratie schat elk uur wat de factuur van de lopende maand wordt: wat ze **tot nu toe**
zou zijn, en wat ze op het einde van de maand **verwacht** wordt. Het is een schatting, geen
factuur.

Hoe ze rekent:

- De tarieven komen uit je laatste facturen: prijzen per kWh (accijns, certificaten,
  evenwichtsbijdragen), vaste kosten per dag, btw.
- Energie en injectie: je verbruik per uur maal de marktprijs van dat uur, plus een opslag die
  geleerd wordt uit je vorige facturen. Zolang die opslag nog niet geleerd is, rekent ze met de
  eenheidsprijs van je laatste factuur.
- Netkosten: het gemiddelde van je maandpieken over de laatste 12 maanden (capaciteitstarief)
  en je kWh, geleerd uit je facturen zodra er drie maanden met die gegevens zijn.
- Flexibiliteitsvergoeding: per kWh die je batterijsturing verhandelde, geleerd uit je
  facturen; zonder die gegevens het gemiddelde per dag.
- De rest van de maand wordt ingevuld met je gemiddelde verbruik per dag van deze maand
  (de eerste week gemengd met de vorige maand).

Instellen: **Instellingen → Apparaten en diensten → Dots Energy → Configureren**. Kies sensoren
met uurstatistieken (`state_class` `total` of `total_increasing`):

| Optie | Nodig | Voorbeeld |
|---|---|---|
| Afname van het net (kWh) | ja | `sensor.my_grid_import` |
| Injectie in het net (kWh) | nee | `sensor.my_grid_export` |
| Gemiddelde maandpiek laatste 12 maanden (kW) | nee | `sensor.my_capacity_average` |
| Door de batterijsturing verhandelde kWh | nee | `sensor.my_steered_charge`, `sensor.my_steered_discharge` |
| Marktprijs per kWh (day-ahead) | nee | een sensor van een Nord Pool- of ENTSO-E-integratie |
| Prijs is inclusief btw | nee | aan als de prijssensor btw bevat |

De marktprijs wordt elk uur vastgelegd (een prijssensor bewaart zelf maar een paar dagen
historiek). De opslag op de marktprijs kan dus pas geleerd worden na een volledige
factuurmaand met vastgelegde prijzen.

Ontbreekt er iets (prijs, meting, gegevens van de batterijsturing), dan blijft de voorspelling
een getal tonen en staat het attribuut `kwaliteit` op `ruw`, met in `ontbreekt` wat er
ontbrak.

Gebouwd en getest voor een **dynamisch** contract in **Vlaanderen** (capaciteitstarief) met een
enkelvoudige meter. In andere situaties vallen de regels terug op de prijzen van je laatste
factuur.

Voorbeelden van een dashboard en een melding staan in [`examples/`](examples/).
