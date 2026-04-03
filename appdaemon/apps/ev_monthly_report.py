import appdaemon.plugins.hass.hassapi as hass
import requests
import json
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta


class EVMonthlyReport(hass.Hass):

    def initialize(self):
        self._influx_host = self.args.get("influxdb_host", "a0d7b954-influxdb")
        self._influx_port = self.args.get("influxdb_port", 8086)
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_auth = (
            self.args.get("influxdb_user", "db_write"),
            self.args.get("influxdb_password", "db_write_pass")
        )
        self._email_to = self.args.get("email_to", "kamil@hanusek.net")
        self._email_from = self.args.get("email_from", "ha@hanusek.net")
        self._smtp_host = self.args.get("smtp_host", "")
        self._smtp_port = self.args.get("smtp_port", 25)
        self._kwh_price = float(self.args.get("kwh_price_czk", 4.50))
        self._elroq_kwh = float(self.args.get("elroq_battery_kwh", 77.0))
        self._ford_kwh = float(self.args.get("ford_battery_kwh", 11.8))

        # Session tracking state
        self._elroq_session = None  # {"soc_start": float, "time_start": datetime}
        self._ford_last_energy = None  # last energytransferlogentry value
        self._ford_session = None  # {"soc_start": float, "time_start": datetime}

        # Listen for Elroq charger connection
        self.listen_state(self._on_elroq_charger,
                          "binary_sensor.skoda_elroq_charger_connected")

        # Listen for Ford plug state
        self.listen_state(self._on_ford_plug,
                          "sensor.fordpass_wf0fxxwpmhsc70607_elvehplug")

        # Monthly report: 1st day of month at 08:00
        self.run_daily(self._check_monthly_report,
                       datetime.now().replace(hour=8, minute=0, second=0))

        # Manual trigger via input_button
        self.listen_state(self._on_manual_report,
                          "input_button.ev_report_generate")

        # Store initial Ford energy value
        try:
            val = self.get_state("sensor.fordpass_wf0fxxwpmhsc70607_energytransferlogentry")
            if val and val != "unavailable":
                self._ford_last_energy = float(val)
        except Exception:
            pass

        self.log("EVMonthlyReport initialized (elroq={}kWh, ford={}kWh, price={} CZK/kWh)".format(
            self._elroq_kwh, self._ford_kwh, self._kwh_price))

    # ── ELROQ SESSION TRACKING ─────────────────────────────

    def _on_elroq_charger(self, entity, attribute, old, new, kwargs):
        if new == "on" and old != "on":
            # Charger connected — start session
            soc = self._get_float("sensor.skoda_elroq_battery_percentage")
            self._elroq_session = {
                "soc_start": soc,
                "time_start": datetime.now()
            }
            self.log("Elroq charging started: SOC={}%".format(soc))

        elif new == "off" and old == "on" and self._elroq_session:
            # Charger disconnected — end session
            soc_end = self._get_float("sensor.skoda_elroq_battery_percentage")
            soc_start = self._elroq_session["soc_start"]
            time_start = self._elroq_session["time_start"]
            duration_min = (datetime.now() - time_start).total_seconds() / 60.0

            if soc_end > soc_start:
                kwh = (soc_end - soc_start) / 100.0 * self._elroq_kwh
            else:
                kwh = 0.0

            self._save_session("elroq", kwh, soc_start, soc_end, duration_min)
            self.log("Elroq session: {}% -> {}% = {:.1f} kWh ({:.0f} min)".format(
                soc_start, soc_end, kwh, duration_min))

            self._elroq_session = None

    # ── FORD SESSION TRACKING ──────────────────────────────

    def _on_ford_plug(self, entity, attribute, old, new, kwargs):
        if new == "CONNECTED" and old != "CONNECTED":
            # Skip if wallbox is active — ev_charging_manager handles it
            wallbox = self.get_state("sensor.ev_charger_stav") or ""
            if wallbox in ("Pripojeno", "Ceka", "Nabiji"):
                self.log("Ford plug connected but wallbox active — "
                         "ev_charging_manager handles this session")
                return

            # Plug connected — start session
            soc = self._get_float("sensor.fordpass_wf0fxxwpmhsc70607_soc")
            self._ford_session = {
                "soc_start": soc,
                "time_start": datetime.now()
            }
            self.log("Ford charging started: SOC={}%".format(soc))

        elif new == "DISCONNECTED" and old == "CONNECTED" and self._ford_session:
            # Skip if wallbox just went Volny (ev_charging_manager already logged it)
            wallbox = self.get_state("sensor.ev_charger_stav") or ""
            if wallbox == "Volny" and self._ford_session.get("wallbox_skip"):
                self._ford_session = None
                return
            # Plug disconnected — end session
            soc_end = self._get_float("sensor.fordpass_wf0fxxwpmhsc70607_soc")
            soc_start = self._ford_session["soc_start"]
            time_start = self._ford_session["time_start"]
            duration_min = (datetime.now() - time_start).total_seconds() / 60.0

            # Try energytransferlogentry for actual kWh
            energy_now = self._get_float(
                "sensor.fordpass_wf0fxxwpmhsc70607_energytransferlogentry")
            if self._ford_last_energy is not None and energy_now > self._ford_last_energy:
                kwh = energy_now - self._ford_last_energy
            elif soc_end > soc_start:
                kwh = (soc_end - soc_start) / 100.0 * self._ford_kwh
            else:
                kwh = 0.0

            self._ford_last_energy = energy_now
            self._save_session("ford", kwh, soc_start, soc_end, duration_min)
            self.log("Ford session: {}% -> {}% = {:.1f} kWh ({:.0f} min)".format(
                soc_start, soc_end, kwh, duration_min))

            self._ford_session = None

        # Track energy value changes even without session
        if new == "DISCONNECTED":
            try:
                val = self._get_float(
                    "sensor.fordpass_wf0fxxwpmhsc70607_energytransferlogentry")
                if val is not None:
                    self._ford_last_energy = val
            except Exception:
                pass

    # ── INFLUXDB ───────────────────────────────────────────

    def _save_session(self, auto, kwh, soc_start, soc_end, duration_min):
        try:
            line = ('ev_charging_sessions,'
                    'auto={} '
                    'kwh={:.2f},'
                    'soc_start={:.1f},'
                    'soc_end={:.1f},'
                    'duration_min={:.1f}').format(
                auto, kwh, soc_start, soc_end, duration_min)
            requests.post(
                "http://{}:{}/write".format(self._influx_host, self._influx_port),
                params={"db": self._influx_db},
                data=line.encode(),
                auth=self._influx_auth,
                timeout=5
            )
            self.log("Saved session to InfluxDB: {} {:.1f} kWh".format(auto, kwh))
        except Exception as e:
            self.log("InfluxDB write error: {}".format(e), level="ERROR")

    def _query_influx(self, query):
        try:
            resp = requests.get(
                "http://{}:{}/query".format(self._influx_host, self._influx_port),
                params={"db": self._influx_db, "q": query},
                auth=self._influx_auth,
                timeout=10
            )
            data = resp.json()
            results = data.get("results", [{}])[0]
            series = results.get("series", [])
            return series
        except Exception as e:
            self.log("InfluxDB query error: {}".format(e), level="ERROR")
            return []

    # ── MONTHLY REPORT ─────────────────────────────────────

    def _check_monthly_report(self, kwargs):
        now = datetime.now()
        if now.day == 1:
            self.log("First day of month — generating report")
            self._generate_monthly_report()

    def _generate_monthly_report(self, target_month=None):
        """Generate and send monthly report. Can be called manually."""
        now = datetime.now()

        if target_month:
            # target_month = "2026-03" format
            year, month = map(int, target_month.split("-"))
        else:
            # Previous month
            first_of_this = now.replace(day=1)
            last_of_prev = first_of_this - timedelta(days=1)
            year = last_of_prev.year
            month = last_of_prev.month

        # Date range for InfluxDB query
        start = "{}-{:02d}-01T00:00:00Z".format(year, month)
        if month == 12:
            end = "{}-01-01T00:00:00Z".format(year + 1)
        else:
            end = "{}-{:02d}-01T00:00:00Z".format(year, month + 1)

        MONTHS_CZ = ["", "leden", "unor", "brezen", "duben", "kveten", "cerven",
                      "cervenec", "srpen", "zari", "rijen", "listopad", "prosinec"]
        month_name = MONTHS_CZ[month]

        # Query Elroq sessions
        elroq_q = ("SELECT count(kwh), sum(kwh), mean(kwh) "
                    "FROM ev_charging_sessions "
                    "WHERE auto='elroq' AND time >= '{}' AND time < '{}'").format(start, end)
        elroq_data = self._query_influx(elroq_q)

        elroq_count = 0
        elroq_total = 0.0
        elroq_avg = 0.0
        if elroq_data:
            vals = elroq_data[0].get("values", [[]])[0]
            # [time, count, sum, mean]
            if len(vals) >= 4:
                elroq_count = int(vals[1] or 0)
                elroq_total = float(vals[2] or 0)
                elroq_avg = float(vals[3] or 0)

        # Query Ford sessions
        ford_q = ("SELECT count(kwh), sum(kwh), mean(kwh) "
                  "FROM ev_charging_sessions "
                  "WHERE auto='ford' AND time >= '{}' AND time < '{}'").format(start, end)
        ford_data = self._query_influx(ford_q)

        ford_count = 0
        ford_total = 0.0
        if ford_data:
            vals = ford_data[0].get("values", [[]])[0]
            if len(vals) >= 3:
                ford_count = int(vals[1] or 0)
                ford_total = float(vals[2] or 0)

        total_kwh = elroq_total + ford_total
        cost_czk = total_kwh * self._kwh_price

        # Build email body
        body = """===========================
EV NABIJENI - {} {}
===========================

Skoda Elroq:
  Sessions:     {}x
  Celkem:       {:.1f} kWh
  Prumer:       {:.1f} kWh/session

Ford PHEV:
  Sessions:     {}x
  Celkem:       {:.1f} kWh

CELKEM:         {:.1f} kWh
Odhadovane naklady: {:.0f} Kc (@ {:.2f} Kc/kWh)

===========================
Generovano: {}
Home Assistant - ha.hanusek.net
===========================""".format(
            month_name.upper(), year,
            elroq_count, elroq_total, elroq_avg,
            ford_count, ford_total,
            total_kwh, cost_czk, self._kwh_price,
            now.strftime("%d.%m.%Y %H:%M")
        )

        subject = "EV nabijeni [{} {}] - souhrn".format(
            month_name.capitalize(), year)

        self.log("Monthly report: Elroq {}x/{:.1f}kWh, Ford {}x/{:.1f}kWh, total {:.1f}kWh".format(
            elroq_count, elroq_total, ford_count, ford_total, total_kwh))

        # Send email (if SMTP configured)
        if self._smtp_host:
            self._send_email(subject, body)
        else:
            self.log("SMTP not configured, skipping email")

        # Also push notification
        try:
            self.call_service("notify/mobile_app_iphone_17",
                              title="EV Report - {} {}".format(month_name, year),
                              message="{:.1f} kWh celkem ({:.0f} Kc)".format(
                                  total_kwh, cost_czk))
        except Exception as e:
            self.log("Push notify error: {}".format(e))

    def _send_email(self, subject, body):
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = self._email_from
            msg["To"] = self._email_to

            smtp = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30)
            smtp.sendmail(self._email_from, [self._email_to], msg.as_string())
            smtp.quit()
            self.log("Email sent to {}".format(self._email_to))
        except Exception as e:
            self.log("Email error: {}".format(e), level="ERROR")

    # ── MANUAL TRIGGER ─────────────────────────────────────

    def _on_manual_report(self, entity, attribute, old, new, kwargs):
        """Triggered by input_button.ev_report_generate press."""
        self.log("Manual report triggered")
        # Generate for current month (not previous)
        now = datetime.now()
        self._generate_monthly_report(
            target_month="{}-{:02d}".format(now.year, now.month))

    # ── HELPERS ────────────────────────────────────────────

    def _get_float(self, entity_id):
        try:
            val = self.get_state(entity_id)
            if val is None or val == "unavailable" or val == "unknown":
                return 0.0
            return float(val)
        except (ValueError, TypeError):
            return 0.0
