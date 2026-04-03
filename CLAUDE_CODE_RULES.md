# Claude Code Rules — Home Assistant

## Pravidlo 1 — Zaloha pred kazdou zmenou lovelace

Pred jakymkoliv zapisem do lovelace VZDY nejdrive:

```bash
cp /config/.storage/lovelace.lovelace /config/.storage/lovelace.lovelace.bak_$(date +%Y%m%d_%H%M)
echo "Zaloha: $(ls -t /config/.storage/lovelace.lovelace.bak_* | head -1)"
find /config/.storage -name "lovelace.lovelace.bak_*" -mtime +7 -delete
```

**POZOR:** HA storage mode nepodporuje REST API pro lovelace config.
Lovelace je v `/config/.storage/lovelace.lovelace` (JSON, 80-100 KB).
Pouzivej `jq` pro modifikace — nikdy primo `sftp.file('w')` bez zalohy.

## Pravidlo 2 — Overeni pred zapisem

Po sestaveni nove konfigurace ji nejdrive zobraz a vyckej na potvrzeni.
Nikdy nezapisuj do lovelace bez predchoziho zobrazeni vysledku.

```bash
# Overeni po zmene:
jq ".data.config.views | map({title, cards_count: (.cards|length)})" /config/.storage/lovelace.lovelace
```

## Pravidlo 3 — Nikdy netruncovat existujici soubory

Pri zapisu do existujicich souboru VZDY:
1. Nacti obsah do promenne
2. Uprav v pameti
3. Over ze vysledek je validni JSON/YAML
4. Teprve pak zapis

**KRITICKE:** Nikdy nepouzivej `open(soubor, 'w')` nebo `sftp.file(path, 'w')`
bez predchozi zalohy obsahu v promenne. SFTP open('w') OKAMZITE truncatuje
soubor na 0 bytes — pokud nasledny zapis selze (encoding error, timeout),
original je NENÁVRATNĚ ztracen.

Bezpecny postup:
```python
# 1. Zaloha
ssh.exec_command('cp /config/.storage/lovelace.lovelace /config/.storage/lovelace.lovelace.bak')

# 2. Modifikace pres jq (atomicka operace)
ssh.exec_command('jq --slurpfile nv /tmp/new_views.json ".data.config.views += $nv[0]" '
                 '/config/.storage/lovelace.lovelace.bak > /tmp/lovelace_new.json')

# 3. Overeni
ssh.exec_command('jq ".data.config.views | length" /tmp/lovelace_new.json')

# 4. Zapis az po overeni
ssh.exec_command('cp /tmp/lovelace_new.json /config/.storage/lovelace.lovelace')
```

## Pravidlo 4 — Obnoveni ze zalohy

Pokud dojde k poskozeni konfigurace:

```bash
# Z lokalni zalohy:
BACKUP=$(ls -t /config/.storage/lovelace.lovelace.bak_* 2>/dev/null | head -1)
cp "$BACKUP" /config/.storage/lovelace.lovelace
ha core restart

# Z HA automaticke zalohy (sifrovane):
# 1. Zjisti heslo:
jq ".data.config.create_backup.password" /config/.storage/backup
# 2. Obnov:
ha backups restore <SLUG> --homeassistant --password "<HESLO>"
```

## Pravidlo 5 — AppDaemon secrets

AppDaemon `!secret` cte z `/config/appdaemon/secrets.yaml`.
Po zmene secrets je NUTNY restart AppDaemon addonu:
```bash
ha addons restart a0d7b954_appdaemon
```
Pozor: restart trva 2-3 minuty (instalace pip balicku).
Alternativa: vlozit hodnoty primo do `apps.yaml` (funguje okamzite pri reload).

## Pravidlo 6 — Disk space

Pred velkymi operacemi (backup restore) over volne misto:
```bash
df -h /config
```
Zalohy v `/backup/` mohou zabrat 20+ GB. Stare mazat pres:
```bash
ha backups delete <SLUG>
```

## Pravidlo 7 — Bezpecna editace configuration.yaml

configuration.yaml obsahuje HA-specificke YAML tagy (!include,
!include_dir_merge_list, !secret) ktere standardni Python yaml modul
NEZNA a zpusobi chybu pri validaci.

**NIKDY nepouzivej yaml.safe_load() ani yaml.dump() na configuration.yaml.**

PRED kazdou upravou:
1. Zaloha: `cp /config/configuration.yaml /config/configuration.yaml.bak_$(date +%Y%m%d_%H%M)`
2. Edituj pouze textove (sed, grep, rucni nahrazeni radku)
3. Po uprave over syntaxi pres HA:
   ```bash
   ha core check 2>&1
   ```
4. Pokud check selze -> obnov zalohu PRED restartem
5. NIKDY nerestartuj HA bez overeni syntaxe

Pri mazani bloku z configuration.yaml:
- Pouzij sed nebo rucni cut radku
- Over ze nezbyvaji orphaned fragmenty (prazdne radky s odsazenim,
  osirely Jinja2 kod bez kontextu)
- Vzdy zkontroluj oblast kolem smazaneho bloku

## Pracovni postup

Na zacatku kazde session:
1. Precti /config/CLAUDE_CODE_RULES.md
2. Precti /config/CLAUDE_CONTEXT.md
3. Precti /config/.claude/lessons.md - NEopakuj stejne chyby!

Pri reseni ukolu postupuj takto:

1. ANALYZA — pred zacatkem vysvetli plan (co udelas a proc)
2. IMPLEMENTACE — pis kod, upravuj soubory
3. VALIDACE — pred dokoncenim vzdy over:
   - Kod/konfigurace funguje (test, check_config, logy)
   - Zadne nove chyby v HA logu
   - Entity/sensory maji spravne hodnoty
4. DOKUMENTACE — po uspesne validaci:
   - Aktualizuj CLAUDE_CONTEXT.md (TODO, gotchas, nove entity)
   - Commit a push do Gitu s popisnym commit message
5. SHRNUTI — vypis co bylo udelano, co bylo otestovano, co zbyva

Pokud narazis na problem ktery nemuzes vyresit samostatne, zastav se a popis situaci.
Nezavadet breaking changes bez zalohy.

Po kazde oprave/chybe:
- Zapis pouceni do /config/.claude/lessons.md
- Formuluj jako pravidlo ktere zabrani stejne chybe

Elegance check (pred commitem):
- 'Je toto nejjednodussi reseni?'
- 'Je toto citelne a udrzovatelne?'
- 'Pokud je fix hacky -> implementuj elegantni reseni'
