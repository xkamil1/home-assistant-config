# Lessons Learned - Home Assistant projekt

## Lovelace / Dashboard
- REST API /api/lovelace/config vraci 404 v storage mode
  -> VZDY edituj primo /config/.storage/lovelace.lovelace
  -> VZDY zaloha pred zapisem
  -> NUTNY ha core restart po kazde zmene (HA cte soubor jen pri startu)
- horizontal-stack rozdeluje sirku rovnomerne - nelze ovlivnit bez custom karet
  -> Pro presny layout pouzij custom:layout-card + custom:stack-in-card
- card-mod je nutny pro CSS styling standardnich karet
  -> Bez card-mod nelze stylovat markdown, entities, glance karty
- button-card custom_fields s Jinja2 sablonami nefunguje spolehlive
  -> Pouzij markdown kartu s inline HTML misto custom_fields

## AppDaemon
- AppDaemon set_state() vytvori "virtualni" entitu
  -> HA service cally (input_text.set_value) na ni NEFUNGUJI
  -> Pro HA service cally nutne definovat helper v configuration.yaml
- listen_event("call_service", domain="input_button") nezachytava press
  -> VZDY pouzij listen_state() na input_button entitu
- AppDaemon !secret nefunguje spolehlive v HA addon
  -> Pouzij inline hodnoty v apps.yaml + apps.yaml do .gitignore
- AppDaemon cte soubory z /homeassistant/ ne /config/
  -> Cesta pro open(): /homeassistant/CLAUDE_CONTEXT.md
- Po rotaci HA tokenu: NEJDRIV aktualizovat appdaemon.yaml
  POTOM zneplatnit stary token, jinak se AppDaemon zablokuje (ip_ban)
  -> appdaemon.yaml je v /addon_configs/a0d7b954_appdaemon/appdaemon.yaml
- input_button entity nemusi byt dostupna pri init AppDaemon
  -> Pouzij run_in(callback, 30) pro zpozdenou registraci listen_state

## HA Konfigurace
- Nikdy nepouzivej yaml.dump() pro zapis do configuration.yaml
  -> Vzdy textova manipulace (replace, sed) - yaml.dump zmeni formatovani
- platform: template a platform: history_stats jsou legacy
  -> Pouzij moderni template: sekci
- check_config pred kazdym restartem HA - nikdy nerestart bez overeni
- Pri string replace v configuration.yaml: overit ze pattern je unikatni
  -> "- sensor:" se vyskytuje vicekrat - replace zasahne vsechny
- climate.topeni temperature se nemeni periodic, jen pri prechodu stavu
  -> Uzivatelske nastaveni (Siri, dashboard) prezije do dalsi udalosti

## Tuya / Wallbox
- Wallbox je pouze 3f, minimum 6A x 3f = 4.14 kW
  -> Jednofazove nabijeni neni mozne
- work_state hodnoty: charger_free/insert/wait/charging/end/pause/fault
  -> Pouzij tyto raw hodnoty pro triggery, ne prelozene ceske verze
- tinytuya 1.17.6 sendcommand ma bug
  -> Pouzij _tuyaplatform() primo s {"commands": [...]} wrapperem

## TC / Topeni
- switch.tepelnecerpadlo_3w_teplavoda a 3w_topeni NIKDY soucasne ON
- Pri ukonceni ohrevu bojleru: rozhodnout zda TC necha bezet pro topeni
  -> Pokud obyvak < target: jen prepni ventil, kompresor nech bezet
  -> Pokud obyvak >= target: vypni kompresor, pak prepni ventil
- _boiler_active() musi trackovat _last_eval_state
  -> Jinak po skonceni bojleru dojde k falesne transition a reset teploty

## InfluxDB
- InfluxDB je v1 (InfluxQL) - HTTP API s username/password
- Casova zona: InfluxDB uklada UTC, HA zobrazuje CET (UTC+1)
- InfluxDB entity_id neobsahuje "sensor." prefix
  -> Query: entity_id = 'inverter_input_power' (ne 'sensor.inverter_...')
- Shell escaping v InfluxQL queries: pouzij temp soubor
  -> Zapsat query do /tmp/iq.txt, pak --data-urlencode "q@/tmp/iq.txt"

## Git / Bezpecnost
- apps.yaml obsahuje credentials -> VZDY v .gitignore
- GitHub token v push URL -> maskuj v output
- Historicke commity s credentials -> rotovat tokeny
- Po rotaci tokenu: aktualizovat VSECHNY mista (apps.yaml, appdaemon.yaml, secrets.yaml)

## Cloudflare Tunnel
- CNAME zaznam musi byt Proxied (oranzovy mrak) ne DNS only
- AppDaemon addon IP (172.30.33.x) muze byt zablokovana ip_ban
  -> Po rotaci tokenu zkontrolovat ip_bans.yaml
- WebSocket funguje nativne pres Cloudflare Tunnel - zadna extra konfigurace

## HDO / Tarify
- HDO casy PTV3 jsou orientacni - distributor se jimi nemusi ridit presne
- VT okna: 08-09, 12-13, 15-16, 19-20h
- Ohrev vody spoustet v NT (13:00), ne v poledne (12:00 = VT)

## REST integrace
- REST sensory potrebuji timeout: 30 (vychozi 10s je malo pro prvni volani)
- json_attributes_path: "$" nefunguje v moderni rest: integraci -> odstranit

## Tablet dashboard
- HTML5 audio prehravac funguje primo v prohlizeci bez instalace
- mix-blend-mode: lighten odstrani cerne pozadi fotek na tmavem pozadi
- Fully Kiosk Remote Admin vyzaduje Plus licenci
- config.json s HA tokenem nesmi byt v gitu
- Starsi Android WebView nepodporuje CSS gap ve flexboxu -> pouzit margin-bottom
- object-fit: contain + max-width/max-height pro loga ruznych pomeru stran

## DLM - Dynamic Load Management
- Fazove mereni: kladne = export, zaporne = import
  -> zatizeni faze = max(0, -hodnota) / 230
- CRITICAL bez debounce - jinak pri rychlem narustu nestihne reagovat
- Nocni default 13A (ne 16A) - bezpecna rezerva pro TC + dum
- Obnoveni wallboxu na 6A (ne plny proud) - postupne zvysovani

## Susicka entity
- Puvodni entity_id: susucka (preklep) -> opraveno na susicka (28.3.2026)
- Prejmenovani entity vyzaduje: ha core stop, edit registry, ha core start
- HA REST API nema endpoint pro entity rename - jen pres WebSocket nebo stop/edit/start
