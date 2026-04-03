# Presence Patterns

Sleduje pritomnost 4 clenu domacnosti pomoci Unifi device trackeru.

## Osoby
| Osoba | Entity | Odchod | Prichod |
|:------|:-------|:-------|:--------|
| Kamil | device_tracker.iphone_19 | 05:26 | 15:24 |
| Romana | unifi_default_c2_eb_91_20_3b_6d | doma | - |
| Nela | unifi_default_de_f6_6b_c7_67_74 | 05:51 | 16:06 |
| Adela | unifi_default_0e_c7_df_8a_66_f9 | 06:01 | 16:10 |

## InfluxDB
- presence_transitions: hodina+minuta prechodu (pro prumery)
- presence_log: plny zaznam (person, transition, hour, is_workday, someone_home_after)

## Sensor
sensor.presence_patterns — prumerne casy per osoba, data_days, learning status
