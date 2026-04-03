# Udrzba systemu (stav 24.3.2026)

## Disk
- Celkem: 50.5 GB, volne ~11 GB (77%)
- Zalohy: 2x ~900 MB (InfluxDB vylouceno)
- HA databaze: 1.9 GB (recorder 10 dni)

## Zalohy
- Frekvence: denne ~05:00
- Retence: 2 zalohy
- Obsah: HA config + AppDaemon + Grafana + SSH + Mosquitto (BEZ InfluxDB)
- Velikost: ~900 MB (drive 7.2 GB s InfluxDB)

## InfluxDB vs Recorder
| Data | Recorder | InfluxDB |
|:-----|:--------:|:--------:|
| Teploty, vykony (W, kWh, C) | 10 dni | od 10/2023 |
| climate stavy (heat/off) | 10 dni | od 24.3.2026 (state_log) |
| switch stavy (on/off) | 10 dni | od 24.3.2026 (state_log) |
| device_tracker (home/away) | 10 dni | od 24.3.2026 (presence_log) |

## Plan
1. Za tyden: recorder retence na 5 dni
2. Po snizeni: vyloucit Historii ze zalohy (~300 MB zaloha)

## Git auto-commit
- Skript: /config/git_autocommit.sh
- Cas: denne 03:00
- Repo: https://github.com/xkamil1/home-assistant-config
