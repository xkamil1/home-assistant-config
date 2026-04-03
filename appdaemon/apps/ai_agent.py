import appdaemon.plugins.hass.hassapi as hass
import requests
import json
import re
from datetime import datetime

# System prompt for Sonnet - interpretation and planning
SONNET_SYSTEM = """
Jsi AI agent pro rizeni Home Assistant systemu.
Dostanes dokumentaci systemu, aktualni stav entit a instrukci uzivatele.

Tvuj ukol:
1. Pouzij dokumentaci pro pochopeni systemu a spravne mapovani entit
2. Sestav presny akcni plan jako JSON
3. Vrat POUZE validni JSON

Dostupne akce: get_state, switch_on, switch_off, set_value,
select_option, set_temperature

Format JSON:
{
  "summary": "Strucny popis (cesky)",
  "confirmation_prompt": "Text pro uzivatele pred provedenim (cesky)",
  "requires_confirmation": true/false,
  "steps": [
    {"action": "...", "entity": "...", "value": "...",
     "expected": "...",
     "description": "Co tento krok dela (cesky)"}
  ]
}

Pokud instrukce je dotaz (read-only) -> requires_confirmation: false
Pokud instrukce meni stav -> requires_confirmation: true

KRITICKE: switch.tepelnecerpadlo_3w_teplavoda a switch.tepelnecerpadlo_3w_topeni
NIKDY nesmi byt ON soucasne! Bojler ma PREDNOST.

Pokud entita neexistuje nebo instrukce neni jasna:
{"error": "Popis problemu cesky"}
"""

# System prompt for Haiku - action evaluation
HAIKU_SYSTEM = """
Jsi exekutor akci pro Home Assistant. Dostanes JSON akcni plan
a seznam vysledku kroku ktere byly provedeny.

Tvuj ukol:
1. Pro kazdy krok zkontroluj vysledek
2. Pokud je expected hodnota - over ji
3. Vrat strucne shrnuti cesky co bylo provedeno a zda vse probehlo OK

Format odpovedi (kazdy radek zvlast):
OK: <co bylo hotovo>
FAIL: <co selhalo s duvodem>
INFO: <dulezita informace>
DONE: <zaverecne shrnuti>
"""


class AIAgent(hass.Hass):

    def initialize(self):
        self._api_key = self.args.get("anthropic_api_key", "")
        self._influx_host = self.args.get("influxdb_host", "a0d7b954-influxdb")
        self._influx_port = self.args.get("influxdb_port", 8086)
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_auth = (
            self.args.get("influxdb_user", "db_write"),
            self.args.get("influxdb_password", "db_write_pass")
        )
        self._log_messages = []
        self._pending_plan = None
        self._stats = {"calls_sonnet": 0, "calls_haiku": 0,
                       "tokens_in": 0, "tokens_out": 0}
        self._initial_credit_usd = float(self.args.get("initial_credit_usd", 24.65))
        self._total_cost_usd = 0.0
        self._query_costs = []  # last N query costs for averaging
        self._usd_czk = float(self.args.get("usd_czk_fallback", 23.0))

        # Restore stats from InfluxDB
        self._restore_stats()
        self._update_exchange_rate()

        # Weekly exchange rate update (Monday 06:00)
        self.run_daily(self._update_exchange_rate,
                       datetime.now().replace(hour=6, minute=0, second=0))

        # Listen on input_text
        self.listen_state(self._on_request,
                          "input_text.ai_agent_request")

        # Listen on confirm/reject buttons via state change
        self.listen_state(self._on_confirm_btn,
                          "input_button.ai_agent_confirm")
        self.listen_state(self._on_reject_btn,
                          "input_button.ai_agent_reject")

        self._update_log_sensor()
        self._update_stats_sensor()
        self._update_pending_sensor("idle", {})
        self._update_history_sensor()
        self._add_log("AI Agent pripraven (Sonnet->Haiku)")
        self.log("AIAgent initialized")

    # --- INPUT ---

    def _on_request(self, entity, attribute, old, new, kwargs):
        if not new or new == old or new in ("unknown", ""):
            return
        self._log_messages = []
        self._pending_plan = None
        self._last_instruction = new
        self._add_log("Instrukce: {}".format(new))
        self._add_log("Sonnet analyzuje instrukci...")
        self._update_pending_sensor("analyzing", {})
        self.run_in(lambda *a: self._plan_with_sonnet(new), 0)

    def _load_system_context(self):
        """Load relevant sections from CLAUDE_CONTEXT.md."""
        try:
            # AppDaemon maps HA config to /homeassistant/
            import os
            for path in ['/homeassistant/CLAUDE_CONTEXT.md', '/config/CLAUDE_CONTEXT.md']:
                if os.path.exists(path):
                    break
            with open(path, 'r', encoding='utf-8') as f:
                doc = f.read()
            relevant_keywords = [
                'Konvence', 'entity', 'Entity', 'Huawei', 'Baterie',
                'AppDaemon', 'TC', 'Bojler', 'Daikin', 'EV', 'FVE',
                'Mapov', 'Energetika', 'vytap', 'Vytap', 'Elroq',
                'Architektura', 'Hardware', 'Pritomnost', 'pritomnost',
            ]
            sections = []
            current = []
            include = False
            for line in doc.split('\n'):
                if line.startswith('## '):
                    if current and include:
                        sections.append('\n'.join(current))
                    current = [line]
                    include = any(k in line for k in relevant_keywords)
                else:
                    current.append(line)
            if current and include:
                sections.append('\n'.join(current))
            result = '\n\n'.join(sections)
            self.log("Loaded system context: {} chars from {} sections".format(
                len(result), len(sections)))
            return result
        except Exception as e:
            self.log("Cannot load CLAUDE_CONTEXT.md: {}".format(e), level="WARNING")
            return ""

    def _plan_with_sonnet(self, instruction):
        ha_context = self._get_context()
        system_doc = self._load_system_context()
        prompt = (
            "DOKUMENTACE SYSTEMU:\n{}\n\n"
            "AKTUALNI STAV HA ENTIT:\n{}\n\n"
            "INSTRUKCE UZIVATELE: {}\n\n"
            "Sestav akcni plan jako JSON."
        ).format(system_doc, ha_context, instruction)

        try:
            response_text, tokens = self._call_claude(
                "claude-sonnet-4-20250514", SONNET_SYSTEM, prompt)
            self._stats["calls_sonnet"] += 1
            self._stats["tokens_in"] += tokens.get("input_tokens", 0)
            self._stats["tokens_out"] += tokens.get("output_tokens", 0)
            self._track_cost("sonnet", tokens)

            # Parse JSON
            clean = re.sub(r'```json\s*|\s*```', '', response_text).strip()
            plan = json.loads(clean)

            if "error" in plan:
                self._add_log("Sonnet: {}".format(plan['error']))
                self._update_pending_sensor("idle", {})
                return

            self._add_log("Plan: {}".format(plan.get('summary', '')))
            self._add_log("Kroku: {}".format(len(plan.get('steps', []))))

            for i, step in enumerate(plan.get("steps", []), 1):
                self._add_log("  {}. {}".format(
                    i, step.get('description', step.get('action', ''))))

            if plan.get("requires_confirmation", True):
                self._pending_plan = plan
                self._update_pending_sensor("waiting_confirmation", plan)
                self._add_log("Cekam na potvrzeni...")
            else:
                self._add_log("Spoustim bez potvrzeni...")
                self._execute_plan(plan)

        except json.JSONDecodeError as e:
            self._add_log("Chyba parsovani JSON: {}".format(e))
            self._add_log("Odpoved: {}".format(response_text[:200]))
            self._update_pending_sensor("idle", {})
        except Exception as e:
            self._add_log("Chyba Sonnet: {}".format(e))
            self._update_pending_sensor("idle", {})

    # --- CONFIRMATION ---

    def _on_confirm_btn(self, entity, attribute, old, new, kwargs):
        """Called when input_button.ai_agent_confirm is pressed."""
        if not self._pending_plan:
            self._add_log("Nic k potvrzeni")
            return
        self._add_log("Potvrzeno - spoustim Haiku...")
        plan = self._pending_plan
        self._pending_plan = None
        self._update_pending_sensor("executing", {})
        self.run_in(lambda *a: self._execute_plan(plan), 0)

    def _on_reject_btn(self, entity, attribute, old, new, kwargs):
        """Called when input_button.ai_agent_reject is pressed."""
        if not self._pending_plan:
            return
        self._add_log("Zamitnuto uzivatelem")
        self._pending_plan = None
        self._update_pending_sensor("idle", {})

    # --- EXECUTION ---

    def _execute_plan(self, plan):
        results = []

        for step in plan.get("steps", []):
            action = step.get("action", "")
            desc = step.get("description", action)

            self._add_log("Provadim: {}".format(desc))

            try:
                result = self._execute_step(step)
                results.append({"step": desc, "result": result, "ok": True})
                self._add_log("  -> {}".format(result))
            except Exception as e:
                results.append({"step": desc, "result": str(e), "ok": False})
                self._add_log("  -> CHYBA: {}".format(e))

        # Haiku evaluates results
        self._add_log("Haiku hodnoti vysledky...")
        self._summarize_with_haiku(plan, results)

        # Save to InfluxDB
        self._save_to_influxdb(plan.get("summary", ""), results, self._last_instruction)
        self._update_pending_sensor("idle", {})
        self._update_history_sensor()

        # Push notification with result
        ok_count = sum(1 for r in results if r.get("ok"))
        fail_count = len(results) - ok_count
        if fail_count == 0:
            msg = "{}".format(plan.get('summary', 'Akce dokoncena'))
        else:
            msg = "{} ({} ok, {} chyb)".format(
                plan.get('summary', 'Akce dokoncena'), ok_count, fail_count)
        try:
            self.call_service("notify/mobile_app_iphone_17",
                              title="AI Agent", message=msg)
        except Exception as e:
            self.log("Push notify error: {}".format(e))

    def _execute_step(self, step):
        action = step.get("action", "")
        entity = step.get("entity", "")
        value = step.get("value")
        option = step.get("option")
        expected = step.get("expected")

        if action == "switch_on":
            domain = entity.split(".")[0]
            if domain == "input_boolean":
                self.call_service("input_boolean/turn_on", entity_id=entity)
            else:
                self.call_service("switch/turn_on", entity_id=entity)
            return "{} = on".format(entity)

        elif action == "switch_off":
            domain = entity.split(".")[0]
            if domain == "input_boolean":
                self.call_service("input_boolean/turn_off", entity_id=entity)
            else:
                self.call_service("switch/turn_off", entity_id=entity)
            return "{} = off".format(entity)

        elif action == "set_value":
            domain = entity.split(".")[0]
            self.call_service("{}/set_value".format(domain),
                              entity_id=entity, value=float(value))
            state = self.get_state(entity)
            return "{} = {}".format(entity, state)

        elif action == "select_option":
            self.call_service("select/select_option",
                              entity_id=entity, option=option)
            state = self.get_state(entity)
            return "{} = {}".format(entity, state)

        elif action == "set_temperature":
            self.call_service("climate/set_temperature",
                              entity_id=entity, temperature=float(value))
            state = self.get_state(entity, attribute="temperature")
            return "{} target = {}C".format(entity, state)

        elif action == "get_state":
            state = self.get_state(entity)
            if expected:
                match = "OK" if str(state) == str(expected) else "MISMATCH"
                return "{} = {} (expected: {}) {}".format(
                    entity, state, expected, match)
            return "{} = {}".format(entity, state)

        else:
            return "Neznama akce: {}".format(action)

    def _summarize_with_haiku(self, plan, results):
        results_text = "\n".join(
            ["{} {}: {}".format(
                "OK" if r['ok'] else "FAIL", r['step'], r['result'])
             for r in results]
        )
        prompt = (
            "Plan byl: {}\n\nVysledky kroku:\n{}\n\n"
            "Zhodnot vysledky strucne."
        ).format(plan.get('summary', ''), results_text)

        try:
            response_text, tokens = self._call_claude(
                "claude-haiku-4-5-20251001", HAIKU_SYSTEM, prompt)
            self._stats["calls_haiku"] += 1
            self._stats["tokens_in"] += tokens.get("input_tokens", 0)
            self._stats["tokens_out"] += tokens.get("output_tokens", 0)
            self._track_cost("haiku", tokens)

            for line in response_text.split("\n"):
                if line.strip():
                    self._add_log(line.strip())

        except Exception as e:
            self._add_log("Haiku summary error: {}".format(e))

    # --- CLAUDE API ---

    def _call_claude(self, model, system, prompt):
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": model,
                "max_tokens": 1000,
                "system": system,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=60
        )
        data = response.json()
        if "error" in data:
            raise Exception(data["error"].get("message", "API error"))
        text = data["content"][0]["text"]
        usage = data.get("usage", {})
        self.log("Claude {} call: in={} out={} tokens".format(
            model.split("-")[1], usage.get("input_tokens", 0),
            usage.get("output_tokens", 0)))
        return text, usage

    # --- CONTEXT ---

    def _get_context(self):
        entities = {
            "battery_soc": "sensor.battery_state_of_capacity",
            "battery_mode": "select.battery_working_mode",
            "battery_grid_charge": "switch.battery_charge_from_grid",
            "battery_grid_cutoff_soc": "number.battery_grid_charge_cutoff_soc",
            "battery_grid_max_power": "number.battery_grid_charge_maximum_power",
            "battery_max_charge_power": "number.battery_maximum_charging_power",
            "battery_end_discharge_soc": "number.battery_end_of_discharge_soc",
            "battery_backup_soc": "number.battery_backup_power_soc",
            "battery_power_w": "sensor.battery_charge_discharge_power",
            "pv_power_w": "sensor.inverter_input_power",
            "grid_power_w": "sensor.power_meter_active_power",
            "ev_soc": "sensor.skoda_elroq_battery_percentage",
            "ev_range_km": "sensor.skoda_elroq_range",
            "ev_nocharge_tonight": "input_boolean.ev_nocharge_tonight",
            "ev_target_soc": "input_number.ev_target_soc_tomorrow",
            "boiler_temp": "sensor.teplota_bojler_spodni_teplota",
            "boiler_spirala": "switch.tepelnecerpadlo_bojler",
            "3w_teplavoda": "switch.tepelnecerpadlo_3w_teplavoda",
            "3w_topeni": "switch.tepelnecerpadlo_3w_topeni",
            "heating_target": "climate.topeni",
            "summer_mode": "input_boolean.summer_mode",
            "outdoor_temp": "sensor.venkovni_teplota_temperature",
            "indoor_temp": "sensor.teplota_obyvak_temperature",
            "solar_confidence_tomorrow": "sensor.solar_confidence_tomorrow",
            "forcible_charge": "sensor.batteries_forcible_charge",
        }
        lines = []
        for name, eid in entities.items():
            try:
                state = self.get_state(eid)
                if state is None:
                    state = "unavailable"
                lines.append("- {} ({}): {}".format(eid, name, state))
            except Exception:
                lines.append("- {} ({}): unavailable".format(eid, name))
        lines.append("- cas: {}".format(datetime.now().strftime("%H:%M")))
        return "\n".join(lines)

    # --- SENSORS ---

    def _add_log(self, message):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = "{} {}".format(ts, message)
        self._log_messages.append(entry)
        if len(self._log_messages) > 100:
            self._log_messages = self._log_messages[-100:]
        self._update_log_sensor()
        self.log(message)

    def _update_log_sensor(self):
        last = self._log_messages[-1] if self._log_messages else ""
        self.set_state("sensor.ai_agent_log",
            state=str(len(self._log_messages)),
            attributes={
                "friendly_name": "AI Agent Log",
                "messages": self._log_messages,
                "last_message": last,
                "updated": datetime.now().strftime("%H:%M:%S")
            }
        )

    def _update_pending_sensor(self, state, plan):
        self.set_state("sensor.ai_agent_pending",
            state=state,
            attributes={
                "friendly_name": "AI Agent Pending",
                "summary": plan.get("summary", ""),
                "confirmation_prompt": plan.get("confirmation_prompt", ""),
                "steps_count": len(plan.get("steps", [])),
                "steps": [s.get("description", "")
                          for s in plan.get("steps", [])]
            }
        )

    # --- COST TRACKING ---

    # Pricing per 1M tokens (USD)
    PRICING = {
        "sonnet": {"input": 3.0, "output": 15.0},
        "haiku":  {"input": 0.80, "output": 4.0},
    }

    def _calculate_cost(self, model, tokens):
        pricing = self.PRICING.get(model, self.PRICING["sonnet"])
        cost_in = tokens.get("input_tokens", 0) * pricing["input"] / 1_000_000
        cost_out = tokens.get("output_tokens", 0) * pricing["output"] / 1_000_000
        return round(cost_in + cost_out, 6)

    def _track_cost(self, model, tokens):
        cost = self._calculate_cost(model, tokens)
        self._total_cost_usd += cost
        self._query_costs.append(cost)
        if len(self._query_costs) > 50:
            self._query_costs = self._query_costs[-50:]
        self._update_stats_sensor()
        self._save_cost_to_influxdb(model, tokens, cost)

    def _update_exchange_rate(self, kwargs=None):
        """Fetch USD/CZK from CNB. Called daily, only updates on success."""
        try:
            resp = requests.get(
                "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/"
                "kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt",
                timeout=10)
            for line in resp.text.split("\n"):
                if "USD" in line:
                    # Format: USA|dolar|1|USD|23,456
                    rate_str = line.strip().split("|")[-1].replace(",", ".")
                    self._usd_czk = float(rate_str)
                    self.log("USD/CZK updated: {:.3f}".format(self._usd_czk))
                    break
        except Exception as e:
            self.log("CNB rate error: {} (using {:.2f})".format(e, self._usd_czk))

    def _update_stats_sensor(self):
        remaining = max(0, self._initial_credit_usd - self._total_cost_usd)
        avg_cost = (sum(self._query_costs) / len(self._query_costs)
                    if self._query_costs else 0)
        est_queries = int(remaining / avg_cost) if avg_cost > 0 else 0

        cost_czk = round(self._total_cost_usd * self._usd_czk, 2)
        remaining_czk = round(remaining * self._usd_czk, 1)
        avg_czk = round(avg_cost * self._usd_czk, 3)

        self.set_state("sensor.ai_agent_stats",
            state=str(self._stats["calls_sonnet"] + self._stats["calls_haiku"]),
            attributes={
                "friendly_name": "AI Agent Stats",
                "calls_sonnet": self._stats["calls_sonnet"],
                "calls_haiku": self._stats["calls_haiku"],
                "tokens_in": self._stats["tokens_in"],
                "tokens_out": self._stats["tokens_out"],
                "total_cost_czk": cost_czk,
                "estimated_remaining_czk": remaining_czk,
                "avg_cost_per_query_czk": avg_czk,
                "usd_czk_rate": self._usd_czk,
                "total_cost_session_usd": round(self._total_cost_usd, 4),
                "initial_credit_usd": self._initial_credit_usd,
                "estimated_remaining_usd": round(remaining, 2),
                "estimated_remaining_queries": est_queries,
                "avg_cost_per_query_usd": round(avg_cost, 5),
                "updated": datetime.now().strftime("%H:%M:%S")
            }
        )

    def _update_history_sensor(self):
        history = self._get_history(10)
        lines = []
        for h in history:
            lines.append("{} [{}] {}".format(h["time"], h["status"], (h.get("instruction") or h.get("summary") or "")[:60]))
        self.set_state("sensor.ai_agent_history",
            state=str(len(history)),
            attributes={
                "friendly_name": "AI Agent Historie",
                "entries": history,
                "display": lines,
                "updated": datetime.now().strftime("%H:%M:%S")
            }
        )

    def _save_cost_to_influxdb(self, model, tokens, cost):
        try:
            line = ("ai_agent_costs "
                    "model=\"{}\","
                    "tokens_in={}i,"
                    "tokens_out={}i,"
                    "cost_usd={},"
                    "cumulative_cost_usd={}").format(
                model,
                tokens.get("input_tokens", 0),
                tokens.get("output_tokens", 0),
                cost,
                round(self._total_cost_usd, 6))
            requests.post(
                "http://{}:{}/write".format(self._influx_host, self._influx_port),
                params={"db": self._influx_db},
                data=line.encode(),
                auth=self._influx_auth,
                timeout=5
            )
        except Exception as e:
            self.log("InfluxDB cost error: {}".format(e))

    def _save_to_influxdb(self, summary, results, instruction=""):
        try:
            ok_count = sum(1 for r in results if r.get("ok"))
            fail_count = len(results) - ok_count
            # InfluxDB line protocol: escape quotes and newlines
            def esc(s):
                s = s.replace("\\", "").replace('"', "'")
                s = s.replace("\n", " ").replace("\r", "")
                return s
            safe_summary = esc(summary)[:100]
            safe_instr = esc(instruction)[:200]
            line = ('ai_agent_history '
                    'summary="{}",'
                    'instruction="{}",'
                    'steps_total={}i,'
                    'steps_ok={}i,'
                    'steps_fail={}i').format(
                safe_summary, safe_instr, len(results), ok_count, fail_count)
            requests.post(
                "http://{}:{}/write".format(self._influx_host, self._influx_port),
                params={"db": self._influx_db},
                data=line.encode(),
                auth=self._influx_auth,
                timeout=5
            )
        except Exception as e:
            self.log("InfluxDB history error: {}".format(e))

    def _restore_stats(self):
        """Restore cumulative stats from InfluxDB on startup."""
        try:
            # Get total costs
            q = 'SELECT sum(cost_usd) FROM ai_agent_costs'
            resp = requests.get(
                "http://{}:{}/query".format(self._influx_host, self._influx_port),
                params={"db": self._influx_db, "q": q},
                auth=self._influx_auth, timeout=10)
            data = resp.json()
            series = data.get("results", [{}])[0].get("series", [])
            if series and series[0].get("values"):
                total = series[0]["values"][0][1]
                if total:
                    self._total_cost_usd = float(total)

            # Get call counts
            for model in ["sonnet", "haiku"]:
                q2 = "SELECT count(cost_usd) FROM ai_agent_costs WHERE model='{}'".format(model)
                resp2 = requests.get(
                    "http://{}:{}/query".format(self._influx_host, self._influx_port),
                    params={"db": self._influx_db, "q": q2},
                    auth=self._influx_auth, timeout=10)
                data2 = resp2.json()
                series2 = data2.get("results", [{}])[0].get("series", [])
                if series2 and series2[0].get("values"):
                    count = int(series2[0]["values"][0][1] or 0)
                    self._stats["calls_{}".format(model)] = count

            # Get total tokens
            q3 = 'SELECT sum(tokens_in), sum(tokens_out) FROM ai_agent_costs'
            resp3 = requests.get(
                "http://{}:{}/query".format(self._influx_host, self._influx_port),
                params={"db": self._influx_db, "q": q3},
                auth=self._influx_auth, timeout=10)
            data3 = resp3.json()
            series3 = data3.get("results", [{}])[0].get("series", [])
            if series3 and series3[0].get("values"):
                vals = series3[0]["values"][0]
                self._stats["tokens_in"] = int(vals[1] or 0)
                self._stats["tokens_out"] = int(vals[2] or 0)

            # Get recent query costs for averaging
            q4 = 'SELECT cost_usd FROM ai_agent_costs ORDER BY time DESC LIMIT 50'
            resp4 = requests.get(
                "http://{}:{}/query".format(self._influx_host, self._influx_port),
                params={"db": self._influx_db, "q": q4},
                auth=self._influx_auth, timeout=10)
            data4 = resp4.json()
            series4 = data4.get("results", [{}])[0].get("series", [])
            if series4 and series4[0].get("values"):
                self._query_costs = [v[1] for v in series4[0]["values"]
                                     if v[1] is not None]

            self.log("Stats restored: cost=${:.4f}, sonnet={}x, haiku={}x, tokens={}/{}".format(
                self._total_cost_usd, self._stats["calls_sonnet"],
                self._stats["calls_haiku"], self._stats["tokens_in"],
                self._stats["tokens_out"]))

        except Exception as e:
            self.log("Stats restore error: {}".format(e), level="WARNING")

    def _get_history(self, limit=10):
        """Get recent instruction history from InfluxDB."""
        try:
            q = 'SELECT instruction, summary, steps_ok, steps_fail FROM ai_agent_history ORDER BY time DESC LIMIT {}'.format(limit)
            resp = requests.get(
                "http://{}:{}/query".format(self._influx_host, self._influx_port),
                params={"db": self._influx_db, "q": q},
                auth=self._influx_auth, timeout=10)
            data = resp.json()
            series = data.get("results", [{}])[0].get("series", [])
            if not series:
                return []
            history = []
            for row in series[0].get("values", []):
                ts = row[0][:16].replace("T", " ")
                instruction = row[1] or ""
                summary = row[2] or ""
                ok = row[3] or 0
                fail = row[4] or 0
                status = "OK" if fail == 0 else "FAIL"
                history.append({
                    "time": ts, "instruction": instruction,
                    "summary": summary, "status": status
                })
            return history
        except Exception:
            return []
