import appdaemon.plugins.hass.hassapi as hass
import requests
import json
import os
import glob
from datetime import datetime, timedelta


HEAT_LOSS_COEFF = 96    # W/C
THERMAL_CAPACITY = 4200  # Wh/C
REPORTS_DIR = "/homeassistant/heating_reports"

class WeeklyHeatingReport(hass.Hass):
    """Weekly heating report with Haiku AI analysis, every Monday 07:00."""

    def initialize(self):
        self._api_key = self.args.get("anthropic_api_key", "")
        self._notify = self.args.get("notify_entity", "notify.mobile_app_iphone_17")
        self._influx_host = self.args.get("influxdb_host", "a0d7b954-influxdb")
        self._influx_port = self.args.get("influxdb_port", 8086)
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_auth = (
            self.args.get("influxdb_user", "db_write"),
            self.args.get("influxdb_password", "db_write_pass"))
        self._kwh_price = float(self.args.get("kwh_price", 4.53))
        self._day_target = float(self.args.get("day_target", 22.0))
        self._night_target = float(self.args.get("night_target", 21.0))

        os.makedirs(REPORTS_DIR, exist_ok=True)

        # Every Monday at 07:00
        self.run_daily(self._scheduled_run,
                       datetime.now().replace(hour=7, minute=0, second=0))

        # Manual trigger + week selector
        self.run_in(self._register_triggers, 30)
        self.log("WeeklyHeatingReport initialized")

    def _register_triggers(self, kwargs):
        self.listen_state(self._on_manual, "input_button.heating_report_generate")
        self.listen_state(self._on_week_select, "input_select.heating_report_week")
        self._sync_week_selector()
        self._load_latest_report()

    def _on_manual(self, entity, attribute, old, new, kwargs):
        self.log("Manual heating report triggered")
        self._generate_report()

    def _on_week_select(self, entity, attribute, old, new, kwargs):
        if new and new != old:
            self._load_report(new)

    def _scheduled_run(self, kwargs):
        if datetime.now().weekday() != 0:  # Monday = 0
            return
        self.log("Scheduled weekly heating report")
        self._generate_report()

    # ── Data collection ────────────────────────────────────────────

    def _influx_query(self, query):
        r = requests.get(
            "http://{}:{}/query".format(self._influx_host, self._influx_port),
            params={"db": self._influx_db, "q": query},
            auth=self._influx_auth, timeout=15)
        return r.json()

    def _series_values(self, result):
        """Extract [(time, value)] from InfluxDB result."""
        s = result.get("results", [{}])[0].get("series", [])
        if not s:
            return []
        return [(v[0], v[1]) for v in s[0].get("values", []) if v[1] is not None]

    def _collect_week(self, days_ago_start, days_ago_end):
        """Collect data for a period: days_ago_start..days_ago_end (both positive ints)."""
        period = "time > now() - {}d AND time <= now() - {}d".format(
            days_ago_start, days_ago_end)
        hourly_period = period

        # TC power hourly (for fireplace detection and energy calc)
        tc_power = self._series_values(self._influx_query(
            'SELECT mean("value") FROM "W" WHERE "entity_id" = \'shelly_3em_okamzita_spotreba\' AND {} GROUP BY time(1h) fill(none)'.format(hourly_period)))

        # Indoor temp hourly
        indoor = self._series_values(self._influx_query(
            'SELECT mean("value") FROM "\u00b0C" WHERE "entity_id" = \'teplota_obyvak_temperature\' AND {} GROUP BY time(1h) fill(none)'.format(hourly_period)))

        # Outdoor temp daily
        outdoor_daily = self._series_values(self._influx_query(
            'SELECT mean("value") FROM "\u00b0C" WHERE "entity_id" = \'venkovni_teplota_temperature\' AND {} GROUP BY time(1d) fill(none)'.format(period)))

        # Outdoor temp hourly (for fireplace detection)
        outdoor_hourly = self._series_values(self._influx_query(
            'SELECT mean("value") FROM "\u00b0C" WHERE "entity_id" = \'venkovni_teplota_temperature\' AND {} GROUP BY time(1h) fill(none)'.format(hourly_period)))

        # Indoor temp daily
        indoor_daily = self._series_values(self._influx_query(
            'SELECT mean("value") FROM "\u00b0C" WHERE "entity_id" = \'teplota_obyvak_temperature\' AND {} GROUP BY time(1d) fill(none)'.format(period)))

        # Target temp daily
        target_daily = self._series_values(self._influx_query(
            'SELECT mean("temperature") FROM "climate.topeni" WHERE {} GROUP BY time(1d) fill(none)'.format(period)))

        # 3W valve (for bojler separation)
        valve = self._series_values(self._influx_query(
            'SELECT last("value") FROM "switch.tepelnecerpadlo_3w_teplavoda" WHERE {} GROUP BY time(1h) fill(none)'.format(hourly_period)))

        # Build hourly dicts
        pwr_h = {v[0]: v[1] for v in tc_power}
        ind_h = {v[0]: v[1] for v in indoor}
        out_h = {v[0]: v[1] for v in outdoor_hourly}
        valve_h = {}
        last_v = 0
        all_ts = sorted(set(pwr_h.keys()) & set(ind_h.keys()))
        for ts in all_ts:
            vv = next((v[1] for v in valve if v[0] == ts), None)
            if vv is not None:
                last_v = vv
            valve_h[ts] = last_v

        # Fireplace detection
        fireplace_hours = set()
        ts_list = sorted(ind_h.keys())
        for i in range(1, len(ts_list)):
            ts, ts_prev = ts_list[i], ts_list[i - 1]
            if ts in ind_h and ts_prev in ind_h and ts in pwr_h:
                delta = ind_h[ts] - ind_h[ts_prev]
                if delta > 0.5 and pwr_h[ts] < 100:
                    fireplace_hours.add(ts)

        from collections import defaultdict
        day_fire = defaultdict(int)
        for ts in fireplace_hours:
            day_fire[ts[:10]] += 1
        krb_days = [d for d, cnt in day_fire.items() if cnt >= 2]

        # Energy separation (topeni vs bojler) per day
        daily_topeni = defaultdict(float)
        daily_total = defaultdict(float)
        for ts in all_ts:
            day = ts[:10]
            pwr = pwr_h.get(ts, 0)
            kwh = pwr / 1000
            daily_total[day] += kwh
            if valve_h.get(ts, 0) < 0.5:
                daily_topeni[day] += kwh

        # TC runtime from history_stats (if available)
        runtime_vals = self._series_values(self._influx_query(
            'SELECT max("value") FROM "h" WHERE "entity_id" = \'tepelne_cerpadlo_cas_v_provozu\' AND {} GROUP BY time(1d) fill(none)'.format(period)))

        # TC start count
        starts_vals = self._series_values(self._influx_query(
            'SELECT max("value") FROM "x" WHERE "entity_id" = \'heatpump_start_count\' AND {} GROUP BY time(1d) fill(none)'.format(period)))

        # Build daily summary
        days = sorted(set(
            [v[0][:10] for v in outdoor_daily] +
            list(daily_total.keys())))

        daily_data = []
        for day in days:
            od = next((v[1] for v in outdoor_daily if v[0][:10] == day), None)
            id_ = next((v[1] for v in indoor_daily if v[0][:10] == day), None)
            tgt = next((v[1] for v in target_daily if v[0][:10] == day), None)
            rt = next((v[1] for v in runtime_vals if v[0][:10] == day), None)
            st = next((v[1] for v in starts_vals if v[0][:10] == day), None)
            daily_data.append({
                "date": day,
                "kwh_total": round(daily_total.get(day, 0), 1),
                "kwh_topeni": round(daily_topeni.get(day, 0), 1),
                "outdoor": round(od, 1) if od else None,
                "indoor": round(id_, 1) if id_ else None,
                "target": round(tgt, 1) if tgt else None,
                "runtime_h": round(rt, 1) if rt else None,
                "starts": int(st) if st else None,
                "krb": day in krb_days,
            })

        return daily_data, krb_days

    # ── Report generation ──────────────────────────────────────────

    def _generate_report(self):
        try:
            # Current week (7 days)
            week_data, krb_days = self._collect_week(7, 0)
            # Historical (previous 4 weeks)
            hist_data, _ = self._collect_week(35, 7)

            if not week_data:
                self.log("No data for weekly report", level="WARNING")
                return

            # Aggregate current week
            w = self._aggregate(week_data)
            # Aggregate history per week
            h = self._aggregate(hist_data)

            week_end = datetime.now().strftime("%d.%m.")
            week_start = (datetime.now() - timedelta(days=7)).strftime("%d.%m.")
            week_label = "Tyden {}/{}".format(
                datetime.now().isocalendar()[1], datetime.now().year)

            # Format data for prompt
            week_table = self._format_table(week_data)
            hist_summary = self._format_hist(h, len(hist_data))

            prompt = (
                "Jsi analytik vytapeni. Proved analyzu tydenního reportu "
                "vytapeni rodinneho domu v cestine.\n\n"
                "REFERENCNI DATA DOMU:\n"
                "- Merna tepelna ztrata: {} W/C\n"
                "- Tepelna kapacita: {:.1f} kWh/C\n"
                "- Nastaveni: den {:.1f}C, noc {:.1f}C\n"
                "- Cena elektriny: {:.2f} Kc/kWh\n\n"
                "DATA TOHOTO TYDNE ({} - {}):\n{}\n\n"
                "Souhrn tydne: {:.1f} kWh topeni, {:.0f} Kc, "
                "prumer venkovni {:.1f}C, vnitrni {:.1f}C\n"
                "Krbove dny: {}\n\n"
                "HISTORICKE PRUMERY (predchozi 4 tydny):\n{}\n\n"
                "Napis strucnou analyzu (max 300 slov):\n"
                "1. Shrnuti tydne - spotreba, naklady, efektivita\n"
                "2. Srovnani s historii - lepsi/horsi a proc\n"
                "3. Vliv venkovni teploty na spotrebu\n"
                "4. Pocet sepnuti TC - zhodnot zivotnost kompresoru "
                "(ideal < 6x/den, varovani > 10x/den)\n"
                "5. Doporuceni pro pristi tyden\n\n"
                "Pis konkretne s cisly. Vyhni se obecnym frazim. "
                "Format: odstavce, zadne markdown nadpisy."
            ).format(
                HEAT_LOSS_COEFF, THERMAL_CAPACITY / 1000,
                self._day_target, self._night_target, self._kwh_price,
                week_start, week_end, week_table,
                w["kwh_topeni"], w["kwh_topeni"] * self._kwh_price,
                w["avg_outdoor"] or 0, w["avg_indoor"] or 0,
                ", ".join(krb_days) if krb_days else "zadne",
                hist_summary)

            # Call Haiku
            report_text = self._call_haiku(prompt)
            if not report_text:
                report_text = "Chyba pri generovani reportu."

            total_cost = w["kwh_topeni"] * self._kwh_price

            # Build report data
            report_data = {
                "week_label": week_label,
                "report": report_text,
                "generated_at": datetime.now().isoformat(),
                "week_start": week_start,
                "week_end": week_end,
                "total_kwh": round(w["kwh_topeni"], 1),
                "total_cost_czk": round(total_cost, 0),
                "avg_outdoor_temp": w["avg_outdoor"],
                "avg_indoor_temp": w["avg_indoor"],
                "krb_days": len(krb_days),
                "avg_daily_starts": w["avg_starts"],
                "avg_daily_runtime_h": w["avg_runtime"],
                "daily_data": week_data,
            }

            # Save to persistent file
            self._save_report(week_label, report_data)

            # Update HA sensor
            self._set_report_sensor(report_data)

            # Update week selector
            self._sync_week_selector(week_label)

            # Push notification
            svc = self._notify.replace("notify.", "notify/")
            self.call_service(svc,
                              title="Tydenni report vytapeni",
                              message="{}: {:.1f} kWh ({:.0f} Kc). Zprava v HA.".format(
                                  week_label, w["kwh_topeni"], total_cost))

            # Save to InfluxDB
            self._write_influx(w, total_cost, len(krb_days))

            self.log("Weekly heating report generated: {:.1f} kWh, {:.0f} Kc".format(
                w["kwh_topeni"], total_cost))

        except Exception as e:
            self.log("Weekly report error: {}".format(e), level="ERROR")

    # ── Persistence ─────────────────────────────────────────────

    def _report_filename(self, week_label):
        safe = week_label.replace("/", "-").replace(" ", "_")
        return os.path.join(REPORTS_DIR, "{}.json".format(safe))

    def _save_report(self, week_label, report_data):
        path = self._report_filename(week_label)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)
        self.log("Report saved: {}".format(path))

    def _load_report(self, week_label):
        path = self._report_filename(week_label)
        if not os.path.exists(path):
            self.log("Report not found: {}".format(path), level="WARNING")
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._set_report_sensor(data)
        self.log("Loaded report: {}".format(week_label))

    def _load_latest_report(self):
        files = sorted(glob.glob(os.path.join(REPORTS_DIR, "*.json")))
        if not files:
            return
        with open(files[-1], "r", encoding="utf-8") as f:
            data = json.load(f)
        self._set_report_sensor(data)

    def _set_report_sensor(self, data):
        self.set_state("sensor.heating_weekly_report",
                       state=data.get("week_label", "?"),
                       attributes={
                           "report": data.get("report", ""),
                           "generated_at": data.get("generated_at", ""),
                           "week_start": data.get("week_start", ""),
                           "week_end": data.get("week_end", ""),
                           "total_kwh": data.get("total_kwh"),
                           "total_cost_czk": data.get("total_cost_czk"),
                           "avg_outdoor_temp": data.get("avg_outdoor_temp"),
                           "avg_indoor_temp": data.get("avg_indoor_temp"),
                           "krb_days": data.get("krb_days"),
                           "avg_daily_starts": data.get("avg_daily_starts"),
                           "avg_daily_runtime_h": data.get("avg_daily_runtime_h"),
                           "friendly_name": "Heating Weekly Report",
                           "icon": "mdi:chart-bar",
                       })

    def _get_available_weeks(self):
        files = sorted(glob.glob(os.path.join(REPORTS_DIR, "*.json")))
        weeks = []
        for f in files:
            name = os.path.splitext(os.path.basename(f))[0]
            label = name.replace("_", " ").replace("-", "/")
            weeks.append(label)
        return weeks

    def _sync_week_selector(self, current=None):
        weeks = self._get_available_weeks()
        if not weeks:
            weeks = ["(zadne reporty)"]
        try:
            self.call_service("input_select/set_options",
                              entity_id="input_select.heating_report_week",
                              options=weeks)
            if current and current in weeks:
                self.call_service("input_select/select_option",
                                  entity_id="input_select.heating_report_week",
                                  option=current)
            elif weeks:
                self.call_service("input_select/select_option",
                                  entity_id="input_select.heating_report_week",
                                  option=weeks[-1])
        except Exception as e:
            self.log("Sync week selector: {}".format(e), level="WARNING")

    # ── Aggregation ──────────────────────────────────────────────

    def _aggregate(self, data):
        """Aggregate daily data into summary."""
        if not data:
            return {"kwh_topeni": 0, "kwh_total": 0, "avg_outdoor": None,
                    "avg_indoor": None, "avg_starts": None, "avg_runtime": None}
        kwh_t = sum(d["kwh_topeni"] for d in data)
        kwh_tot = sum(d["kwh_total"] for d in data)
        outs = [d["outdoor"] for d in data if d["outdoor"] is not None]
        ins = [d["indoor"] for d in data if d["indoor"] is not None]
        starts = [d["starts"] for d in data if d["starts"] is not None]
        runtimes = [d["runtime_h"] for d in data if d["runtime_h"] is not None]
        return {
            "kwh_topeni": kwh_t,
            "kwh_total": kwh_tot,
            "avg_outdoor": round(sum(outs) / len(outs), 1) if outs else None,
            "avg_indoor": round(sum(ins) / len(ins), 1) if ins else None,
            "avg_starts": round(sum(starts) / len(starts), 1) if starts else None,
            "avg_runtime": round(sum(runtimes) / len(runtimes), 1) if runtimes else None,
        }

    def _format_table(self, data):
        lines = ["Den        kWh_top  kWh_cel  Ven   Vnitr  Cil   Beh_h  Sep  Krb"]
        for d in data:
            lines.append("{} {:>6.1f}  {:>6.1f}  {:>+5.1f}  {:>5.1f}  {:>4.1f}  {:>5}  {:>3}  {}".format(
                d["date"],
                d["kwh_topeni"], d["kwh_total"],
                d["outdoor"] if d["outdoor"] is not None else 0,
                d["indoor"] if d["indoor"] is not None else 0,
                d["target"] if d["target"] is not None else 0,
                "{:.1f}".format(d["runtime_h"]) if d["runtime_h"] is not None else "-",
                str(d["starts"]) if d["starts"] is not None else "-",
                "ANO" if d["krb"] else ""))
        return "\n".join(lines)

    def _format_hist(self, h, num_days):
        weeks = max(1, num_days // 7)
        return ("Prumer za {} tydnu: {:.1f} kWh/tyden topeni, "
                "venkovni {}, vnitrni {}, sepnuti/den {}, beh/den {}h").format(
            weeks,
            h["kwh_topeni"] / weeks if weeks else 0,
            "{}C".format(h["avg_outdoor"]) if h["avg_outdoor"] else "?",
            "{}C".format(h["avg_indoor"]) if h["avg_indoor"] else "?",
            h["avg_starts"] if h["avg_starts"] else "?",
            h["avg_runtime"] if h["avg_runtime"] else "?")

    # ── Haiku API call ─────────────────────────────────────────────

    def _call_haiku(self, prompt):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 600,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30)
            data = r.json()
            if "content" in data and data["content"]:
                return data["content"][0].get("text", "")
            self.log("Haiku response: {}".format(data), level="WARNING")
            return None
        except Exception as e:
            self.log("Haiku API error: {}".format(e), level="ERROR")
            return None

    # ── InfluxDB write ─────────────────────────────────────────────

    def _write_influx(self, w, cost, krb_count):
        try:
            fields = []
            fields.append("total_kwh={:.1f}".format(w["kwh_topeni"]))
            fields.append("total_cost={:.0f}".format(cost))
            if w["avg_outdoor"] is not None:
                fields.append("avg_outdoor={:.1f}".format(w["avg_outdoor"]))
            if w["avg_indoor"] is not None:
                fields.append("avg_indoor={:.1f}".format(w["avg_indoor"]))
            if w["avg_starts"] is not None:
                fields.append("avg_starts={:.1f}".format(w["avg_starts"]))
            if w["avg_runtime"] is not None:
                fields.append("avg_runtime={:.1f}".format(w["avg_runtime"]))
            fields.append("krb_days={}i".format(krb_count))

            line = "heating_weekly_report {}".format(",".join(fields))
            requests.post(
                "http://{}:{}/write".format(self._influx_host, self._influx_port),
                params={"db": self._influx_db},
                auth=self._influx_auth,
                data=line, timeout=5)
        except Exception as e:
            self.log("InfluxDB write error: {}".format(e), level="WARNING")
