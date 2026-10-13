"""Bridge pro TC virivky (Tuya Pool Mini) nad localtuya — bez cloudu (od 2026-10-12).

Zarizeni vlastni localtuya (host 10.0.40.108, protokol 3.4, device bf4b4782140e456631syi9):
  climate.tc_virivka_klima  DP1 on/off (heat/off), DP2 cilova teplota, DP3 aktualni teplota
  select.tc_virivka_mode    DP4 mode Heat/Cool/Auto (friendly Topi/Chladi/Auto, raw v atributu raw_state)
  sensor.tc_virivka_fault   DP21 bitmapa poruch

Tato app zachovava puvodni entity a ovladani:
  sensor.tc_virivka_teplota / _teplota_cil / _rezim / _porucha, switch.tc_virivka_switch (virtualni)
  input_number.tc_virivka_teplota_cil -> climate.set_temperature
  input_select.tc_virivka_rezim (Heat/Cool/Auto) -> select.select_option
  switch.tc_virivka_switch turn_on/off -> climate.set_hvac_mode heat/off
  InfluxDB measurement pool_tc_data (kazdych poll_interval s).
DP101 aux_manual lokalne neni k dispozici -> sensor.tc_virivka_aux_manual se uz neaktualizuje.
"""

import appdaemon.plugins.hass.hassapi as hass
import requests

CLIMATE = "climate.tc_virivka_klima"
SELECT = "select.tc_virivka_mode"
FAULT = "sensor.tc_virivka_fault"

MODE_MAP = {"Heat": "Topi", "Cool": "Chladi", "Auto": "Auto"}   # raw -> cesky (= friendly option v select)


class PoolTCPoller(hass.Hass):

    def initialize(self):
        self._poll_secs = self.args.get("poll_interval", 60)
        self._influx_url = "http://{}:{}".format(
            self.args.get("influxdb_host", "a0d7b954-influxdb"),
            self.args.get("influxdb_port", 8086))
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_auth = (self.args.get("influxdb_user", "db_write"),
                             self.args.get("influxdb_password", "db_write_pass"))

        # Ovladani (helpery -> localtuya entity)
        self.listen_state(self.on_temp_change, "input_number.tc_virivka_teplota_cil")
        self.listen_state(self.on_mode_change, "input_select.tc_virivka_rezim")
        self.listen_event(self.on_switch_service, "call_service")

        # Okamzita aktualizace pri zmene localtuya entit
        self.listen_state(self.on_source_change, CLIMATE, attribute="all")
        self.listen_state(self.on_source_change, SELECT)
        self.listen_state(self.on_source_change, FAULT)

        self.run_every(self.poll, "now", self._poll_secs)
        self.log("Pool TC bridge (localtuya) started, Influx every {}s".format(self._poll_secs))

    # -- Ovladani -------------------------------------------------------------

    def on_switch_service(self, event_name, data, kwargs):
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
            self.call_service("climate/set_hvac_mode", entity_id=CLIMATE,
                              hvac_mode="heat" if desired else "off")
            self.set_state("switch.tc_virivka_switch",
                           state="on" if desired else "off",
                           attributes={"friendly_name": "TC virivka"})
            self.log("SET switch {} -> {}".format("ON" if desired else "OFF", CLIMATE))
            self.run_in(self.poll, 4)
        except Exception as e:
            self.log("on_switch_service error: {}".format(e), level="ERROR")

    def on_temp_change(self, entity, attribute, old, new, kwargs):
        if new == old or new in (None, "unavailable", "unknown"):
            return
        try:
            temp = max(5, min(40, int(float(new))))
            self.call_service("climate/set_temperature", entity_id=CLIMATE, temperature=temp)
            self.log("SET temp_set {} C -> {}".format(temp, CLIMATE))
            self.run_in(self.poll, 4)
        except Exception as e:
            self.log("on_temp_change error: {}".format(e), level="ERROR")

    def on_mode_change(self, entity, attribute, old, new, kwargs):
        if new == old or new not in MODE_MAP:
            return
        try:
            self.call_service("select/select_option", entity_id=SELECT, option=MODE_MAP[new])
            self.log("SET mode {} -> {}".format(new, SELECT))
            self.run_in(self.poll, 4)
        except Exception as e:
            self.log("on_mode_change error: {}".format(e), level="ERROR")

    # -- Cteni ----------------------------------------------------------------

    def _read(self):
        """Vrati dict ve stejnem tvaru jako drive cloudove dps, nebo None kdyz localtuya nema data."""
        clim = self.get_state(CLIMATE, attribute="all") or {}
        state = clim.get("state")
        if state in (None, "unavailable", "unknown"):
            self._nodata = getattr(self, "_nodata", 0) + 1
            if self._nodata in (1, 5) or self._nodata % 30 == 0:
                self.log("localtuya bez dat ({}x): climate={!r} select={!r}".format(
                    self._nodata, clim, self.get_state(SELECT)), level="WARNING")
            return None
        self._nodata = 0
        attrs = clim.get("attributes", {})
        dps = {"switch": state != "off"}
        if attrs.get("current_temperature") is not None:
            dps["temp_current"] = attrs["current_temperature"]
        if attrs.get("temperature") is not None:
            dps["temp_set"] = attrs["temperature"]
        sel = self.get_state(SELECT, attribute="all") or {}
        raw_mode = sel.get("attributes", {}).get("raw_state")
        if raw_mode in MODE_MAP:
            dps["mode"] = raw_mode
        elif sel.get("state") in MODE_MAP.values():
            dps["mode"] = [k for k, v in MODE_MAP.items() if v == sel["state"]][0]
        fault = self.get_state(FAULT)
        try:
            dps["fault"] = int(float(fault))
        except (TypeError, ValueError):
            pass
        return dps

    def on_source_change(self, entity, attribute, old, new, kwargs):
        dps = self._read()
        if dps:
            self._update_sensors(dps, influx=False)

    def poll(self, kwargs):
        dps = self._read()
        if not dps:
            self.set_state("sensor.tc_virivka_porucha", state="NEDOSTUPNE",
                           attributes={"friendly_name": "TC virivka porucha",
                                       "raw_bitmap": "n/a", "active_bits": "localtuya unavailable"})
            return
        self._update_sensors(dps, influx=True)

    # -- Senzory --------------------------------------------------------------

    def _update_sensors(self, dps, influx=True):
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

        if not influx:
            return
        line = ('pool_tc_data '
                'temp_current={tc}i,temp_set={ts}i,switch={sw}i,'
                'fault={fl}i,mode="{md}"'.format(
                    tc=int(dps.get("temp_current", 0)),
                    ts=int(dps.get("temp_set", 0)),
                    sw=1 if dps.get("switch") else 0,
                    fl=int(dps.get("fault", 0)),
                    md=dps.get("mode", "unknown")))
        try:
            requests.post("{}/write?db={}".format(self._influx_url, self._influx_db),
                          auth=self._influx_auth, data=line.encode("utf-8"),
                          timeout=5)
        except Exception:
            pass
