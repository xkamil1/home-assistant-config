import appdaemon.plugins.hass.hassapi as hass
import tinytuya
import requests
from datetime import datetime

CLIENT_ID = "etmsgcxsy55u44hwuv87"
CLIENT_SECRET = "d9c4e5e4fea6403bb11ba0de4cc704e8"
DEVICE_ID = "bf48bed640e8dcec8b7k2k"
POLL_SECS = 30
MIN_AMPS = 6
MAX_AMPS = 16

INFLUX_URL = "http://a0d7b954-influxdb:8086"
INFLUX_DB = "homeassistant"
INFLUX_AUTH = ("db_write", "db_write_pass")

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
        self._cloud = tinytuya.Cloud(
            apiRegion="eu",
            apiKey=CLIENT_ID,
            apiSecret=CLIENT_SECRET,
            apiDeviceID=DEVICE_ID
        )
        self._init_sensors()
        # Catch HA service calls for the switch entity
        self.listen_event(self.on_switch_service, "call_service")
        self.listen_state(self.on_current_change, "input_number.ev_charger_proud")
        self.run_every(self.poll, "now", POLL_SECS)
        self.log("EV Charger (cloud) started")

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
            result = self._sendcmd([{"code": "switch", "value": desired}])
            if result.get("success"):
                self.set_state("switch.ev_charger_switch",
                               state="on" if desired else "off",
                               attributes={"friendly_name": "EV Charger Switch"})
                self.log("SET switch {} via cloud OK".format("ON" if desired else "OFF"))
            else:
                self.log("SET switch {} FAILED: {}".format("ON" if desired else "OFF", result), level="WARNING")
            self.run_in(self.poll, 3)
        except Exception as e:
            self.log("on_switch_service error: {}".format(e), level="ERROR")

    def on_current_change(self, entity, attribute, old, new, kwargs):
        if new == old:
            return
        try:
            amps = max(MIN_AMPS, min(MAX_AMPS, int(float(new))))
            result = self._sendcmd([{"code": "charge_cur_set", "value": amps}])
            if result.get("success"):
                self.log("SET current {}A via cloud OK".format(amps))
            else:
                self.log("SET current {}A FAILED: {}".format(amps, result), level="WARNING")
            self.run_in(self.poll, 2)
        except Exception as e:
            self.log("on_current_change error: {}".format(e), level="ERROR")

    # ── Polling ──────────────────────────────────────────────────────────────

    def poll(self, kwargs):
        try:
            resp = self._cloud.getstatus(DEVICE_ID)
            if not resp.get("success"):
                self.log("Cloud error: {}".format(resp), level="WARNING")
                return
            dps = {item["code"]: item["value"] for item in resp.get("result", [])}
            self._update_sensors(dps)
        except Exception as e:
            self.log("Poll exception: {}".format(e), level="WARNING")

    def _sendcmd(self, commands):
        """Send commands using correct Tuya API format: {\"commands\": [...]}."""
        return self._cloud._tuyaplatform(
            "iot-03/devices/{}/commands".format(DEVICE_ID),
            action="POST",
            post={"commands": commands},
        )

    # ── Sensors ──────────────────────────────────────────────────────────────

    def _init_sensors(self):
        try:
            resp = self._cloud.getstatus(DEVICE_ID)
            if resp.get("success"):
                dps = {item["code"]: item["value"] for item in resp.get("result", [])}
                self._update_sensors(dps)
                self.log("Sensors initialized from Tuya cloud")
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
