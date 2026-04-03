# Claude Context — Home Assistant Automation System
# Tento soubor aktualizuj po kazde vetsi zmene.
# Na zacatku session: "Precti CLAUDE_CODE_RULES.md a CLAUDE_CONTEXT.md"

## Posledni aktualizace
Datum: 2026-04-03
Session: Voice assistant, solar_confidence fix, Ford/Elroq detekce, VT surplus

## Pristi kroky (TODO)
- [ ] Loznice Daikin - ceka na teplotni cidlo
- [x] Ford PHEV integrace do ev_charging_manager (3.4.2026)
- [x] solar_confidence: OWM cloud_coverage + bias correction + kalibrace (3.4.2026)
- [ ] Koupelna/kuchyne - pridat teplotni cidla
- [ ] Recorder retence snizit na 5 dni
- [ ] Historii vyloucit ze zalohy (po snizeni retence)
- [ ] Redesign AI Agent dashboard (vysoka priorita - pro prezentaci)
- [ ] Cidlo 1 obyvak (teplota_obyvak_temperature) - offset +3-4C, diagnostikovat/zkalibrovat
- [x] Prumerovy senzor obyvaku upraven na cidla 2+3 (29.3.2026)
- [x] ApexCharts graf 4 teplot obyvaku pridan do Ovladani topeni (29.3.2026)
- [x] Tablet dashboard +/- tlacitka opravena (29.3.2026)
- [x] history_stats senzor TC cas v provozu opraven (29.3.2026)
- [x] Heating teploty: noc 21C, den 22C, predehrev 04:30 (29.3.2026)
- [x] input_number.topeni_night_temp pridan (29.3.2026)
- [x] weekly_heating_report.py nasazen s Haiku AI analyzou (29.3.2026)
- [x] Analyza tepelne ztraty domu: 96 W/C, kapacita 4.2 kWh/C (29.3.2026)
- [x] HDO binary sensor 26.3.2026
- [x] ev_monthly_report.py nasazen 26.3.2026
- [x] ev_charging_manager.py nasazen 27.3.2026
- [x] Scheduler deaktivace - ohrev vody prenes do heating_manager 27.3.2026
- [x] Udrzba - zalohy, disk, databaze 24.3.2026
- [x] OWM integrace - obnovena 24.3.2026
- [x] card-mod nainstalovano 26.3.2026
- [x] AI Agent dvouvrstva architektura nasazeno 25.3.2026
- [x] energy_planner interaktivni rezim opraven
- [x] Notifikace - push (mobile_app_iphone_17) 26.3.2026
- [x] Vzdaleny pristup - Cloudflare Tunnel 26.3.2026
- [x] PND - login opraven 24.3.2026

## Architektura systemu

### Hardware
- FVE: 7.4 kWp, jih 45, Huawei SUN2000 invertor
- Baterie: ~10 kWh Huawei LUNA
- TC: tepelne cerpadlo — podlaha + radiatory, ovladano pres Tasmota
- Wallbox: Tuya, 3-fazovy, 4-16A
- Elroq: 77 kWh, ~85 km/den prumer, SOC/km ~0.20 (dynamicky)
- Ford PHEV: 11.8 kWh, integrace FordPass, session tracking v ev_charging_manager
- Bojler: 200l, TC ohrev + spirala 1.5 kW na fazi B
- Daikin: 4x split (Adela, Nela, Pracovna, Loznice — loznice bez cidla)
- Shelly EM3: meri TC spotrebu po fazich
- Mereni: SmartThings (pracka/susicka), Zigbee zasuvky

### Klicove konvence — KRITICKE
# power_meter_active_power (Huawei SUN2000):
#   KLADNE = export do site (prebytek FVE)
#   ZAPORNE = import ze site

# Vypocet spotreby domu:
#   home_consumption_w = pv_power_w - grid_power_w - battery_power_w

# Faze B: spirala bojleru (1.5 kW)
# Faze C: pretizena — pracka, susicka, Daikin outdoor, cast TC

### TC ventil — KRITICKE PRAVIDLO
# switch.tepelnecerpadlo_3w_teplavoda a switch.tepelnecerpadlo_3w_topeni
# NIKDY nesmi byt ON soucasne!
# Bojler ohrev ma PREDNOST pred topenim

# Manualni ohrev vody TC: VZDY pres input_boolean.ohrev_vody_switch (turn_on)
# NIKDY primo prepinat switch.tepelnecerpadlo_3w_teplavoda!
# heating_manager provede kompletni sekvenci (TC target, ventil, flagy, stop pri 43C)

## AppDaemon Apps — aktualni stav

### ev_charging_manager.py (aktualizovano 3.4.2026)
- Deterministicke rizeni nabijeni EV s Ford/Elroq detekcii
- Detekce vozidla: Ford plug CONNECTED -> Ford, jinak -> Elroq (Skoda API nespolehlivy)
- Ford: pasivni tracking (bez battery lock, bez DLM)
- Elroq: plne rizeni (battery lock, DLM, proud)
- VT surplus override: kazdych 5 min kontrola prebytku >= 1.5 kW, pokud ano nabiji 6A
- Ford plug listener jako primarni trigger (30s detekce)
- Startup recovery: 30s + 60s retry, detekce pres Ford plug
- Triggery: pripojeni auta, VT/NT prechod (HDO), den/noc, dokonceni, odpojeni
- Den (05-21): 6A 3f = 4.1 kW (solar/sit)
- Noc (21-05): 16A 3f = 11 kW (sit NT)
- VT okna: pauza nabijeni, baterie FVE odemcena (maximise_self_consumption)
- NT okna: nabijeni obnoveno, baterie FVE zamcena (fixed_charge_discharge, discharge=0)
- Fallback 07:30: odemkne baterii pokud auto neni pripojeno
- InfluxDB: ev_charging_sessions (auto, kwh, soc_start, soc_end, duration)
- Push notifikace pri kazde zmene stavu
- SMAZANO: energy_ai.py (Claude Haiku 15min cyklus)

### ev_charger.py
- Tuya cloud API komunikace s wallboxem
- InfluxDB: ev_charger_data (kazdych 30s)
- Entity: ev_charger_phase_power, ev_charger_vykon

### boiler_surplus.py
- Spirala bojleru z prebytku FVE na fazi B
- Podminky: phase_b > 1800W AND teplota < 58C AND 7:00-17:00

### solar_confidence.py (aktualizovano 3.4.2026)
- Met.no + OWM -> confidence 0-100% s bias correction
- OWM cloud_coverage % primo misto condition mapping
- Confidence correction factor: rolling 7-day EMA z prediction accuracy
- CONDITION_CONFIDENCE kalibrovano z realnych dat (sunny 75, cloudy 10, rainy 5)
- Feedback loop: predikce, verifikace, kalibrace vah (denne 23:30)
- InfluxDB: solar_prediction, solar_prediction_accuracy
- OWM vyreseno 25.3.2026:
  - weather.openweathermap -> forecast mode (40h hourly, pro solar_confidence)
  - weather.openweathermap_2 -> current mode (aktualni stav, 17 senzoru)
  - API klic: ec45d055048226f001e04a92d185242c (Free tier)
  - v3.0 mode NEFUNGUJE (vyzaduje placeny OneCall 3.0)

### energy_planner.py
- Denni planovani nabijeni Elroq (23:00 km, 23:05 plan)
- 5denni vyhled, interaktivni rezim (Haiku)
- SOC/km dynamicky z aktualniho SOC/range (~0.20%/km)
- InfluxDB: ev_daily_km (backfill od 5.3.2026)

### consumption_monitor.py
- Rozklad spotreby per zarizeni kazdych 5 min
- Fazova nerovnovaha alert >2000W
- InfluxDB: consumption_breakdown, consumption_daily

### heating_manager.py (aktualizovano 29.3.2026)
- TC + Daikin rizeni podle pritomnosti
- Priority: bojler > leto > noc > doma > pryc
- Daikin hystereze: heat ON <19C, OFF >20.5C; cool ON >25C, OFF <23.5C
- Ranni predehrev: Po-Pa, start 04:30, 60 min pred odchodem
- Teploty: den 22C (input_number.topeni_target_temp), noc 21C (input_number.topeni_night_temp)
- InfluxDB: heating_log
- Status rozlisuje: Topi / Doma (idle) / Away / Noc / Leto / Bojler prednost
- Teplotu meni JEN pri prechodu stavu (zachovava uzivatelske nastaveni pres Siri/dashboard)
- Ohrev vody TC: trigger 13:00 nebo SOC>90%, podminka bojler<40C, stop pri 43C
  - Smart stop: pokud obyvak < target, TC prejde na topeni bez vypnuti kompresoru
  - Preneseno z heating.yaml automaci 27.3.2026
- Scheduler automace ohrevu vody deaktivovany (presunuto sem)

### presence_patterns.py
- Vzorce pritomnosti z device_tracker + InfluxDB (presence_transitions)
- Denne 23:00 prepocet, realtime listen_state na 4 trackery
- Data_days: 10/28 (ucim se)
- Clenove: Kamil, Romana, Nela, Adela


### ai_agent.py (NOVY 2026-03-25)
- Dvouvrstva AI architektura: Sonnet (planovani) -> Haiku (evaluace)
- Uzivatel zada instrukci v prirozene cestine pres input_text
- Sonnet sestavi JSON akcni plan, ceka na potvrzeni
- Po potvrzeni Haiku provede akce a zhodnoti vysledky
- Podporuje: baterie, EV, bojler, vytapeni, cteni stavu
- Dashboard: view 8 "AI Agent" v lovelace (panel: true, full width)
  - stack-in-card horizontal: input_text + button-card (Potvrdit/Zamitnout)
  - Custom karty: button-card, stack-in-card, layout-card (nainstalovano)
- InfluxDB: ai_agent_stats, ai_agent_history
- Sensory: sensor.ai_agent_log, sensor.ai_agent_pending, sensor.ai_agent_stats
- Tlacitka: listen_state na input_button (listen_event nefungovalo)
- Overeno: read-only (auto-execute), write (confirm->execute), reject

### weekly_heating_report.py (NOVY 29.3.2026)
- Tydenni AI analyza vytapeni generovana Haiku
- Spousteni: kazde pondeli 07:00 + manualne input_button.heating_report_generate
- Data z InfluxDB: TC spotreba, teploty, 3W ventil (separace topeni/bojler), detekce krbu
- Historicke srovnani s predchozimi 4 tydny
- Referencni hodnoty: tepelna ztrata 96 W/C, kapacita 4.2 kWh/C
- Persistentni JSON reporty v /homeassistant/heating_reports/
- input_select.heating_report_week pro vyber historickeho tydne
- InfluxDB: heating_weekly_report (total_kwh, total_cost, avg_outdoor, avg_indoor)
- Push notifikace na iPhone pri generovani
- Dashboard: view 10 Heating Report (panel mode, scrollovatelny markdown)

### ev_monthly_report.py (NOVY 2026-03-26)
- Sleduje nabijeci sessions Elroq (charger_connected) a Ford PHEV (elvehplug)
- Loguje do InfluxDB: ev_charging_sessions (kwh, soc_start, soc_end, duration, auto tag)
- Mesicni report 1. den mesice v 08:00 (push notifikace + email pokud SMTP nakonfigurovan)
- Manualni spusteni: input_button.ev_report_generate
- SMTP zatim nefunguje (smtp.t-mobile.cz neni resolvatelny z HAOS)
- Ford entity prefix: fordpass_wf0fxxwpmhsc70607_
### Faktury (REST integrace 27.3.2026)
- API: http://10.0.0.55/api/invoices/ha/unpaid + due-soon
- Sensory: neuhrazene_faktury, faktury_k_uhrade_celkem, faktury_po_splatnosti, faktury_pred_splatnosti
- Automatizace: denne 08:00 push notifikace pri splatnosti do 2 dnu nebo po splatnosti
- Dashboard: view Faktury (button-card summary + markdown tabulka)

## InfluxDB measurements

| Measurement | App | Interval |
|:------------|:----|:---------|
| solar_prediction | solar_confidence | 30 min |
| solar_prediction_accuracy | solar_confidence | kazdou hod |
| ev_daily_km | energy_planner | denne 23:00 |
| ev_charger_data | ev_charger | 30s |
| consumption_breakdown | consumption_monitor | 5 min |
| consumption_daily | consumption_monitor | denne 23:55 |
| heating_log | heating_manager | pri zmene |
| state_log | heating_manager | pri zmene climate/switch |
| presence_log | presence_patterns | pri prichodu/odchodu |
| presence_transitions | presence_patterns | realtime |
| ai_agent_stats | ai_agent | pri volani |
| ai_agent_history | ai_agent | pri akci |
| heating_weekly_report | weekly_heating_report | tydne (pondeli) |
| appliance_cycles | appliance_tracker | pri dokonceni cyklu |

## Dashboardy (lovelace.lovelace — storage mode)

| View | Nazev | Karet |
|:-----|:------|:------|
| 0 | Ovladani topeni | 19 |
| 1 | PND | 6 |
| 2 | Home | 29 (pritomnost, vzorce, vytapeni, ...) |
| 3 | Klimatizace | 3 |
| 4 | Teploty | 0 |
| 5 | Spotreba elektriny | 1 |
| 6 | Energy Planer | 2 (solar + planner s 5d vyhledem) |
| 7 | Spotreba | 1 (consumption monitor) |
| 8 | AI Agent | 4 (vstup+tlacitka, prompt, live vystup, stav+API) |
| 9 | Faktury | 3 (summary, detail tabulka, link) |
| 10 | Heating Report | 3 (markdown report, statistiky, week selector) |


### Voice Assistant (NOVY 3.4.2026)
- Pipeline: Cesky asistent (Whisper STT -> Claude Haiku -> Google Cloud TTS)
- STT: Whisper medium-int8, jazyk cs
- LLM: Claude Haiku (nativni Anthropic integrace) s Assist + Search Services + Weather Forecast tools
- TTS: Google Cloud cs-CZ-Chirp3-HD-Aoede na soundbar (media_player.q_series_soundbar_2)
- Web search: Anthropic native web_search tool
- Wake word: openWakeWord "alexa" (ceka na Voice PE hardware)
- Radio: 6 stanic pres dedikacne scripty (script.radio_radiozurnal, _evropa2, _frekvence1, _helax, _fresh, _orion)
- TTS relay: Claude vola script.say_on_soundbar -> tts.google_cloud -> soundbar
- Prompt: custom cesky prompt s entity mappingem, ulozeny v core.config_entries (edit jen pri zastavenem HA)
- radio_stations.json: centralni seznam stanic (name, aliases, url)
- 408 entit exposed pro conversation
- Konfigurace: Nastaveni -> Zarizeni a sluzby -> Claude -> Claude conversation -> Nastavit
- POZOR: prompt v core.config_entries se MUSI editovat pri zastavenem HA (ha core stop -> edit -> ha core start)

### Voice Assistant - zname problemy
- Whisper obcas komoli ceska jmena
- iPhone app: TTS odpoved nehraje primo, nutno pres script say_on_soundbar
- IDOS jizdni rady: scraping nefunguje (SPA), web search da jen obecne info
- Google Nest: nepodporuje cestinu pro hlasove ovladani

## Zname problemy / Gotchas

1. HA zahazuje atributy s hodnotou 0.0 nebo False
   -> Reseni: pouzij string "0" nebo "yes"/"no"

2. Lovelace je v storage mode — REST API /api/lovelace/config vraci 404
   -> Edituj pres jq v /config/.storage/lovelace.lovelace
   -> VZDY zalohuj pred zapisem! (viz CLAUDE_CODE_RULES.md)

3. SFTP open('w') OKAMZITE truncatuje soubor
   -> Pri encoding error se data ztrati NENÁVRATNĚ
   -> Pouzivej jq na remote nebo cp + edit

4. AppDaemon !secret nerespektuje zmeny bez restartu addonu
   -> Token je inline v apps.yaml

5. InfluxDB je v1 (InfluxQL, ne Flux) — HTTP API s username/password

6. Casova zona: InfluxDB uklada UTC, HA zobrazuje CET (UTC+1)
   -> Pri cteni z InfluxDB prevadej na lokalni cas

7. SmartThings pracka/susicka: sensor power = vzdy 0W
   -> Pouzij energy (kWh) delta pro detekci aktivity

8. Daikin interni teplotni senzory jsou nespolehlive
   -> Vzdy pouzivej Zigbee cidla pro regulaci

9. iOS randomizuje WiFi MAC adresy
   -> Trackery pojmenovany podle aktualniho MAC (24.3.2026)



10. Lovelace storage mode: REST API /api/lovelace/config vraci 404
   -> Edituj primo /config/.storage/lovelace.lovelace (Python/jq)
   -> NUTNY ha core restart po kazde zmene (HA cte soubor jen pri startu)
   -> Vzdy zalohovat pred editaci (CLAUDE_CODE_RULES.md pravidlo 1)

11. AppDaemon set_state() entity != realny HA helper
   -> input_text.set_value, input_button.press na ne NEFUNGUJI
   -> Pro HA service cally nutne definovat v configuration.yaml
   -> listen_state na realne helpers FUNGUJE, listen_event na call_service NE
12. HDO casy jsou orientacni - distributor se jimi nemusi ridit presne
   -> Do budoucna nahradit fyzickym spinacem reagujicim na skutecny HDO signal
   -> Sensor: binary_sensor.hdo_nizky_tarif_nt (on=NT, off=VT)

13. Wallbox je pouze 3f, min 6A x 3f = 4.14 kW
   -> 1f nabijeni neni mozne, sigle_phase_power je jen read-only senzor
   -> work_mode: charge_now/charge_pct/charge_energy/charge_schedule (zadne 1f/3f)

14. Pri nabijeni EV je baterie FVE zamcena
   -> ev_charging_manager nastavi fixed_charge_discharge + max_discharging=0
   -> Wallbox bere ze site (NT) nebo ze solaru (den)
   -> Fallback 07:30 odemkne baterii pokud auto neni pripojeno

15. energy_ai.py SMAZANO 27.3.2026
   -> Nahrazeno ev_charging_manager.py (deterministicka logika)
   -> Zadne AI volani pro rizeni nabijeni

16. history_stats platform NEFUNGUJE uvnitr template: sekce (29.3.2026)
   -> Musi byt v top-level sensor: sekci
   -> Template integrace tichy ignoruje platform: history_stats bloky
   -> Symptom: senzor se nevytvori, zadna chyba v logu

17. Obyvak cidlo 1 (teplota_obyvak_temperature) ma offset +3-4C
   -> Pravdepodobne blizko zdroje tepla nebo vadna kalibrace
   -> Vyrazeno z prumeroveho senzoru (sensor.teplota_obyvak_prumer)
   -> climate.topeni pouziva prumer z cidel 2+3

## Zalohy — nastaveni (24.3.2026)
- InfluxDB vylouceno ze zaloh (bylo 6.3 GB z kazde zalohy)
- Zaloha bez InfluxDB: ~900 MB (drive 7.2 GB)
- Retence: 2 zalohy v HA
- Plan: 2 dny -> stare 7.2 GB zalohy zmizi automaticky -> ~1.8 GB celkem

## Huawei Baterie (overeno 25.3.2026)

### Klicove entity
- switch.battery_charge_from_grid: on/off nabijeni ze site
- number.battery_grid_charge_cutoff_soc: cilovy SOC (20-100%)
- number.battery_grid_charge_maximum_power: max vykon (0-10000W)
- number.battery_maximum_charging_power: max FVE nabijeni (0-5000W)
- number.battery_end_of_discharge_soc: min SOC (0-20%, aktualne 10%)
- number.battery_backup_power_soc: zalozni SOC (aktualne 20%)
- select.battery_working_mode: rezim baterie
- sensor.battery_state_of_capacity: aktualni SOC %
- sensor.batteries_forcible_charge: stav forced charging

### Rezimy (select.battery_working_mode)
- maximise_self_consumption (vychozi)
- time_of_use_luna2000
- fixed_charge_discharge
- fully_fed_to_grid
- adaptive

### Forcible charge ze site
service: huawei_solar.forcible_charge_soc
data:
  device_id: 289045a227358f942945b07e45ba6bed
  target_soc: 80
  power: 5000
-> Automaticky zastavi po dosazeni target SOC
-> Service call trva ~10-30s (invertor komunikace)

### TOU periody (aktualni)
- Period 1: 00:00-06:00 vsechny dny NABIJENI (+)
- Period 2: 08:00-22:00 vsechny dny VYBIJENI (-)

### Backup okruhy
- Zalohovane: lednice, HA server, domaci sit
- NEZALOHOVANE: vytapeni (TC), ohrev vody, nabijeni EV
- battery_backup_power_soc = 20%


## Mapovani zarizeni na HA entity (pro AI Agent)

### Energetika - FVE a baterie
| Zarizeni | Entity | Poznamka |
|:---------|:-------|:---------|
| FVE vykon | sensor.inverter_input_power | W |
| FVE denni vyroba | sensor.inverter_daily_yield | kWh |
| Baterie SOC | sensor.battery_state_of_capacity | % |
| Baterie vykon | sensor.battery_charge_discharge_power | W, kladne=nabijeni |
| Baterie rezim | select.battery_working_mode | maximise_self_consumption atd. |
| Baterie grid charge | switch.battery_charge_from_grid | on/off |
| Baterie cutoff SOC | number.battery_grid_charge_cutoff_soc | % |
| Baterie max grid power | number.battery_grid_charge_maximum_power | W |
| Baterie max charge power | number.battery_maximum_charging_power | W |
| Baterie min discharge SOC | number.battery_end_of_discharge_soc | % |
| Baterie backup SOC | number.battery_backup_power_soc | % |
| Grid tok | sensor.power_meter_active_power | W, kladne=export |
| Grid faze A | sensor.power_meter_phase_a_active_power | W |
| Grid faze B | sensor.power_meter_phase_b_active_power | W |
| Grid faze C | sensor.power_meter_phase_c_active_power | W |
| Forcible charge | sensor.batteries_forcible_charge | Stopped/Running |

### Tepelne cerpadlo (TC)
DULEZITE: Dotazy na "spotreba TC", "prikon TC", "kolik bere TC" -> pouzij Shelly EM3 senzory!
Dotazy na "stav TC", "topi TC", "rezim TC" -> pouzij switch a climate entity.

| Zarizeni | Entity | Poznamka |
|:---------|:-------|:---------|
| TC prikon CELKEM (W) | sensor.shelly_3em_okamzita_spotreba | pouzij pro "spotreba/prikon TC" |
| TC prikon faze A | sensor.shellyem3_34945475ecce_channel_a_power | W |
| TC prikon faze B | sensor.shellyem3_34945475ecce_channel_b_power | W |
| TC prikon faze C | sensor.shellyem3_34945475ecce_channel_c_power | W |
| TC topeni switch | switch.tepelnecerpadlo_topeni | on/off - fyzicke spinani |
| TC ventil topeni | switch.tepelnecerpadlo_3w_topeni | NIKDY oba 3w soucasne! |
| TC ventil bojler | switch.tepelnecerpadlo_3w_teplavoda | NIKDY oba 3w soucasne! |
| Spirala bojleru | switch.tepelnecerpadlo_bojler | on/off |
| Climate topeni | climate.topeni | heat/off, target temp |
| Letni rezim | input_boolean.summer_mode | on/off |

### Bojler
| Zarizeni | Entity |
|:---------|:-------|
| Bojler horni teplota | sensor.teplota_bojler_temperature |
| Bojler spodni teplota | sensor.teplota_bojler_spodni_teplota |

### Daikin klimatizace
| Mistnost | Climate entity | Teplotni cidlo |
|:---------|:---------------|:---------------|
| Adela pokoj | climate.adela_pokoj_room_temperature | sensor.adela_pokoj_temperature |
| Nela pokoj | climate.nela_pokoj_room_temperature | sensor.nela_pokoj_temperature |
| Pracovna | climate.pracovna_room_temperature | sensor.2_temperature |
| Loznice | climate.loznice_room_temperature | bez cidla |

### EV Wallbox (Tuya 3f)
| Parametr | Entity | Poznamka |
|:---------|:-------|:--------|
| Stav wallboxu | sensor.ev_charger_stav | Volny/Pripojeno/Ceka/Nabiji/Dokonceno/Pauza/Porucha |
| Vykon | sensor.ev_charger_vykon | kW |
| Faze vykon | sensor.ev_charger_phase_power | W (read-only) |
| Proud nastaven | sensor.ev_charger_proud_nastaven | A |
| Energie session | sensor.ev_charger_energie_seance | kWh |
| Switch | switch.ev_charger_switch | on/off |
| Proud slider | input_number.ev_charger_proud | 6-16A |
| Teplota | sensor.ev_charger_teplota | C |
| POZOR: jen 3f, min 6A = 4.14 kW | | 1f neni mozne |

### EV - Skoda Elroq
| Parametr | Entity |
|:---------|:-------|
| SOC | sensor.skoda_elroq_battery_percentage |
| Dojezd | sensor.skoda_elroq_range |
| Pripojen | binary_sensor.skoda_elroq_charger_connected |
| Nocni nabijeni zakazat | input_boolean.ev_nocharge_tonight |
| Cilovy SOC | input_number.ev_target_soc_tomorrow |

### Teploty a pritomnost
| Mistnost/Zarizeni | Entity |
|:------------------|:-------|
| Venkovni teplota | sensor.venkovni_teplota_temperature |
| Obyvak cidlo 1 | sensor.teplota_obyvak_temperature | OFFSET +3-4C, vyrazeno z prumeru |
| Obyvak cidlo 2 | sensor.obyvak2_temperature |
| Obyvak cidlo 3 | sensor.teplota_obyvak3_temperature |
| Obyvak prumer (ridici) | sensor.teplota_obyvak_prumer | prumer cidel 2+3, target pro climate.topeni |
| Adela pokoj | sensor.adela_pokoj_temperature |
| Nela pokoj | sensor.nela_pokoj_temperature |
| Pracovna | sensor.2_temperature |


### Ford PHEV
| Parametr | Entity |
|:---------|:-------|
| SOC (EV baterie) | sensor.fordpass_wf0fxxwpmhsc70607_soc |
| EV dojezd | sensor.fordpass_wf0fxxwpmhsc70607_elveh |
| Palivo | sensor.fordpass_wf0fxxwpmhsc70607_fuel |
| Nabijeni stav | sensor.fordpass_wf0fxxwpmhsc70607_elvehcharging |
| Konektor | sensor.fordpass_wf0fxxwpmhsc70607_elvehplug |
| Posledni session kWh | sensor.fordpass_wf0fxxwpmhsc70607_energytransferlogentry |
| Tachometr | sensor.fordpass_wf0fxxwpmhsc70607_odometer |
| Poloha | device_tracker.fordpass_wf0fxxwpmhsc70607_tracker |
| Zamek | lock.fordpass_wf0fxxwpmhsc70607_doorlock |
| Konektivita | sensor.fordpass_wf0fxxwpmhsc70607_deviceconnectivity |
### HDO tarif
| Parametr | Entity | Poznamka |
|:---------|:-------|:--------|
| Nizky tarif (NT) | binary_sensor.hdo_nizky_tarif_nt | on=NT, off=VT |
| Atribut tarif | state_attr(..., "tarif") | NT/VT |
| HDO kod | PTV3 | CEZ Distribuce |
| NT casy | 00-08, 09-12, 13-15, 16-19, 20-24 | |
| VT casy | 08-09, 12-13, 15-16, 19-20 | |

### Predikce
| Parametr | Entity |
|:---------|:-------|
| Solar confidence zitra | sensor.solar_confidence_tomorrow |
| Heating manager stav | sensor.heating_manager_status |


## Tablet dashboard (28.3.2026)

### Hardware
- Lenovo Tab M10, 10.1", 1280x800, landscape
- Fully Kiosk Browser Plus
- IP: 10.0.0.154
- URL: http://10.0.0.55/tablet
- HA integrace: fully_kiosk (lenovo_tab_m10)

### Obsah dashboardu
- Bojler: gradient bar, teplota, badge OK/HREJE SE/STUDENA, tlacitko Spustit ohrev
- Vytapeni: cilova teplota +/- tlacitka, teploty mistnosti (5 pokoju)
- Pritomnost: 4 osoby s barevnymi avatary
- Ford Kuga PHEV: foto, zamek, EV dojezd, benzin
- Skoda Elroq: foto, zamek, SOC, dojezd
- Roborock Saros 10R: foto, stav, tlacitko spustit uklid
- Pracka + susicka: foto, stav, posledni cyklus (kWh, L, CZK)
- Radio: 6 stanic (Radiozurnal, Evropa 2, Frekvence 1, Helax, Fresh Radio, Hitradio Orion)
- Pocasi: aktualni + predpoved 2 dny (Met.no)

### Soubory
- Server: 10.0.0.55, /opt/projects/tablet-dashboard/
- index.html, config.json (HA token), fotky (ford/elroq/roborock/pracka/susicka.jpg)
- Radio loga: radio_radiozurnal.png, radio_evropa2.png, radio_frekvence1.png, radio_helax.svg, radio_fresh.png, radio_orion.svg
- Nginx proxy: /ha-api/ -> https://ha.hanusek.net/api/ (CORS workaround)

### Fully Kiosk automatizace (automations/tablet_brightness.yaml)
- 05:00 jas 60, 06:00 jas 180, 20:00 jas 80, 23:00 jas 20
- Hodinovy reload stranky (button.lenovo_tab_m10_restart_browser)

### AppDaemon: appliance_tracker.py (NOVY 28.3.2026)
- Sleduje cykly pracky a susicky (machine_state run->stop/end)
- Delta tracking: energy kWh, water L (jen pracka), cena CZK
- Cena = energie * 4.53 + voda_litry/1000 * 138
- Sensory: sensor.pracka_last_cycle, sensor.susicka_last_cycle
- InfluxDB: appliance_cycles

### DLM: Dynamic Load Management (ev_charging_manager.py, 28.3.2026)
- Ochrana jistice 3x25A behem nabijeni EV
- Prahy: WARNING >18A (-2A), CRITICAL >21A (okamzite 6A), EMERGENCY >24A (kill)
- Nocni default: 13A (bezpecna rezerva pro TC)
- Obnoveni pri <15A na 6A, postupne zvysovani po 120s
- 30s check interval behem aktivniho nabijeni
## Tepelna charakteristika domu (analyza 29.3.2026)
- Merna tepelna ztrata: 96 W/C (regrese z 86 bodu, R2=0.65)
- Tepelna kapacita: 4200 Wh/C = 4.2 kWh/C (z 39 epizod ohrevu)
- Rychlost ohrevu TC: +0.60 C/h (prumer)
- Rychlost chladnuti bez TC: -0.20 C/h (prumer)
- Cas 19->21C: ~3.3 hodiny
- Spotreba TC: den 22C = 7.2 kWh/den, noc 21C = 5.0 kWh/den
- TC je 3x predimenzovane (10kW vs max ztrata ~3.5kW pri -15C)
- Bojler = 5% spotreby TC, zbytek topeni
- Cena: kazdy stupen navic ~2.2 kWh/den = ~10 Kc/den

## Git

Repozitar: https://github.com/xkamil1/home-assistant-config
Auto-commit: denne 03:00 (git_autocommit.sh + HA automation)
Branch: main

## SSH pristup
root@10.0.0.67, heslo v memory, pres paramiko (sshpass neni k dispozici)

## Na konci kazde session Claude Code by mel:
1. Aktualizovat sekci "Posledni aktualizace"
2. Aktualizovat TODO
3. Pridat nove gotchas
4. Commitnout a pushnout
