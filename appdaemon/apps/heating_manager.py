import appdaemon.plugins.hass.hassapi as hass
import requests
from datetime import datetime, timedelta

TRACKERS = {
    "Kamil": "device_tracker.iphone_19",
    "Romana": "device_tracker.unifi_default_c2_eb_91_20_3b_6d",
    "Nela": "device_tracker.unifi_default_de_f6_6b_c7_67_74",
    "Adela": "device_tracker.unifi_default_0e_c7_df_8a_66_f9",
}

DAIKIN = {
    "Adela": {
        "climate": "climate.adela_pokoj_room_temperature",
        "temp": "sensor.adela_pokoj_temperature",
        "tracker": "device_tracker.unifi_default_0e_c7_df_8a_66_f9",
    },
    "Nela": {
        "climate": "climate.nela_pokoj_room_temperature",
        "temp": "sensor.nela_pokoj_temperature",
        "tracker": "device_tracker.unifi_default_de_f6_6b_c7_67_74",
    },
    "Pracovna": {
        "climate": "climate.pracovna_room_temperature",
        "temp": "sensor.2_temperature",
        "tracker": "device_tracker.iphone_19",  # Kamil
        "presence_only": True,  # only turn off when away, no auto heat/cool
    },
    "Loznice": {
        "climate": "climate.loznice_room_temperature",
        "temp": "sensor.teplota_loznice_temperature",
        "tracker": ["device_tracker.iphone_19", "device_tracker.unifi_default_c2_eb_91_20_3b_6d"],  # Kamil OR Romana
        "presence_only": True,  # only turn off when away, no auto heat/cool
    },
}

NIGHT_START = 22
NIGHT_END = 5
NIGHT_TEMP = 21
PREHEAT_MINUTES = 60
FALLBACK_DEPARTURE = "06:00"
FALLBACK_RETURN = "15:00"

# Daikin hysteresis thresholds
DAIKIN_HEAT_ON_TEMP = 19.0     # start heating below this
DAIKIN_HEAT_OFF_TEMP = 20.5    # stop heating above this
DAIKIN_HEAT_TARGET = 20.0      # Daikin target in heat mode
DAIKIN_COOL_ON_TEMP = 25.0     # start cooling above this
DAIKIN_COOL_OFF_TEMP = 23.5    # stop cooling below this
DAIKIN_COOL_TARGET = 23.0      # Daikin target in cool mode


class HeatingManager(hass.Hass):

    def initialize(self):
        self._last_tc_target = None
        self._last_status = None
        self._last_eval_state = None  # track state transitions
        self._boiler_heated_today = False
        self._boiler_heating = False
        self._boiler_started_at = None  # track boiler start time
        self._saved_tc_target = 22  # saved TC target during boiler heating
        self._last_action = "Inicializace"
        self._last_action_time = datetime.now().strftime("%H:%M")
        self._cycle_count = 0

        # InfluxDB v1
        self._influx_url = "http://{}:{}".format(
            self.args.get("influxdb_host", "a0d7b954-influxdb"),
            self.args.get("influxdb_port", 8086))
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_user = self.args.get("influxdb_user", "db_write")
        self._influx_pass = self.args.get("influxdb_password", "db_write_pass")
        self._influx_ok = False
        try:
            r = requests.get("{}/ping".format(self._influx_url), timeout=5)
            self._influx_ok = r.status_code == 204
        except Exception:
            pass

        # Listen for presence changes
        for name, eid in TRACKERS.items():
            self.listen_state(self._on_presence, eid, name=name)
        self.listen_state(self._on_boiler, "switch.tepelnecerpadlo_3w_teplavoda")
        self.listen_state(self._on_summer, "input_boolean.summer_mode")
        self.listen_state(self._on_manual_boiler_request,
                          "input_boolean.ohrev_vody_switch", new="on")

        # Boiler heating control
        self.run_daily(self._boiler_schedule_check,
                       datetime.now().replace(hour=13, minute=0, second=0))
        self.listen_state(self._on_battery_soc,
                          "sensor.battery_state_of_capacity")
        self.listen_state(self._on_boiler_temp,
                          "sensor.teplota_bojler_spodni_teplota")
        self.run_daily(self._boiler_reset_daily,
                       datetime.now().replace(hour=0, minute=0, second=1))

        # Track TC switch state changes for InfluxDB logging
        for sw in ["switch.tepelnecerpadlo_topeni", "switch.tepelnecerpadlo_3w_topeni",
                    "switch.tepelnecerpadlo_3w_teplavoda", "switch.tepelnecerpadlo_bojler"]:
            self.listen_state(self._on_switch_change, sw)

        # Periodic checks
        self.run_every(self._periodic, "now+30", 600)       # TC every 10 min
        self.run_every(self._daikin_check, "now+60", 300)    # Daikin every 5 min
        self.run_every(self._build_schedule, "now+90", 1800) # Schedule every 30 min
        self.run_every(self._load_log, "now+120", 1800)      # Log every 30 min

        # Morning preheat at 04:30
        self.run_daily(self._morning_preheat, datetime.now().replace(
            hour=4, minute=30, second=0, microsecond=0))

        self._check_scheduler()
        self.log("HeatingManager initialized (influxdb={})".format(
            "OK" if self._influx_ok else "OFF"))

        # Startup recovery: detect stuck boiler valve from before restart
        self.run_in(self._startup_boiler_check, 15)

    def _startup_boiler_check(self, kwargs):
        """Detect stuck boiler valve from before AppDaemon restart."""
        teplavoda_on = self.get_state("switch.tepelnecerpadlo_3w_teplavoda") == "on"
        tc_on = self.get_state("switch.tepelnecerpadlo_topeni") == "on"
        temp = self._f("sensor.teplota_bojler_spodni_teplota", 50)

        if teplavoda_on and tc_on and temp < 43:
            # Boiler was heating before restart — resume tracking
            self._boiler_heating = True
            self._boiler_heated_today = True
            self._boiler_started_at = datetime.now()
            try:
                self._saved_tc_target = float(self.get_state("climate.topeni", attribute="temperature") or 21)
            except (ValueError, TypeError):
                self._saved_tc_target = 21
            # Set current_temp+1 to keep compressor on
            current_temp = self._f("sensor.teplota_obyvak_prumer",
                                   self._f("sensor.teplota_obyvak_temperature", 22))
            boiler_target = min(current_temp + 1, 30)
            self.call_service("climate/set_hvac_mode",
                              entity_id="climate.topeni", hvac_mode="heat")
            self.call_service("climate/set_temperature",
                              entity_id="climate.topeni", temperature=boiler_target)
            self.log("Startup: boiler resumed (TC {:.1f}->{:.1f}C, boiler={:.1f}C)".format(
                self._saved_tc_target, boiler_target, temp))
        elif teplavoda_on and not tc_on:
            # Stuck state: valve open but compressor off — reset
            self.log("Startup: STUCK boiler valve detected (teplavoda=ON, TC=OFF, temp={:.1f}C) — resetting".format(temp))
            self.call_service("switch/turn_off", entity_id="switch.tepelnecerpadlo_3w_teplavoda")
            self.call_service("switch/turn_on", entity_id="switch.tepelnecerpadlo_3w_topeni")
            self._boiler_heating = False
            self.run_in(lambda k: self._evaluate_tc("startup_recovery"), 5)
        else:
            self.log("Startup: boiler OK (teplavoda={}, TC={}, temp={:.1f}C)".format(
                "ON" if teplavoda_on else "OFF", "ON" if tc_on else "OFF", temp))

    # ── InfluxDB ───────────────────────────────────────────────────────────

    def _influx_write(self, line):
        if not self._influx_ok:
            return
        try:
            requests.post("{}/write?db={}".format(self._influx_url, self._influx_db),
                          auth=(self._influx_user, self._influx_pass),
                          data=line.encode("utf-8"), timeout=5)
        except Exception:
            pass

    def _influx_query(self, q):
        if not self._influx_ok:
            return []
        try:
            r = requests.get("{}/query".format(self._influx_url),
                             params={"db": self._influx_db, "q": q},
                             auth=(self._influx_user, self._influx_pass), timeout=10)
            if r.status_code == 200:
                return r.json().get("results", [{}])[0].get("series", [])
        except Exception:
            pass
        return []

    def _log_state(self, entity, domain, state, previous, reason):
        """Log state transition to InfluxDB state_log."""
        venkovni = self._f("sensor.venkovni_teplota_temperature")
        obyvak = self._f("sensor.teplota_obyvak_temperature")
        line = (
            'state_log,entity={ent},domain={dom} '
            'state="{st}",previous_state="{prev}",'
            'temp_indoor={ti},temp_outdoor={to},'
            'reason="{rsn}"'.format(
                ent=entity, dom=domain,
                st=state, prev=previous,
                ti=round(obyvak, 1), to=round(venkovni, 1),
                rsn=reason.replace('"', '')))
        self._influx_write(line)

    def _log_action(self, device, action, description, reason, target=None):
        """Log action to InfluxDB and update last_action."""
        self._last_action = description
        self._last_action_time = datetime.now().strftime("%H:%M")

        venkovni = self._f("sensor.venkovni_teplota_temperature")
        obyvak = self._f("sensor.teplota_obyvak_temperature")
        who = ",".join(self._who_home()) or "nikdo"

        line = (
            'heating_log,device={dev},action={act} '
            'description="{desc}",temp_indoor={ti},temp_outdoor={to},'
            'temp_target={tt},reason="{rsn}",persons_home="{who}"'.format(
                dev=device, act=action,
                desc=description.replace('"', '\\"')[:200],
                ti=round(obyvak, 1), to=round(venkovni, 1),
                tt=round(target or 0, 1), rsn=reason, who=who))
        self._influx_write(line)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _f(self, entity_id, default=0.0):
        try:
            v = self.get_state(entity_id)
            return float(v) if v not in (None, "unavailable", "unknown") else default
        except (ValueError, TypeError):
            return default

    def _someone_home(self):
        return any(self.get_state(e) == "home" for e in TRACKERS.values())

    def _who_home(self):
        return [n for n, e in TRACKERS.items() if self.get_state(e) == "home"]

    def _is_night(self):
        h = datetime.now().hour
        night_start = int(self._f("input_number.topeni_noc_od", NIGHT_START))
        night_end = int(self._f("input_number.topeni_noc_do", NIGHT_END))
        return h >= night_start or h < night_end

    def _is_summer(self):
        return self.get_state("input_boolean.summer_mode") == "on"

    def _boiler_active(self):
        return self.get_state("switch.tepelnecerpadlo_3w_teplavoda") == "on"

    # ── TC control ─────────────────────────────────────────────────────────

    def _set_tc(self, target, mode="heat", reason="", desc=""):
        if self._boiler_active():
            return

        current_mode = self.get_state("climate.topeni")
        current_temp = self.get_state("climate.topeni", attribute="temperature")
        changed = False

        if mode == "off" and current_mode != "off":
            self.call_service("climate/set_hvac_mode",
                              entity_id="climate.topeni", hvac_mode="off")
            changed = True
        elif mode == "heat":
            if current_mode != "heat":
                self.call_service("climate/set_hvac_mode",
                                  entity_id="climate.topeni", hvac_mode="heat")
                changed = True
            try:
                if abs(float(current_temp or 0) - target) > 0.1:
                    self.call_service("climate/set_temperature",
                                      entity_id="climate.topeni", temperature=target)
                    changed = True
            except (ValueError, TypeError):
                self.call_service("climate/set_temperature",
                                  entity_id="climate.topeni", temperature=target)
                changed = True

        if changed:
            full_desc = desc or "TC {}C {} - {}".format(target, mode, reason)
            self._log_action("tc", "target_change" if mode == "heat" else "off",
                             full_desc, reason, target)
            self._log_state("climate_topeni", "climate", mode,
                            current_mode or "unknown", reason)
            self.log("TC: {}".format(full_desc))

        self._last_tc_target = target

    def _set_daikin(self, climate_eid, mode, target=None, reason="", name=""):
        current = self.get_state(climate_eid)
        if mode == "off" and current == "off":
            return
        if mode in ("heat", "cool") and current == mode:
            try:
                ct = float(self.get_state(climate_eid, attribute="temperature") or 0)
                if abs(ct - (target or 0)) < 0.1:
                    return
            except (ValueError, TypeError):
                pass

        if mode == "off":
            self.call_service("climate/set_hvac_mode",
                              entity_id=climate_eid, hvac_mode="off")
        elif mode in ("heat", "cool") and target:
            self.call_service("climate/set_hvac_mode",
                              entity_id=climate_eid, hvac_mode=mode)
            self.call_service("climate/set_temperature",
                              entity_id=climate_eid, temperature=target)

        dev = "daikin_{}".format(name.lower()) if name else climate_eid.split(".")[1][:15]
        desc = "Daikin {} {} {}C - {}".format(name, mode, target or "", reason)
        self._log_action(dev, mode, desc, reason, target)
        self._log_state(dev, "climate", mode, current or "unknown", reason)
        self.log(desc)

    # ── Event handlers ─────────────────────────────────────────────────────

    def _on_presence(self, entity, attribute, old, new, kwargs):
        if new == "unavailable" or old == new:
            return
        name = kwargs.get("name", "?")
        self._evaluate_tc("{} {}".format(name, "prisel" if new == "home" else "odesel"))

    def _on_switch_change(self, entity, attribute, old, new, kwargs):
        if new in ("unavailable",) or old == new:
            return
        ent_name = entity.replace("switch.", "switch_").replace(".", "_")
        self._log_state(ent_name, "switch", new, old or "unknown", "state_change")

    def _on_manual_boiler_request(self, entity, attribute, old, new, kwargs):
        """Manual hot water request (tablet, AI agent, dashboard)."""
        # Reset the boolean immediately (it's a trigger, not a state)
        self.call_service("input_boolean/turn_off",
                          entity_id="input_boolean.ohrev_vody_switch")
        if self._boiler_heating:
            self.log("Manual boiler request ignored — already heating")
            return
        self._boiler_heated_today = False  # explicit request overrides daily flag
        self._try_start_boiler("manual_request")

    def _on_boiler(self, entity, attribute, old, new, kwargs):
        if new == "off" and self._boiler_heating:
            self.log("Boiler valve turned off externally")
            self._stop_boiler("valve_off_external")

    def _boiler_schedule_check(self, kwargs):
        self._try_start_boiler("schedule_13")

    def _on_battery_soc(self, entity, attribute, old, new, kwargs):
        try:
            old_v = float(old) if old not in (None, "unavailable", "unknown") else 0
            new_v = float(new) if new not in (None, "unavailable", "unknown") else 0
        except (ValueError, TypeError):
            return
        if old_v < 90 and new_v >= 90:
            self._try_start_boiler("soc_90")

    def _on_boiler_temp(self, entity, attribute, old, new, kwargs):
        if not self._boiler_heating:
            return
        try:
            temp = float(new) if new not in (None, "unavailable", "unknown") else 0
        except (ValueError, TypeError):
            return
        if temp >= 43:
            self._stop_boiler("temp_43")

    def _boiler_reset_daily(self, kwargs):
        self._boiler_heated_today = False
        self.log("Boiler daily flag reset")

    def _try_start_boiler(self, reason):
        if self._boiler_heated_today or self._boiler_heating:
            return
        temp = self._f("sensor.teplota_bojler_spodni_teplota", 50)
        if temp >= 40:
            return
        # Save current target, set current_temp+1 to keep compressor on
        # Set flags AFTER saving target (so _saved_tc_target is clean)
        try:
            self._saved_tc_target = float(self.get_state("climate.topeni", attribute="temperature") or 21)
        except (ValueError, TypeError):
            self._saved_tc_target = 21
        current_temp = self._f("sensor.teplota_obyvak_prumer",
                               self._f("sensor.teplota_obyvak_temperature", 22))
        boiler_target = min(current_temp + 1, 30)  # max_temp=30 in config
        self.call_service("climate/set_hvac_mode",
                          entity_id="climate.topeni", hvac_mode="heat")
        self.call_service("climate/set_temperature",
                          entity_id="climate.topeni", temperature=boiler_target)
        self.call_service("switch/turn_off", entity_id="switch.tepelnecerpadlo_3w_topeni")
        self.call_service("switch/turn_on", entity_id="switch.tepelnecerpadlo_3w_teplavoda")
        # Set flags after service calls
        self._boiler_heating = True
        self._boiler_heated_today = True
        self._boiler_started_at = datetime.now()
        self.log("Boiler START ({}): {:.1f}C (TC target {:.1f}->{:.1f}C)".format(
            reason, temp, self._saved_tc_target, boiler_target))
        try:
            self.call_service("notify/mobile_app_iphone_17",
                              title="Bojler", message="Ohrev zahajen ({:.1f}C)".format(temp))
        except Exception:
            pass

    def _stop_boiler(self, reason):
        if not self._boiler_heating:
            return
        self._boiler_heating = False
        self._boiler_started_at = None

        # Decide: does heating need TC right now? Use SAVED target, not current (override)
        obyvak = self._f("sensor.teplota_obyvak_temperature", 20)
        saved_target = getattr(self, "_saved_tc_target", 21)
        needs_heating = (not self._is_summer() and obyvak < saved_target)

        # Restore saved thermostat target, switch valve back to heating
        self.call_service("switch/turn_off", entity_id="switch.tepelnecerpadlo_3w_teplavoda")
        self.call_service("switch/turn_on", entity_id="switch.tepelnecerpadlo_3w_topeni")
        self.call_service("climate/set_temperature",
                          entity_id="climate.topeni", temperature=saved_target)

        if needs_heating:
            self.log("Boiler STOP -> heating ({:.1f}C < {:.1f}C, TC restored {:.1f}C)".format(
                obyvak, saved_target, saved_target))
        else:
            self.log("Boiler STOP -> idle ({:.1f}C >= {:.1f}C, TC restored {:.1f}C)".format(
                obyvak, saved_target, saved_target))

        temp = self._f("sensor.teplota_bojler_spodni_teplota", 0)
        self.log("Boiler STOP ({}): {:.1f}C".format(reason, temp))
        try:
            self.call_service("notify/mobile_app_iphone_17",
                              title="Bojler", message="Ohrev dokoncen ({:.1f}C)".format(temp))
        except Exception:
            pass
        # Force state re-evaluation by resetting last_eval_state
        self._last_eval_state = None
        self.run_in(lambda k: self._evaluate_tc("Bojler dokoncen"), 5)

    def _on_summer(self, entity, attribute, old, new, kwargs):
        self._evaluate_tc("Summer mode {}".format(new))

    def _periodic(self, kwargs):
        self._cycle_count += 1
        self._evaluate_tc("periodic")
        self._update_sensor()
        if self._cycle_count % 3 == 1:
            self._log_summary()

    # ── TC evaluation ──────────────────────────────────────────────────────

    def _evaluate_tc(self, trigger=""):
        # Determine current state (always, even during boiler)
        if self._is_summer():
            new_state = "summer"
        elif self._is_night():
            someone = self._someone_home()
            obyvak = self._f("sensor.teplota_obyvak_temperature", 20)
            if someone and obyvak < 18:
                new_state = "night_cold"
            else:
                new_state = "night"
        elif self._someone_home():
            new_state = "home"
        else:
            new_state = "away"

        state_changed = (new_state != self._last_eval_state)
        self._last_eval_state = new_state

        # Boiler has priority — track state but don't change temperature
        if self._boiler_active():
            # Timeout protection: if boiler runs > 90 min, something is wrong
            if self._boiler_started_at and (datetime.now() - self._boiler_started_at).total_seconds() > 5400:
                self.log("WARNING: boiler heating timeout (>90 min) — forcing stop")
                self._stop_boiler("timeout_90min")
                return
            # Detect stuck valve without heating (no _boiler_heating flag)
            if not self._boiler_heating:
                tc_on = self.get_state("switch.tepelnecerpadlo_topeni") == "on"
                if not tc_on:
                    self.log("WARNING: stuck boiler valve (teplavoda=ON, TC=OFF, _boiler_heating=False) — resetting")
                    self.call_service("switch/turn_off", entity_id="switch.tepelnecerpadlo_3w_teplavoda")
                    self.call_service("switch/turn_on", entity_id="switch.tepelnecerpadlo_3w_topeni")
                    # Fall through to normal evaluation
                else:
                    # TC is on, someone started boiler externally — track it
                    self._boiler_heating = True
                    self._boiler_started_at = datetime.now()
                    try:
                        self._saved_tc_target = float(self.get_state("climate.topeni", attribute="temperature") or 21)
                    except (ValueError, TypeError):
                        self._saved_tc_target = 21
                    current_temp = self._f("sensor.teplota_obyvak_prumer",
                                           self._f("sensor.teplota_obyvak_temperature", 22))
                    boiler_target = min(current_temp + 1, 30)  # max_temp=30 in config
                    self.call_service("climate/set_hvac_mode",
                                      entity_id="climate.topeni", hvac_mode="heat")
                    self.call_service("climate/set_temperature",
                                      entity_id="climate.topeni", temperature=boiler_target)
                    self.log("External boiler heating detected — tracking (TC target {:.1f}C)".format(boiler_target))
                    self._last_status = "Bojler prednost"
                    return
            else:
                self._last_status = "Bojler prednost"
                return

        # Only change temperature on state transitions
        # Periodic calls just update status display
        who = self._who_home()

        if new_state == "summer":
            if state_changed:
                self._set_tc(11, "off", "summer", "Letni rezim")
            self._last_status = "Leto"

        elif new_state == "night_cold":
            if state_changed:
                self._set_tc(21, "heat", "night_cold",
                             "Noc chladno - {}".format(trigger))
            self._last_status = "Noc (chladno)"

        elif new_state == "night":
            if state_changed:
                night = self._f("input_number.topeni_night_temp", NIGHT_TEMP)
                self._set_tc(night, "heat", "night",
                             "Noc {}C - {}".format(night, trigger))
            self._last_status = "Noc"

        elif new_state == "home":
            if state_changed:
                target = self._f("input_number.topeni_target_temp", 21)
                self._set_tc(target, "heat", "someone_home",
                             "Doma ({}) - {}".format(", ".join(who), trigger))
            # Always reflect actual hvac_action for status
            hvac_action = self.get_state("climate.topeni", attribute="hvac_action")
            if hvac_action == "heating":
                self._last_status = "Topi"
            else:
                self._last_status = "Doma (idle)"

        elif new_state == "away":
            if state_changed:
                away = self._f("input_number.topeni_away_temp", 19)
                self._set_tc(away, "heat", "nobody_home",
                             "Vsichni pryc - {}".format(trigger))
            self._last_status = "Away"

    # ── Daikin ─────────────────────────────────────────────────────────────

    def _daikin_check(self, kwargs):
        summer = self._is_summer()
        for name, cfg in DAIKIN.items():
            # Presence check — single tracker or list (any home = home)
            tracker = cfg["tracker"]
            if isinstance(tracker, list):
                doma = any(self.get_state(t) == "home" for t in tracker)
            else:
                doma = self.get_state(tracker) == "home"

            current_mode = self.get_state(cfg["climate"]) or "off"

            if not doma:
                if current_mode != "off":
                    self._set_daikin(cfg["climate"], "off",
                                     reason="nobody_home", name=name)
                continue

            # presence_only rooms — just turn off when away, don't auto start
            if cfg.get("presence_only"):
                continue

            temp = self._f(cfg["temp"], 20)

            if not summer:
                # Winter — heat with hysteresis
                if current_mode == "off" or current_mode == "cool":
                    if temp < DAIKIN_HEAT_ON_TEMP:
                        self._set_daikin(cfg["climate"], "heat", DAIKIN_HEAT_TARGET,
                                         reason="temp_low", name=name)
                elif current_mode == "heat":
                    if temp > DAIKIN_HEAT_OFF_TEMP:
                        self._set_daikin(cfg["climate"], "off",
                                         reason="temp_ok", name=name)
            else:
                # Summer — cool with hysteresis
                if current_mode == "off" or current_mode == "heat":
                    if temp > DAIKIN_COOL_ON_TEMP:
                        self._set_daikin(cfg["climate"], "cool", DAIKIN_COOL_TARGET,
                                         reason="temp_high", name=name)
                elif current_mode == "cool":
                    if temp < DAIKIN_COOL_OFF_TEMP:
                        self._set_daikin(cfg["climate"], "off",
                                         reason="temp_ok", name=name)

    # ── Morning preheat ────────────────────────────────────────────────────

    def _morning_preheat(self, kwargs):
        now = datetime.now()
        if now.weekday() >= 5 or self._is_summer():
            return
        dep = self._get_earliest_departure()
        if dep:
            pre = dep - timedelta(minutes=int(self._f("input_number.topeni_predehrev_min", PREHEAT_MINUTES)))
        else:
            h, m = map(int, FALLBACK_DEPARTURE.split(":"))
            pre = now.replace(hour=h, minute=m) - timedelta(minutes=int(self._f("input_number.topeni_predehrev_min", PREHEAT_MINUTES)))
        delay = max(0, int((pre - now).total_seconds()))
        if delay > 7200:
            delay = 0
        self.log("Preheat at {} (delay {}min)".format(pre.strftime("%H:%M"), delay // 60))
        self.run_in(self._do_preheat, delay)

    def _do_preheat(self, kwargs):
        if self._boiler_active():
            return
        target = self._f("input_number.topeni_target_temp", 21)
        self._set_tc(target, "heat", "morning_preheat", "Ranni predehrev")
        self._last_status = "Predehrev"

    def _get_earliest_departure(self):
        try:
            attrs = self.get_state("sensor.presence_patterns", attribute="all")
            if not attrs:
                return None
            persons = (attrs.get("attributes") or {}).get("persons", {})
            earliest = None
            for data in persons.values():
                dep = data.get("morning_departure", "-")
                if dep and dep != "-":
                    h, m = map(int, dep.split(":"))
                    mins = h * 60 + m
                    if earliest is None or mins < earliest:
                        earliest = mins
            if earliest:
                now = datetime.now()
                return now.replace(hour=earliest // 60, minute=earliest % 60, second=0)
        except Exception:
            pass
        return None

    # ── Schedule builder ───────────────────────────────────────────────────

    def _build_schedule(self, kwargs):
        try:
            self._do_build_schedule()
        except Exception as e:
            self.log("Schedule error: {}".format(e), level="WARNING")

    def _do_build_schedule(self):
        now = datetime.now()
        target_home = self._f("input_number.topeni_target_temp", 21)
        away = self._f("input_number.topeni_away_temp", 19)
        summer = self._is_summer()

        # Get departure/return patterns
        dep_time, ret_time = self._get_patterns_for_day(now)

        events = []
        prev_state = None

        for offset_min in range(0, 720, 30):  # 12h in 30-min steps
            t = now + timedelta(minutes=offset_min)
            h = t.hour
            dow = t.weekday()
            is_wknd = dow >= 5
            time_str = t.strftime("%H:%M")

            # Determine state
            if summer:
                state = ("Leto", 0, "summer")
            elif h >= int(self._f("input_number.topeni_noc_od", NIGHT_START)) or h < int(self._f("input_number.topeni_noc_do", NIGHT_END)):
                night = self._f("input_number.topeni_night_temp", NIGHT_TEMP)
                state = ("Noc", night, "night")
            elif not is_wknd and dep_time and ret_time:
                t_min = h * 60 + t.minute
                if dep_time <= t_min < ret_time:
                    state = ("Away", away, "nobody_home")
                else:
                    state = ("Doma", target_home, "someone_home")
            else:
                state = ("Doma", target_home, "someone_home")

            # Check preheat
            if not is_wknd and not summer and dep_time:
                pre_min = dep_time - int(self._f("input_number.topeni_predehrev_min", PREHEAT_MINUTES))
                if pre_min <= (h * 60 + t.minute) < dep_time and (h >= int(self._f("input_number.topeni_noc_do", NIGHT_END))):
                    state = ("Predehrev", target_home, "morning_preheat")

            if state != prev_state:
                events.append({
                    "time": time_str,
                    "event": state[0],
                    "tc_target": str(state[1]),
                    "reason": state[2],
                })
                prev_state = state

        self.set_state("sensor.heating_manager_schedule",
                       state=str(len(events)),
                       attributes={
                           "friendly_name": "Heating plan",
                           "icon": "mdi:calendar-clock",
                           "schedule": events[:20],
                           "generated_at": now.strftime("%Y-%m-%d %H:%M"),
                       })

    def _get_patterns_for_day(self, dt):
        """Return (departure_min, return_min) for a given day, or None."""
        if dt.weekday() >= 5:
            return None, None
        try:
            attrs = self.get_state("sensor.presence_patterns", attribute="all")
            if attrs:
                persons = (attrs.get("attributes") or {}).get("persons", {})
                dep_mins = []
                ret_mins = []
                for data in persons.values():
                    dep = data.get("morning_departure", "-")
                    ret = data.get("afternoon_return", "-")
                    if dep and dep != "-":
                        h, m = map(int, dep.split(":"))
                        dep_mins.append(h * 60 + m)
                    if ret and ret != "-":
                        h, m = map(int, ret.split(":"))
                        ret_mins.append(h * 60 + m)
                if dep_mins and ret_mins:
                    return min(dep_mins), max(ret_mins)
        except Exception:
            pass
        # Fallback
        h1, m1 = map(int, FALLBACK_DEPARTURE.split(":"))
        h2, m2 = map(int, FALLBACK_RETURN.split(":"))
        return h1 * 60 + m1, h2 * 60 + m2

    # ── Log loader ─────────────────────────────────────────────────────────

    def _load_log(self, kwargs):
        try:
            self._do_load_log()
        except Exception as e:
            self.log("Log load error: {}".format(e), level="WARNING")

    def _do_load_log(self):
        series = self._influx_query(
            "SELECT description, temp_indoor, temp_target "
            "FROM heating_log WHERE time > now() - 24h "
            "GROUP BY device, action ORDER BY time DESC LIMIT 20")
        log_entries = []
        for s in series:
            tags = s.get("tags", {})
            device = tags.get("device", "?")
            action = tags.get("action", "?")
            cols = s.get("columns", [])
            for row in s.get("values", []):
                d = dict(zip(cols, row))
                ts_str = d.get("time", "")
                # Convert UTC to local time
                local_str = ts_str
                try:
                    from datetime import datetime, timedelta, timezone
                    utc_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    local_dt = utc_dt.astimezone()
                    local_str = local_dt.strftime("%H:%M")
                except Exception:
                    local_str = ts_str[11:16] if len(ts_str) > 11 else ts_str
                log_entries.append({
                    "time": local_str,
                    "device": device,
                    "action": action,
                    "description": (d.get("description") or "")[:80],
                })

        self.set_state("sensor.heating_manager_log",
                       state=str(len(log_entries)),
                       attributes={
                           "friendly_name": "Heating log",
                           "icon": "mdi:history",
                           "log": log_entries,
                           "loaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       })

    # ── Status sensor ──────────────────────────────────────────────────────

    def _update_sensor(self):
        venkovni = self._f("sensor.venkovni_teplota_temperature")
        obyvak = self._f("sensor.teplota_obyvak_temperature")
        ct = self.get_state("climate.topeni", attribute="temperature")

        self.set_state("sensor.heating_manager_status",
                       state=str(self._last_status or "OK"),
                       attributes={
                           "friendly_name": "Heating Manager",
                           "icon": "mdi:radiator",
                           "someone_home": "yes" if self._someone_home() else "no",
                           "who_home": ", ".join(self._who_home()) or "nikdo",
                           "climate_target": str(ct or "?"),
                           "obyvak_temp": str(round(obyvak, 1)),
                           "venkovni_temp": str(round(venkovni, 1)),
                           "summer_mode": "yes" if self._is_summer() else "no",
                           "adela_daikin": self.get_state("climate.adela_pokoj_room_temperature") or "off",
                           "nela_daikin": self.get_state("climate.nela_pokoj_room_temperature") or "off",
                           "pracovna_daikin": self.get_state("climate.pracovna_room_temperature") or "off",
                           "boiler_priority": "yes" if self._boiler_active() else "no",
                           "last_action": self._last_action or "",
                           "last_action_time": self._last_action_time or "",
                           "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       })

    def _log_summary(self):
        venkovni = self._f("sensor.venkovni_teplota_temperature")
        obyvak = self._f("sensor.teplota_obyvak_temperature")
        ct = self.get_state("climate.topeni", attribute="temperature")
        who = self._who_home()
        self.log("{} | TC={}C obyvak={}C venk={}C | doma={}".format(
            self._last_status, ct, round(obyvak, 1), round(venkovni, 1),
            ", ".join(who) if who else "nikdo"))

    def _check_scheduler(self):
        for eid in ['switch.schedule_815d10', 'switch.schedule_92da5f',
                    'switch.schedule_a01615', 'switch.schedule_d4579c']:
            if self.get_state(eid) == "on":
                self.log("Scheduler {} is ON - may conflict".format(eid), level="WARNING")
