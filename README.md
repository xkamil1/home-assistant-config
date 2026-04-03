# Home Assistant - Energy & Heating Automation

Automatizacni system pro rizeni energetiky, nabijeni EV a vytapeni
rodinneho domu s FVE, baterii, tepelnym cerpadlem a klimatizacemi Daikin.

## System

| Komponenta | Popis |
|:-----------|:------|
| FVE | 7.4 kWp, Huawei SUN2000 |
| Baterie | ~10 kWh (Huawei LUNA) |
| TC | Tepelne cerpadlo - podlahove topeni + radiatory |
| EV | Skoda Elroq 77 kWh + Ford PHEV |
| Wallbox | Tuya 3-fazovy 4-16A |
| Daikin | 4x split (loznice, Nela, Adela, pracovna) |
| Bojler | 200l, TC ohrev + spirala 1.5 kW |

## AppDaemon aplikace

| App | Soubor | Funkce |
|:----|:-------|:-------|
| EnergyAI | energy_ai.py | Reaktivni rizeni nabijeni EV (Claude Haiku) |
| EV Charger | ev_charger.py | Tuya wallbox control |
| Boiler Surplus | boiler_surplus.py | Spirala bojleru z prebytku FVE |
| Solar Confidence | solar_confidence.py | Predpoved FVE + feedback loop |
| Energy Planner | energy_planner.py | Planovani nabijeni EV + 5denni vyhled |
| Consumption Monitor | consumption_monitor.py | Rozklad spotreby per zarizeni |
| Heating Manager | heating_manager.py | TC + Daikin rizeni podle pritomnosti |
| Presence Patterns | presence_patterns.py | Vzorce chovani domacnosti |
| PND | pnd.py | Scraping CEZ distribuce |

## Konvence

### power_meter_active_power
- Kladne = export do site (prebytek FVE)
- Zaporne = import ze site

### Fazove prirazeni
| Faze | Spotrebice |
|:-----|:-----------|
| A | TC (cast), wallbox (cast) |
| B | TC (cast), wallbox (cast), spirala bojleru |
| C | TC (cast), wallbox (cast), pracka, susicka, Daikin outdoor |

## InfluxDB measurements

| Measurement | Interval | Popis |
|:------------|:---------|:------|
| solar_prediction | 30 min | Predikce FVE |
| solar_prediction_accuracy | 1h | Verifikace vs skutecnost |
| consumption_breakdown | 5 min | Spotreba per zarizeni |
| ev_charger_data | 30s | Wallbox data |
| ev_daily_km | denne | Najezd Elroq |
| heating_log | pri zmene | Akce heating manageru |
| presence_transitions | realtime | Prichody/odchody |

## Pravidla
Viz CLAUDE_CODE_RULES.md

## Baterie — nabijeni ze site

Forcible charge (nabiti baterie ze site na cilovy SOC):
- service: huawei_solar.forcible_charge_soc
- device_id: 289045a227358f942945b07e45ba6bed
- target_soc: 80, power: 5000
- Automaticky zastavi po dosazeni cile
