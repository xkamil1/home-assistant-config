"""Cloud poller pro TC virivky (Pool Mini) — cte vsechny DPs vcetne skrytych.

Vzor: ev_charger.py (tinytuya.Cloud + set_state senzory + InfluxDB).
Rozdil: getstatus() vraci jen DPs z verejne specifikace, skryte DPs
(mode, fault, aux_manual) jsou dostupne jen pres shadow properties endpoint,
zapis na ne jen pres shadow properties issue.
"""

import appdaemon.plugins.hass.hassapi as hass
import tinytuya
import requests
import json


MODE_MAP = {
    "Heat": "Topi",
    "Cool": "Chladi",
    "Auto": "Auto",
}


class PoolTCPoller(hass.Hass):

    def initialize(self):
        self._device_id = self.args["tuya_device_id"]
        self._poll_secs = self.args.get("poll_interval", 60)
        self._influx_url = "http://{}:{}".format(
            self.args.get("influxdb_host", "a0d7b954-influxdb"),
            self.args.get("influxdb_port", 8086))
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_auth = (self.args.get("influxdb_user", "db_write"),
                             self.args.get("influxdb_password", "db_write_pass"))

        self._cloud = tinytuya.Cloud(
            apiRegion="eu",
            apiKey=self.args["tuya_client_id"],
            apiSecret=self.args["tuya_client_secret"],
            apiDeviceID=self._device_id,
        )

        # Ovladani: realne helpery (viz lessons - set_state entity neumi service cally)
        self.listen_state(self.on_temp_change, "input_number.tc_virivka_teplota_cil")
        self.listen_state(self.on_mode_change, "input_select.tc_virivka_rezim")
        self.listen_event(self.on_switch_service, "call_service")

        self.run_every(self.poll, "now", self._poll_secs)
        self.log("Pool TC poller started (device {}, every {}s)".format(
            self._device_id, self._poll_secs))

    # -- Ovladani -------------------------------------------------------------

    def on_switch_service(self, event_name, data, kwargs):
        """switch.turn_on/off na switch.tc_virivka_switch -> Tuya cloud."""
        if data.get("domain") != "switch":
            return
        service = data.get("service", "")
        if service not in ("turn_on", "turn_off"):
            return
        entity_id = str(data.get("service_data", {}).get("entity_id", ""))
        if "tc_virivka_switch" not in entity_id:
            return
        desired = (service == "turn_on")
        try:
            result = self._set_properties({"switch": desired})
            if result.get("success"):
                self.set_state("switch.tc_virivka_switch",
                               state="on" if desired else "off",
                               attributes={"friendly_name": "TC virivka"})
                self.log("SET switch {} OK".format("ON" if desired else "OFF"))
            else:
                self.log("SET switch {} FAILED: {}".format(
                    "ON" if desired else "OFF", result), level="WARNING")
            self.run_in(self.poll, 3)
        except Exception as e:
            self.log("on_switch_service error: {}".format(e), level="ERROR")

    def on_temp_change(self, entity, attribute, old, new, kwargs):
        if new == old or new in (None, "unavailable", "unknown"):
            return
        try:
            temp = max(0, min(40, int(float(new))))
            result = self._set_properties({"temp_set": temp})
            if result.get("success"):
                self.log("SET temp_set {} C OK".format(temp))
            else:
                self.log("SET temp_set {} FAILED: {}".format(temp, result),
                         level="WARNING")
            self.run_in(self.poll, 3)
        except Exception as e:
            self.log("on_temp_change error: {}".format(e), level="ERROR")

    def on_mode_change(self, entity, attribute, old, new, kwargs):
        if new == old or new not in ("Heat", "Cool", "Auto"):
            return
        try:
            result = self._set_properties({"mode": new})
            if result.get("success"):
                self.log("SET mode {} OK".format(new))
            else:
                self.log("SET mode {} FAILED: {}".format(new, result),
                         level="WARNING")
            self.run_in(self.poll, 3)
        except Exception as e:
            self.log("on_mode_change error: {}".format(e), level="ERROR")

    def _set_properties(self, props):
        """Zapis pres shadow properties issue — funguje i pro skryte DPs."""
        return self._cloud.cloudrequest(
            "/v2.0/cloud/thing/{}/shadow/properties/issue".format(self._device_id),
            action="POST",
            post={"properties": json.dumps(props)},
        )

    # -- Polling --------------------------------------------------------------

    def poll(self, kwargs):
        try:
            resp = self._cloud.cloudrequest(
                "/v2.0/cloud/thing/{}/shadow/properties".format(self._device_id),
                action="GET")
            if not resp.get("success"):
                self.log("Cloud error: {}".format(resp), level="WARNING")
                return
            dps = {p["code"]: p["value"]
                   for p in resp.get("result", {}).get("properties", [])}
            self._update_sensors(dps)
        except Exception as e:
            self.log("Poll exception: {}".format(e), level="WARNING")

    # -- Senzory --------------------------------------------------------------

    def _update_sensors(self, dps):
        if "switch" in dps:
            self.set_state("switch.tc_virivka_switch",
                state="on" if dps["switch"] else "off",
                attributes={"friendly_name": "TC virivka"})

        if "temp_current" in dps:
            self.set_state("sensor.tc_virivka_teplota",
                state=str(dps["temp_current"]),
                attributes={"friendly_name": "TC virivka teplota vody",
                            "unit_of_measurement": "°C",
                            "device_class": "temperature",
                            "state_class": "measurement"})

        if "temp_set" in dps:
            self.set_state("sensor.tc_virivka_teplota_cil",
                state=str(dps["temp_set"]),
                attributes={"friendly_name": "TC virivka cilova teplota",
                            "unit_of_measurement": "°C",
                            "device_class": "temperature"})

        if "mode" in dps:
            self.set_state("sensor.tc_virivka_rezim",
                state=MODE_MAP.get(dps["mode"], dps["mode"]),
                attributes={"friendly_name": "TC virivka rezim",
                            "raw": dps["mode"]})

        if "fault" in dps:
            fault = int(dps["fault"])
            bits = [i for i in range(30) if fault & (1 << i)]
            self.set_state("sensor.tc_virivka_porucha",
                state="OK" if fault == 0 else "PORUCHA",
                attributes={"friendly_name": "TC virivka porucha",
                            "raw_bitmap": str(fault),
                            "active_bits": str(bits) if bits else "none"})

        if "aux_manual" in dps:
            self.set_state("sensor.tc_virivka_aux_manual",
                state="on" if dps["aux_manual"] else "off",
                attributes={"friendly_name": "TC virivka aux_manual"})

        # InfluxDB historie
        mode = dps.get("mode", "unknown")
        line = ('pool_tc_data '
                'temp_current={tc}i,temp_set={ts}i,switch={sw}i,'
                'fault={fl}i,aux_manual={aux}i,mode="{md}"'.format(
                    tc=int(dps.get("temp_current", 0)),
                    ts=int(dps.get("temp_set", 0)),
                    sw=1 if dps.get("switch") else 0,
                    fl=int(dps.get("fault", 0)),
                    aux=1 if dps.get("aux_manual") else 0,
                    md=mode))
        try:
            requests.post("{}/write?db={}".format(self._influx_url, self._influx_db),
                          auth=self._influx_auth, data=line.encode("utf-8"),
                          timeout=5)
        except Exception:
            pass
