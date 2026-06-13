import appdaemon.plugins.hass.hassapi as hass
import requests
from datetime import datetime, timedelta

EV_BATTERY_KWH = 77.0
EV_SOC_PER_KM_DEFAULT = 0.20
MIN_SAMPLES_RELIABLE = 2

CHARGE_LIMIT_ENTITY = "number.skoda_elroq_charge_limit"
SOC_ENTITY = "sensor.skoda_elroq_battery_percentage"
MILEAGE_ENTITY = "sensor.skoda_elroq_mileage"
NOCHARGE_ENTITY = "input_boolean.ev_nocharge_tonight"
TARGET_SOC_ENTITY = "input_number.ev_target_soc_tomorrow"

DAYS_CZ = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]


class EnergyPlanner(hass.Hass):

    def initialize(self):
        self._ha_url = self.args.get("ha_url", "http://10.0.10.67:8123")
        self._ha_token = self.args.get("ha_token")

        if not self._ha_token:
            self.log("ERROR: ha_token not configured", level="ERROR")
            return

        self._influx_url = "http://{}:{}".format(
            self.args.get("influxdb_host", "a0d7b954-influxdb"),
            self.args.get("influxdb_port", 8086))
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_user = self.args.get("influxdb_user", "db_write")
        self._influx_pass = self.args.get("influxdb_password", "db_write_pass")
        self._influx_ok = False
        self._init_influxdb()

        self._ensure_helpers()

        self.run_daily(self._record_daily_km, datetime.now().replace(
            hour=23, minute=0, second=0, microsecond=0))
        self.run_daily(self._daily_plan, datetime.now().replace(
            hour=23, minute=5, second=0, microsecond=0))

        self.run_in(self._startup_plan, 15)

        self.log("EnergyPlanner initialized (influxdb={})".format(
            "OK" if self._influx_ok else "UNAVAILABLE"))

    # ── InfluxDB v1 helpers ────────────────────────────────────────────────

    def _init_influxdb(self):
        try:
            resp = requests.get("{}/ping".format(self._influx_url), timeout=5)
            if resp.status_code == 204:
                self._influx_ok = True
                self.log("InfluxDB connected: {}".format(self._influx_url))
            else:
                self.log("InfluxDB ping {}, disabled".format(resp.status_code),
                         level="WARNING")
        except Exception as e:
            self.log("InfluxDB failed: {}".format(e), level="WARNING")

    def _influx_write(self, line_protocol):
        if not self._influx_ok:
            return False
        try:
            resp = requests.post(
                "{}/write?db={}".format(self._influx_url, self._influx_db),
                auth=(self._influx_user, self._influx_pass),
                data=line_protocol.encode("utf-8"), timeout=10)
            return resp.status_code == 204
        except Exception as e:
            self.log("InfluxDB write failed: {}".format(e), level="WARNING")
        return False

    def _influx_query(self, query):
        if not self._influx_ok:
            return []
        try:
            resp = requests.get(
                "{}/query".format(self._influx_url),
                params={"db": self._influx_db, "q": query},
                auth=(self._influx_user, self._influx_pass), timeout=10)
            if resp.status_code == 200:
                return resp.json().get("results", [{}])[0].get("series", [])
        except Exception as e:
            self.log("InfluxDB query failed: {}".format(e), level="WARNING")
        return []

    # ── Helper entities ────────────────────────────────────────────────────

    def _ensure_helpers(self):
        if self.get_state(NOCHARGE_ENTITY) is None:
            self.set_state(NOCHARGE_ENTITY, state="off",
                           attributes={"friendly_name": "EV nenabíjet dnes v noci",
                                       "icon": "mdi:ev-station"})

        if self.get_state(TARGET_SOC_ENTITY) is None:
            self.set_state(TARGET_SOC_ENTITY, state="80",
                           attributes={"friendly_name": "EV cílový SOC",
                                       "icon": "mdi:battery-charging-80",
                                       "min": 20, "max": 100, "step": 5,
                                       "unit_of_measurement": "%"})

    # ── HA entity helpers ──────────────────────────────────────────────────

    def _float(self, entity_id, default=0.0):
        try:
            v = self.get_state(entity_id)
            return float(v) if v not in (None, "unavailable", "unknown") else default
        except (ValueError, TypeError):
            return default

    def _float_influx_last(self, measurement, entity_id, hours_back=48):
        try:
            q = ('SELECT LAST(value) FROM "{}" '
                 "WHERE entity_id = '{}' AND time > now() - {}h".format(
                     measurement, entity_id, hours_back))
            series = self._influx_query(q)
            for s in series:
                for row in s.get("values", []):
                    if row[1] is not None:
                        return float(row[1])
        except Exception as e:
            self.log("InfluxDB last {} failed: {}".format(entity_id, e), level="WARNING")
        return None

    def _set_ha_input(self, entity_id, value):
        try:
            domain = entity_id.split(".")[0]
            if domain == "input_boolean":
                svc = "input_boolean/turn_on" if value else "input_boolean/turn_off"
                self.call_service(svc, entity_id=entity_id)
            elif domain == "input_number":
                self.call_service("input_number/set_value",
                                  entity_id=entity_id, value=float(value))
        except Exception as e:
            self.log("Failed to set {}: {}".format(entity_id, e), level="WARNING")

    # ══════════════════════════════════════════════════════════════════════
    # Daily km recording (23:00)
    # ══════════════════════════════════════════════════════════════════════

    def _record_daily_km(self, kwargs):
        try:
            self._do_record_km()
        except Exception as e:
            self.log("Record km error: {}".format(e), level="ERROR")

    def _do_record_km(self):
        current_mileage = self._float(MILEAGE_ENTITY)
        if current_mileage <= 0:
            current_mileage = self._float_influx_last("km", "skoda_elroq_mileage", 24)
        if not current_mileage or current_mileage <= 0:
            self.log("Cannot read mileage, skipping km recording", level="WARNING")
            return

        prev_mileage = None
        series = self._influx_query(
            "SELECT LAST(mileage_end) FROM ev_daily_km WHERE time > now() - 3d")
        for s in series:
            for row in s.get("values", []):
                if row[1] is not None:
                    prev_mileage = float(row[1])

        if prev_mileage is None:
            self.log("First km recording, storing baseline mileage={}".format(
                int(current_mileage)))
            delta = 0
        else:
            delta = current_mileage - prev_mileage

        if delta < 0 or delta > 800:
            self.log("Anomaly: delta={}km (prev={}, curr={}), storing 0".format(
                delta, prev_mileage, current_mileage), level="WARNING")
            delta = 0

        today = datetime.now()
        dow = today.weekday()
        date_str = today.strftime("%Y-%m-%d")

        line = ('ev_daily_km,day_of_week={dow} '
                'km_driven={km},mileage_start={ms},mileage_end={me},'
                'date="{date}"'.format(
                    dow=dow, km=round(delta, 1),
                    ms=round(prev_mileage or current_mileage, 1),
                    me=round(current_mileage, 1),
                    date=date_str))

        if self._influx_write(line):
            self.log("Recorded: {}km driven today (dow={}, mileage={})".format(
                round(delta, 1), dow, int(current_mileage)))
        else:
            self.log("Failed to write daily km to InfluxDB", level="WARNING")

    # ══════════════════════════════════════════════════════════════════════
    # Charging plan (23:05 + startup)
    # ══════════════════════════════════════════════════════════════════════

    def _startup_plan(self, kwargs):
        try:
            self._do_plan(startup=True)
        except Exception as e:
            self.log("Startup plan error: {}".format(e), level="ERROR")

    def _daily_plan(self, kwargs):
        try:
            self._do_plan(startup=False)
        except Exception as e:
            self.log("Daily plan error: {}".format(e), level="ERROR")

    def _do_plan(self, startup=False):
        ev_soc = self._float(SOC_ENTITY)
        if ev_soc <= 0:
            val = self._float_influx_last("%", "skoda_elroq_battery_percentage", 24)
            ev_soc = val if val else 0

        target = self._float(CHARGE_LIMIT_ENTITY, default=80)
        target = max(20, min(100, int(target)))

        if ev_soc < target:
            nocharge = False
            decision = "Nabíjet na {}%".format(target)
            reason = "SOC {}% je pod limitem {}% z auta.".format(int(ev_soc), target)
        else:
            nocharge = True
            decision = "Nenabíjet — SOC dostatečný"
            reason = "SOC {}% je na/nad limitem {}% z auta.".format(int(ev_soc), target)

        self._set_ha_input(NOCHARGE_ENTITY, nocharge)
        self._set_ha_input(TARGET_SOC_ENTITY, target)

        self._update_recommendation(
            state=decision,
            ev_soc_now=round(ev_soc, 1),
            target_soc=target,
            charge_limit_source=CHARGE_LIMIT_ENTITY,
            nocharge_tonight=nocharge,
            decision=decision,
            reason=reason)

        prefix = "Startup" if startup else "Daily"
        self.log("{} plan: {} | SOC={}% | target={}% nocharge={}".format(
            prefix, decision, int(ev_soc), target, nocharge))

    def _update_recommendation(self, state, **attrs):
        attrs["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        attrs["friendly_name"] = "Energy Planner doporučení"
        attrs["icon"] = "mdi:car-electric-outline"
        self.set_state("sensor.energy_planner_recommendation",
                       state=state, attributes=attrs)
