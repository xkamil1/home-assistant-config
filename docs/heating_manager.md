# Heating Manager

Ridi TC a Daikin podle pritomnosti.

## Priority TC
1. Bojler ohrev -> SKIP
2. Letni rezim -> TC off
3. Noc 22-05 -> 20C
4. Nekdo doma -> 21C
5. Nikdo doma -> 19C
6. Predehrev Po-Pa 60min pred odchodem

## Daikin hystereze
- Zapni heat: < 19.0C
- Vypni heat: > 20.5C
- Zapni cool: > 25.0C
- Vypni cool: < 23.5C

## Ventil pravidlo
3w_teplavoda a 3w_topeni NIKDY soucasne ON.

## Watchdog
heating_watchdog.yaml - TC=20C a Daikin off pri vypadku.
