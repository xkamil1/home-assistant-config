# Claude Context — Home Assistant Automation System
# Tento soubor aktualizuj po kazde vetsi zmene.
# Na zacatku session: "Precti CLAUDE_CODE_RULES.md a CLAUDE_CONTEXT.md"

## Posledni aktualizace
Datum: 2026-04-04
Session: Infrastrukturni opravy po VM vypadu, SSL proxy setup, voice stack diagnostika

## Pristi kroky (TODO)
- [ ] Krok 5 — EnergyAI 8hodinovy horizont (solar_confidence jako vstup)
- [ ] Loznice Daikin — ceka na teplotni cidlo
- [ ] Ford PHEV integrace do EnergyPlanner
- [ ] Scheduler deaktivace — overit po tydnu provozu heating_manager
- [x] Udrzba — zalohy, disk, databaze (hotovo 24.3.2026)
- [x] OWM integrace — obnovena 24.3.2026
- [ ] solar_confidence: vyuzit OWM cloud_coverage % primo misto condition mapping
- [ ] Prediktivni nabijeni baterie ze site pred slabymi dny
- [ ] Koupelna/kuchyne — pridat teplotni cidla
- [x] Audit systemu proveden 24.3.2026
- [ ] Recorder retence snizit na 5 dni (za tyden az se state_log naplni)
- [ ] Historii vyloucit ze zalohy (po snizeni retence)
- [x] AI Agent dvouvrstva architektura (Sonnet->Haiku) nasazeno 25.3.2026
  - [x] Layout: panel:true + stack-in-card + button-card
  - [ ] API statistiky se resetuji po restartu (pridat persistence do InfluxDB)
  - [ ] Redesign AI Agent dashboard (vysoka priorita - pro prezentaci)
  - Dark theme: #1a1a2e, #00b4d8, #06d6a0, #ef476f, #ffd166
  - Vstupni pole pres celou sirku - tlacitka pod polem nebo vpravo jako ikony
  - Live vystup: monospace font, zalamovani radku, scrollovatelny
  - Responsivni pro mobil i desktop
  - card-mod nainstalovano, button-card, layout-card, stack-in-card
  - Aktualni stav: card-mod styling nasazen, gradient tlacitka fungují
  - [ ] Pridat historii instrukci z InfluxDB na dashboard
- [x] energy_planner interaktivni rezim opraven
- [x] Notifikace - Telegram nahrazen push (mobile_app_iphone_17) 26.3.2026
- [x] AI Agent end-to-end overeno 26.3.2026
  - Sonnet cte CLAUDE_CONTEXT.md (/homeassistant/ path)
  - Mapovani entit funguje (TC prikon, teploty, baterie)
  - Credit tracking s per-model pricing
- [x] Vzdaleny pristup - Cloudflare Tunnel (26.3.2026)
  - ha.hanusek.net -> Cloudflare -> tunel -> http://10.0.0.67:8123
  - trusted_proxies pridany do configuration.yaml (helpers fix) 25.3.2026
- [x] SSL proxy pro lokalni pristup pres NPM (4.4.2026)
  - ha.hanusek.net -> NPM (10.0.0.55) -> http://10.0.0.67:8123 (lokalne)
  - esxi.hanusek.net -> NPM -> https://10.0.0.66:443 (proxy_ssl_verify off)
  - zabbix.hanusek.net -> NPM -> http://10.0.0.150:80
  - Vsechny sdili wildcard cert *.hanusek.net (Let's Encrypt, auto-renew)
  - DNS zaznamy na Windows DNS (10.0.0.70) smeruji na 10.0.0.55
  - 10.0.0.55 pridan do HA trusted_proxies
- [x] Invoice tracker REST URL opravena na port 8000 (primo backend, bypass NPM)
- [x] Appliance tracker: _save_last_cycle() doplneno volani (persistence nefungovala)
- [x] Whisper STT restartovan po DNS vypadu
- [x] Cloudflare tunel restartovan po DNS vypadu
- [ ] Health monitoring pro kriticke komponenty (Zabbix + HA watchdog s push notifikaci)
- [x] PND — login opraven 24.3.2026 (nove credentials)

## Architektura systemu

### Hardware
- FVE: 7.4 kWp, jih 45, Huawei SUN2000 invertor
- Baterie: ~10 kWh Huawei LUNA
- TC: tepelne cerpadlo — podlaha + radiatory, ovladano pres Tasmota
- Wallbox: Tuya, 3-fazovy, 4-16A
- Elroq: 77 kWh, ~85 km/den prumer, SOC/km ~0.20 (dynamicky)
- Ford PHEV: zatim mimo system
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

## AppDaemon Apps — aktualni stav

### energy_ai.py
- Reaktivni rizeni EV nabijeni kazdych 15 min (Claude Haiku)
- Respektuje: ev_nocharge_tonight, ev_target_soc_tomorrow, manual_override
- Bug opraveny: konvence znamenka grid_power_w (22.3.2026)

### ev_charger.py
- Tuya cloud API komunikace s wallboxem
- InfluxDB: ev_charger_data (kazdych 30s)
- Entity: ev_charger_phase_power, ev_charger_vykon

### boiler_surplus.py
- Spirala bojleru z prebytku FVE na fazi B
- Podminky: phase_b > 1800W AND teplota < 58C AND 7:00-17:00

### solar_confidence.py
- Met.no + OWM -> confidence 0-100%
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

### heating_manager.py
- TC + Daikin rizeni podle pritomnosti
- Priority: bojler > leto > noc > doma > pryc
- Daikin hystereze: heat ON <19C, OFF >20.5C; cool ON >25C, OFF <23.5C
- Ranni predehrev: Po-Pa, 60 min pred odchodem
- InfluxDB: heating_log
- Status rozlisuje: Topi / Doma (idle) / Away / Noc / Leto / Bojler prednost
- POZOR: 4 scheduler pravidla stale aktivni — deaktivovat po overeni

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
| 8 | AI Agent | 2 (vstup+statistiky, live vystup+potvrzeni) |

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
| Obyvak | sensor.teplota_obyvak_temperature |
| Adela pokoj | sensor.adela_pokoj_temperature |
| Nela pokoj | sensor.nela_pokoj_temperature |
| Pracovna | sensor.2_temperature |

### Predikce
| Parametr | Entity |
|:---------|:-------|
| Solar confidence zitra | sensor.solar_confidence_tomorrow |
| Heating manager stav | sensor.heating_manager_status |

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
