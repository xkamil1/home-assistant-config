import appdaemon.plugins.hass.hassapi as hass
import tinytuya
import requests
import threading
import yaml

# ── Wallbox AC3-11k-22K, lokalni Tuya protokol 3.5 (bez cloudu) ─────────────
# Local key je v /homeassistant/secrets.yaml (tuya_wallbox_local_key).
# IP a device_id lze prepsat v apps.yaml (args: ip, device_id).
DEFAULT_DEVICE_ID = "bf48bed640e8dcec8b7k2k"
DEFAULT_IP = "10.0.30.115"
PROTOCOL_VERSION = 3.5
SECRETS_FILE = "/homeassistant/secrets.yaml"
SECRET_KEY_NAME = "tuya_wallbox_local_key"

POLL_SECS = 30
SOCKET_TIMEOUT = 6
MIN_AMPS = 6
MAX_AMPS = 16
FAILS_BEFORE_UNAVAILABLE = 6   # 6 x 30 s = 3 min bez odpovedi

INFLUX_URL = "http://a0d7b954-influxdb:8086"
INFLUX_DB = "homeassistant"
INFLUX_AUTH = ("db_write", "db_write_pass")

# DP id -> kod (z Tuya thing modelu, 2026-10-12)
DP_CODES = {
    "3":  "work_state",
    "4":  "charge_cur_set",
    "5":  "sigle_phase_power",
    "9":  "power_total",
    "14": "work_mode",
    "18": "switch",
    "24": "temp_current",
    "25": "charge_energy_once",
}
DP_SWITCH = 18
DP_CURRENT = 4

WORK_STATE_MAP = {
    "charger_free":       "Volny",
    "charger_insert":     "Pripojeno",
    "charger_free_fault": "Chyba",
    "charger_wait":       "Ceka",
    "charger_charging":   "Nabiji",
    "charger_pause":      "Pauza",
    "charger_end":        "Dokonceno",
    "charger_fault":      "Porucha",
}


class EVCharger(hass.Hass):

    def initialize(self):
        self._device_id = self.args.get("device_id", DEFAULT_DEVICE_ID)
        self._ip = self.args.get("ip", DEFAULT_IP)
        self._lock = threading.Lock()
        self._fail_count = 0
        self._last_dps = {}

        key = self._load_local_key()
        if not key:
            self.log("Local key '{}' nenalezen v {} - app nebezi".format(
                SECRET_KEY_NAME, SECRETS_FILE), level="ERROR")
            return

        self._dev = tinytuya.Device(self._device_id, self._ip, key, version=PROTOCOL_VERSION)
        self._dev.set_socketTimeout(SOCKET_TIMEOUT)

        self._init_sensors()
        # Catch HA service calls for the (virtual) switch entity
        self.listen_event(self.on_switch_service, "call_service")
        self.listen_state(self.on_current_change, "input_number.ev_charger_proud")
        self.run_every(self.poll, "now", POLL_SECS)
        self.log("EV Charger (local tinytuya {} / v{}) started, {}".format(
            tinytuya.__version__, PROTOCOL_VERSION, self._ip))

    def _load_local_key(self):
        try:
            with open(SECRETS_FILE) as f:
                return yaml.safe_load(f).get(SECRET_KEY_NAME)
        except Exception as e:
            self.log("secrets.yaml read error: {}".format(e), level="ERROR")
            return None

    # ── Device I/O ───────────────────────────────────────────────────────────

    def _status(self):
        """Vrati dict code->value nebo None pri chybe."""
        with self._lock:
            st = self._dev.status()
        if not isinstance(st, dict) or "dps" not in st:
            raise RuntimeError(st.get("Error", st) if isinstance(st, dict) else st)
        return {DP_CODES[k]: v for k, v in st["dps"].items() if k in DP_CODES}

    def _set_dp(self, dp, value):
        with self._lock:
            res = self._dev.set_value(dp, value)
        if isinstance(res, dict) and res.get("Error"):
            return False, res
        return True, res

    # ── Controls ─────────────────────────────────────────────────────────────

    def on_switch_service(self, event_name, data, kwargs):
        """Intercept switch.turn_on / switch.turn_off calls for ev_charger_switch."""
        if data.get("domain") != "switch":
            return
        service = data.get("service", "")
        if service not in ("turn_on", "turn_off"):
            return
        entity_id = data.get("service_data", {}).get("entity_id", "")
        if "ev_charger_switch" not in str(entity_id):
            return
        desired = (service == "turn_on")
        try:
            ok, res = self._set_dp(DP_SWITCH, desired)
            if ok:
                self.set_state("switch.ev_charger_switch",
                               state="on" if desired else "off",
                               attributes={"friendly_name": "EV Charger Switch"})
                self.log("SET switch {} local OK".format("ON" if desired else "OFF"))
            else:
                self.log("SET switch {} FAILED: {}".format("ON" if desired else "OFF", res), level="WARNING")
            self.run_in(self.poll, 3)
        except Exception as e:
            self.log("on_switch_service error: {}".format(e), level="ERROR")

    def on_current_change(self, entity, attribute, old, new, kwargs):
        if new == old:
            return
        try:
            amps = max(MIN_AMPS, min(MAX_AMPS, int(float(new))))
            ok, res = self._set_dp(DP_CURRENT, amps)
            if ok:
                self.log("SET current {}A local OK".format(amps))
            else:
                self.log("SET current {}A FAILED: {}".format(amps, res), level="WARNING")
            self.run_in(self.poll, 2)
        except Exception as e:
            self.log("on_current_change error: {}".format(e), level="ERROR")

    # ── Polling ──────────────────────────────────────────────────────────────

    def poll(self, kwargs):
        try:
            dps = self._status()
        except Exception as e:
            self._fail_count += 1
            if self._fail_count in (1, FAILS_BEFORE_UNAVAILABLE) or self._fail_count % 20 == 0:
                self.log("Poll failed ({}x): {}".format(self._fail_count, e), level="WARNING")
            if self._fail_count == FAILS_BEFORE_UNAVAILABLE:
                self.set_state("sensor.ev_charger_stav", state="Nedostupny",
                               attributes={"friendly_name": "EV Charger stav", "raw": "unreachable"})
            return
        if self._fail_count >= FAILS_BEFORE_UNAVAILABLE:
            self.log("Wallbox znovu dostupny po {} neuspesnych pokusech".format(self._fail_count))
        self._fail_count = 0
        # DP5 (vykon faze) wallbox v klidu neposila -> doplnime z posledni hodnoty / 0
        merged = dict(self._last_dps)
        merged.update(dps)
        self._last_dps = merged
        self._update_sensors(merged)

    # ── Sensors ──────────────────────────────────────────────────────────────

    def _init_sensors(self):
        try:
            dps = self._status()
            self._last_dps = dps
            self._update_sensors(dps)
            self.log("Sensors initialized from wallbox (local)")
            return
        except Exception as e:
            self.log("Init poll failed ({}), using defaults".format(e), level="WARNING")
        self.set_state("sensor.ev_charger_stav", state="unknown",
            attributes={"friendly_name": "EV Charger stav"})
        self.set_state("sensor.ev_charger_vykon", state="0",
            attributes={"friendly_name": "EV Charger vykon",
                        "unit_of_measurement": "kW", "device_class": "power",
                        "state_class": "measurement"})
        self.set_state("sensor.ev_charger_proud_nastaven", state="0",
            attributes={"friendly_name": "EV Charger proud nastaven",
                        "unit_of_measurement": "A"})
        self.set_state("sensor.ev_charger_teplota", state="0",
            attributes={"friendly_name": "EV Charger teplota",
                        "unit_of_measurement": "°C", "device_class": "temperature",
                        "state_class": "measurement"})
        self.set_state("sensor.ev_charger_energie_seance", state="0",
            attributes={"friendly_name": "EV Charger energie seance",
                        "unit_of_measurement": "kWh", "device_class": "energy",
                        "state_class": "total_increasing"})
        self.set_state("sensor.ev_charger_phase_power", state="0",
            attributes={"friendly_name": "EV Charger vykon/faze",
                        "unit_of_measurement": "W", "device_class": "power",
                        "state_class": "measurement"})

    def _update_sensors(self, dps):
        work_state = dps.get("work_state", "")
        if work_state:
            self.set_state("sensor.ev_charger_stav",
                state=WORK_STATE_MAP.get(work_state, work_state),
                attributes={"friendly_name": "EV Charger stav", "raw": work_state})

        if "switch" in dps:
            self.set_state("switch.ev_charger_switch",
                state="on" if dps["switch"] else "off",
                attributes={"friendly_name": "EV Charger Switch"})

        if "power_total" in dps:
            kw = round(dps["power_total"] / 1000, 3) if work_state == "charger_charging" else 0.0
            self.set_state("sensor.ev_charger_vykon",
                state=str(kw),
                attributes={"friendly_name": "EV Charger vykon",
                            "unit_of_measurement": "kW", "device_class": "power",
                            "state_class": "measurement"})

        if "charge_cur_set" in dps:
            self.set_state("sensor.ev_charger_proud_nastaven",
                state=str(dps["charge_cur_set"]),
                attributes={"friendly_name": "EV Charger proud nastaven",
                            "unit_of_measurement": "A"})

        if "temp_current" in dps:
            self.set_state("sensor.ev_charger_teplota",
                state=str(dps["temp_current"]),
                attributes={"friendly_name": "EV Charger teplota",
                            "unit_of_measurement": "°C", "device_class": "temperature",
                            "state_class": "measurement"})

        if "charge_energy_once" in dps:
            kwh = round(dps["charge_energy_once"] / 100, 2)
            self.set_state("sensor.ev_charger_energie_seance",
                state=str(kwh),
                attributes={"friendly_name": "EV Charger energie seance",
                            "unit_of_measurement": "kWh", "device_class": "energy",
                            "state_class": "total_increasing"})

        # Per-phase power
        phase_w = 0
        if "sigle_phase_power" in dps:
            phase_w = int(dps["sigle_phase_power"]) if work_state == "charger_charging" else 0
            self.set_state("sensor.ev_charger_phase_power",
                state=str(phase_w),
                attributes={"friendly_name": "EV Charger vykon/faze",
                            "unit_of_measurement": "W", "device_class": "power",
                            "state_class": "measurement"})

        # Write to InfluxDB for history
        total_w = int(dps.get("power_total", 0)) if work_state == "charger_charging" else 0
        cur_set = int(dps.get("charge_cur_set", 0))
        temp = int(dps.get("temp_current", 0))
        energy = round(int(dps.get("charge_energy_once", 0)) / 100, 2)
        charging = 1 if work_state == "charger_charging" else 0

        line = ('ev_charger_data '
                'power_total_w={ptw}i,phase_power_w={ppw}i,'
                'current_set_a={cur}i,temperature_c={tmp}i,'
                'energy_session_kwh={eng},charging={chg}i,'
                'work_state="{ws}"'.format(
                    ptw=total_w, ppw=phase_w, cur=cur_set,
                    tmp=temp, eng=energy, chg=charging,
                    ws=work_state.replace('"', '\\"') if work_state else "unknown"))
        try:
            requests.post("{}/write?db={}".format(INFLUX_URL, INFLUX_DB),
                          auth=INFLUX_AUTH, data=line.encode("utf-8"), timeout=5)
        except Exception:
            pass
