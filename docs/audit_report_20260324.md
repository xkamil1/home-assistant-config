# AUDIT REPORT — 2026-03-24

## Kriticke chyby (opravit ihned)

1. **consumption_untracked set_state 400 Bad Request**
   - consumption_monitor.py hazi 400 pri nastaveni sensor.consumption_untracked
   - Pricina: pravdepodobne special znaky ve friendly_name nebo state=None
   - Dopad: sensor se neaktualizuje kazdych 5 min

2. **490 nedostupnych entit**
   - Vetsina jsou stare device_trackery (78), sunsynk senzory (307 — stary invertor?)
   - 14 nedostupnych automatizaci (stare, nikdy nesmazane)

## Varovani (opravit brzy)

1. **weather.openweathermap = unavailable**
   - OWM integrace je unavailable — solar_confidence pocita jen z Met.no
   - Akce: zkontrolovat OWM API klic v HA integracich

2. **presence_log je EMPTY** (0 zaznamu)
   - Pridano dnes — naplni se az pri pristim prichodu/odchodu
   - Neni chyba, jen zatim zadna udalost

3. **recorder neni explicitne nakonfigurovan**
   - configuration.yaml nema sekci recorder:
   - Pouziva se vychozi (purge_keep_days: 10, auto_purge: true)
   - Doporuceni: pridat explicitni konfiguraci

4. **notify.homeassistant_k_5873762629 = unavailable**
   - Telegram notifikacni service je nedostupny
   - Dopad: heating_watchdog nemuze poslat alert

5. **person.romana = unknown** (zadny tracker pripojen)
   - person.adam = unknown, person.mosq_admin = unknown
   - Romana nema HA Companion app

## Mrtve entity

### Automatizace (14 nedostupnych)
| Automatizace | Doporuceni |
|:-------------|:-----------|
| import_data_z_pnd / import_dat_z_pnd | SMAZAT (PND je v AppDaemon) |
| zapnuti/vypnuti_bazenu | SMAZAT (zakomentovano) |
| aktualizace_denni_spotreby_elektriny | SMAZAT (stary) |
| spusteni/vypnuti_topne_spiraly_v_bojleru (3x) | SMAZAT (boiler_surplus je v AppDaemon) |
| pozastavit/obnovit_topeni_pri_ohrevu_vody (2x) | SMAZAT (heating_manager ridi) |
| notifikace_o_sepnuti_spinace_spare2 | SMAZAT (spare2 neexistuje) |
| turn_on_christmas_lights_afternoon/morning | SMAZAT (vanocni stromek) |

### Stare senzory (307 sunsynk)
- sensor.sunsynk_* — 307 senzoru, vsechny unavailable
- Pravdepodobne stara integrace (Sunsynk Power Flow Card?)
- Doporuceni: Najit a odebrat integraci v HA UI

### Device trackery (78 nedostupnych)
- Stare Unifi MAC adresy
- Nelze smazat pres API (Unifi integrace je spravuje)
- Doporuceni: Ignorovat nebo deaktivovat v entity registry

## Navrhy zlepseni

### 1. Energetika
- [ ] EnergyAI 8h horizont — pouzit solar_confidence pro lookahead
- [ ] Spotove ceny — pridat Czech Energy Spot Prices integraci
- [ ] FVE forecasting — obnovit OWM pro lepsi predikci

### 2. Vytapeni
- [ ] Loznice Daikin — pridat Zigbee cidlo pro regulaci
- [ ] Scheduler deaktivace — 4 pravidla koliduji s heating_manager
- [ ] Nocni teplota per mistnost — Daikin podkrovi snizit na 18C v noci

### 3. Pritomnost
- [ ] Romana — pridat HA Companion app na telefon
- [ ] GPS tracking — aktualne jen Kamil (iphone_17_2), pridat ostatni
- [ ] Zony — definovat pracoviste pro presnejsi odjezd/prijezd

### 4. Dashboardy
- [ ] View 4 (Teploty) je prazdny — pridat teplotni prehled
- [ ] View 5 (Spotreba elektriny) ma jen 1 kartu
- [ ] Heating karta zobrazuje "Nacitam" pri prvnim loadu

### 5. Systém
- [ ] Vyresit consumption_untracked 400 error
- [ ] Smazat 14 mrtvych automatizaci
- [ ] Odebrat sunsynk integraci (307 mrtvych senzoru)
- [ ] Pridat recorder: do configuration.yaml
- [ ] Opravit Telegram notifikace

## Akcni plan (priorita)

| Priorita | Akce | Slozitost | Dopad |
|:---------|:-----|:----------|:------|
| 1 | Fix consumption_untracked 400 | nizka | sensor nefunguje |
| 2 | Smazat 14 mrtvych automatizaci | nizka | cistota |
| 3 | Deaktivovat 4 scheduler pravidla | nizka | konflikty s heating_manager |
| 4 | Opravit OWM integraci | nizka | lepsi solar predikce |
| 5 | Opravit Telegram notifikace | nizka | watchdog alert |
| 6 | Pridat recorder: konfiguraci | nizka | kontrola retence |
| 7 | Odebrat sunsynk integraci | stredni | 307 mrtvych senzoru |
| 8 | Pridat Zigbee cidlo loznice | stredni | Daikin regulace |
| 9 | EnergyAI 8h horizont | vysoka | lepsi rozhodovani |
| 10 | Spotove ceny | vysoka | optimalizace nakladu |
