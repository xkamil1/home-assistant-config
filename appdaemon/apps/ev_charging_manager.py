import appdaemon.plugins.hass.hassapi as hass
import requests
from datetime import datetime


FORD_PLUG = "sensor.fordpass_wf0fxxwpmhsc70607_elvehplug"
FORD_SOC = "sensor.fordpass_wf0fxxwpmhsc70607_soc"
FORD_ENERGY = "sensor.fordpass_wf0fxxwpmhsc70607_energytransferlogentry"
HDO_ENTITY = "switch.hdo_signalizace"


class EVChargingManager(hass.Hass):
    """Deterministic EV charging manager with Ford/Elroq detection.

    Vehicle detection: Ford plug CONNECTED → Ford, otherwise → Elroq.
    Elroq: full wallbox control (current, DLM, battery lock).
    Ford: full wallbox control (current, DLM) but no home battery lock.
    """

    def initialize(self):
        self._influx_host = self.args.get("influxdb_host", "a0d7b954-influxdb")
        self._influx_port = self.args.get("influxdb_port", 8086)
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_auth = (
            self.args.get("influxdb_user", "db_write"),
            self.args.get("influxdb_password", "db_write_pass")
        )
        self._notify = self.args.get("notify_entity", "notify.mobile_app_iphone_17")
        self._day_amps = int(self.args.get("day_current_a", 6))
        self._night_amps = int(self.args.get("night_current_a", 13))
        self._day_start = int(self.args.get("day_start_hour", 5))
        self._night_start = int(self.args.get("night_start_hour", 21))
        self._battery_kwh = float(self.args.get("battery_capacity_kwh", 77.0))

        # DLM — Dynamic Load Management
        self._dlm_handle = None
        self._last_current_change = None
        self._dlm_suspended = False

        # Battery mode switch cooldown (min 10 min between switches)
        self._last_battery_mode_change = None
        self._battery_cooldown_sec = 600

        # Session state
        self._session_active = False
        self._session_start_time = None
        self._session_start_soc = 0.0
        self._session_energy_start = 0.0
        self._vt_paused = False

        # Vehicle detection
        self._active_vehicle = None  # "elroq" / "ford" / None
        self._pending_detection = False
        self._ford_session_start = None
        self._ford_soc_start = 0.0
        self._ford_energy_start = 0.0

        # Trigger: Ford plug (primary vehicle detection)
        self.listen_state(self._on_ford_plug_change, FORD_PLUG)

        # Trigger: Wallbox state changes
        self.listen_state(self._on_charger_state, "sensor.ev_charger_stav")

        # Trigger: HDO tariff changes (HW Tasmota switch, on=NT off=VT)
        self.listen_state(self._on_hdo_change, HDO_ENTITY)

        # Trigger: Day/night current switch
        self.run_daily(self._on_day_start,
                       datetime.now().replace(hour=self._day_start, minute=0, second=0))
        self.run_daily(self._on_night_start,
                       datetime.now().replace(hour=self._night_start, minute=0, second=0))

        # Fallback: 07:30 daily check
        self.run_daily(self._fallback_check,
                       datetime.now().replace(hour=7, minute=30, second=0))

        self.log("EVChargingManager initialized (day={}A/{:02d}h, night={}A/{:02d}h)".format(
            self._day_amps, self._day_start, self._night_amps, self._night_start))

        # Startup recovery at 30s and 60s (Tuya cloud entity slow to load)
        self.run_in(self._startup_check, 30)
        self.run_in(self._startup_check, 60)

    # ── STARTUP RECOVERY ──────────────────────────────────

    def _startup_check(self, kwargs):
        if self._session_active:
            return  # already recovered

        stav = self.get_state("sensor.ev_charger_stav") or ""
        if stav not in ("Nabiji", "Ceka", "Pripojeno", "Dokonceno"):
            self.log("Startup: no active session (stav={})".format(stav))
            return

        # Detect vehicle: Ford plug is reliable, Skoda API is not
        ford = self.get_state(FORD_PLUG) == "CONNECTED"
        if ford:
            self._active_vehicle = "ford"
            self._ford_session_start = self.datetime()
            self._ford_soc_start = self._get_float(FORD_SOC)
            self._ford_energy_start = self._get_float(FORD_ENERGY)
            self._session_active = True
            self._session_energy_start = self._get_float("sensor.ev_charger_energie_seance")
            self._vt_paused = False
            # Only pause for evening VT (19h)
            if self._is_vt():
                if datetime.now().hour >= 19:
                    self._vt_paused = True
                    self.log("Startup: Ford session evening VT paused (stav={}, SOC={:.0f}%)".format(
                        stav, self._ford_soc_start))
                    return
            amps = self._get_current_amps()
            self._set_current(amps)
            self._turn_on_switch()
            self._start_dlm()
            self.log("Startup: Ford session (stav={}, SOC={:.0f}%, {}A)".format(
                stav, self._ford_soc_start, amps))
        else:
            self._active_vehicle = "elroq"
            soc = self._get_float("sensor.skoda_elroq_battery_percentage")
            energy = self._get_float("sensor.ev_charger_energie_seance")
            self._session_active = True
            self._session_start_time = datetime.now()
            self._session_start_soc = soc
            self._session_energy_start = energy
            self._vt_paused = False
            if stav == "Nabiji":
                self._start_dlm()
            self.log("Startup: Elroq session (stav={}, SOC={}%, DLM={})".format(
                stav, int(soc), "ON" if stav == "Nabiji" else "OFF"))

    # ── TRIGGERS ──────────────────────────────────────────

    def _on_ford_plug_change(self, entity, attribute, old, new, kwargs):
        """Ford plug state changed — primary vehicle detection trigger."""
        if new == "CONNECTED" and old != "CONNECTED":
            if self._active_vehicle is not None or self._session_active:
                return
            self.log("Ford plug CONNECTED — detecting in 30s")
            self._pending_detection = True
            self.run_in(self._detect_and_start, 30)

    def _on_charger_state(self, entity, attribute, old, new, kwargs):
        """Wallbox state changed."""
        if old == new:
            return

        # Any transition to active charging state → detect vehicle
        if new in ("Pripojeno", "Ceka", "Nabiji"):
            # Swap detection: active Ford session but Ford unplugged → end & re-detect
            if self._session_active and self._active_vehicle == "ford":
                if self.get_state(FORD_PLUG) != "CONNECTED":
                    self.log("Vehicle swap detected (Ford unplugged, wallbox {}), ending Ford session".format(new))
                    self._end_ford_session("swap")
                    self.run_in(self._detect_and_start, 30)
                    return
            if self._session_active or self._active_vehicle is not None:
                return
            if self._pending_detection:
                return  # Ford plug trigger already waiting
            self.log("Wallbox {} -> {} — detecting in 2 min".format(old, new))
            self.run_in(self._detect_and_start, 120)

        # Charging complete
        elif new == "Dokonceno" and self._session_active:
            if self._active_vehicle == "ford":
                self._end_ford_session("dokonceno")
            else:
                self._end_session("dokonceno")

        # Car disconnected
        elif new == "Volny":
            if self._session_active:
                if self._active_vehicle == "ford":
                    self._end_ford_session("odpojeno")
                else:
                    self._end_session("odpojeno")
            self._pending_detection = False

    def _on_hdo_change(self, entity, attribute, old, new, kwargs):
        if new in ("unavailable", "unknown") or old in ("unavailable", "unknown"):
            return
        if not self._session_active:
            return
        if new == "off" and old == "on":
            h = datetime.now().hour
            # Only pause for evening VT (19h). Daytime VT (08,12,15) — keep charging.
            if h < 19:
                self.log("VT started at {}h — daytime, continuing".format(h))
                return
            self.log("VT started at {}h — pausing".format(h))
            self._pause_for_vt()
        elif new == "on" and old == "off":
            if self._vt_paused:
                self._resume_from_vt()

    def _on_day_start(self, kwargs):
        if not self._session_active or self._vt_paused:
            return
        if self.get_state("sensor.ev_charger_stav") == "Volny":
            return
        self._set_current(self._day_amps)
        self.log("Day mode: {}A ({})".format(self._day_amps, self._active_vehicle))

    def _on_night_start(self, kwargs):
        if not self._session_active or self._vt_paused:
            return
        if self.get_state("sensor.ev_charger_stav") == "Volny":
            return
        self._set_current(self._night_amps)
        self.log("Night mode: {}A ({})".format(self._night_amps, self._active_vehicle))

    # ── VEHICLE DETECTION ─────────────────────────────────

    def _detect_and_start(self, kwargs):
        self._pending_detection = False

        if self._active_vehicle is not None or self._session_active:
            return

        # Ford plug is reliable; Skoda charger_connected is NOT
        ford = self.get_state(FORD_PLUG) == "CONNECTED"

        if ford:
            self._active_vehicle = "ford"
            self.log("Detected Ford PHEV")
            self._start_ford_session()
        else:
            # Not Ford → must be Elroq (default)
            self._active_vehicle = "elroq"
            self.log("Detected Elroq (Ford plug={})".format(
                self.get_state(FORD_PLUG)))
            self._start_session()

    # ── ELROQ SESSION ─────────────────────────────────────

    def _start_session(self):
        soc = self._get_float("sensor.skoda_elroq_battery_percentage")
        energy = self._get_float("sensor.ev_charger_energie_seance")
        amps = self._get_current_amps()

        self._session_active = True
        self._session_start_time = datetime.now()
        self._session_start_soc = soc
        self._session_energy_start = energy
        self._vt_paused = False

        # Evening VT (19h) → pause. Daytime VT → charge normally.
        if self._is_vt():
            if datetime.now().hour >= 19:
                self._vt_paused = True
                self.log("Elroq connected during evening VT — waiting for NT")
                self._notify_push("Elroq pripojeno | SOC: {}% | Cekam na NT".format(int(soc)))
                return

        self._lock_battery()
        self._set_current(amps)
        self._turn_on_switch()
        self._start_dlm()

        self.log("Elroq session started: SOC={}%, amps={}A".format(soc, amps))
        self._notify_push("Elroq nabijeni zahajeno | SOC: {}% | {}A".format(int(soc), amps))

    def _end_session(self, reason):
        self._stop_dlm()
        self.call_service("switch/turn_off", entity_id="switch.ev_charger_switch")
        self._unlock_battery(force=True)

        energy_now = self._get_float("sensor.ev_charger_energie_seance")
        kwh = max(0, energy_now - self._session_energy_start)
        soc_now = self._get_float("sensor.skoda_elroq_battery_percentage")
        duration = 0.0
        if self._session_start_time:
            duration = (datetime.now() - self._session_start_time).total_seconds() / 60.0

        self._save_session_influx("elroq", kwh, self._session_start_soc, soc_now, duration)

        self.log("Elroq {}: {:.1f} kWh, {}% -> {}%".format(
            reason, kwh, self._session_start_soc, soc_now))
        self._notify_push("Elroq {} | {:.1f} kWh | SOC: {}%".format(
            reason, kwh, int(soc_now)))

        self._session_active = False
        self._vt_paused = False
        self._active_vehicle = None

    # ── FORD SESSION (passive tracking) ───────────────────

    def _start_ford_session(self):
        self._ford_session_start = self.datetime()
        self._ford_soc_start = self._get_float(FORD_SOC)
        self._ford_energy_start = self._get_float(FORD_ENERGY)
        self._session_active = True
        self._session_start_time = self.datetime()
        self._session_energy_start = self._get_float("sensor.ev_charger_energie_seance")
        self._vt_paused = False

        # Evening VT (19h) → pause. Daytime VT → charge normally.
        if self._is_vt():
            if datetime.now().hour >= 19:
                self._vt_paused = True
                self.log("Ford connected during evening VT — waiting for NT")
                self._notify_push("Ford PHEV pripojen | SOC: {:.0f}% | Cekam na NT".format(self._ford_soc_start))
                return

        amps = self._get_current_amps()
        self._set_current(amps)
        self._turn_on_switch()
        self._start_dlm()

        self.log("Ford session started: SOC={:.0f}%, amps={}A".format(self._ford_soc_start, amps))
        self._notify_push("Ford PHEV nabijeni | SOC: {:.0f}% | {}A".format(self._ford_soc_start, amps))

    def _end_ford_session(self, reason):
        self._stop_dlm()
        self.call_service("switch/turn_off", entity_id="switch.ev_charger_switch")
        self._unlock_battery(force=True)

        if not self._ford_session_start:
            self._session_active = False
            self._active_vehicle = None
            return

        duration_min = (self.datetime() - self._ford_session_start).total_seconds() / 60.0
        soc_end = self._get_float(FORD_SOC)

        # Primary: wallbox energy, fallback: Ford energy entity
        wb_energy = self._get_float("sensor.ev_charger_energie_seance")
        if wb_energy > self._session_energy_start > 0:
            kwh = wb_energy - self._session_energy_start
        else:
            energy_end = self._get_float(FORD_ENERGY)
            if energy_end > self._ford_energy_start > 0:
                kwh = energy_end - self._ford_energy_start
            elif soc_end > self._ford_soc_start:
                kwh = (soc_end - self._ford_soc_start) / 100.0 * 11.8
            else:
                kwh = 0.0

        cost = kwh * self.args.get("kwh_price", 4.53)

        self._save_session_influx(
            "ford", kwh, self._ford_soc_start, soc_end, duration_min)

        self.log("Ford {}: {:.1f} kWh, {:.0f}% -> {:.0f}%, {:.0f} min".format(
            reason, kwh, self._ford_soc_start, soc_end, duration_min))
        self._notify_push("Ford {} | {:.1f} kWh | SOC: {:.0f}% -> {:.0f}% | {:.0f} Kc".format(
            reason, kwh, self._ford_soc_start, soc_end, cost))

        self._ford_session_start = None
        self._session_active = False
        self._active_vehicle = None
        self._vt_paused = False

    # ── VT/NT CONTROL ─────────────────────────────────────

    def _pause_for_vt(self):
        self._vt_paused = True
        self._stop_dlm()
        self.call_service("switch/turn_off", entity_id="switch.ev_charger_switch")
        if self._active_vehicle != "ford":
            self._unlock_battery(force=True)
        self.log("VT pause: {} charging paused".format(self._active_vehicle or "EV"))
        self._notify_push("Nabijeni {} preruseno — VT tarif".format(
            self._active_vehicle or "EV"))

    def _resume_from_vt(self):
        self._vt_paused = False

        stav = self.get_state("sensor.ev_charger_stav")
        if stav == "Volny":
            self.log("NT resume: car not connected, ending session")
            self._session_active = False
            self._active_vehicle = None
            return

        amps = self._get_current_amps()
        self._lock_battery()
        self._set_current(amps)
        self._turn_on_switch()
        self._start_dlm()
        self.log("NT resume: {} charging at {}A".format(self._active_vehicle, amps))
        self._notify_push("{} nabijeni obnoveno — NT | {}A".format(
            self._active_vehicle.capitalize() if self._active_vehicle else "EV", amps))

    # ── BATTERY CONTROL ───────────────────────────────────

    def _lock_battery(self):
        now = datetime.now()
        if self._last_battery_mode_change:
            elapsed = (now - self._last_battery_mode_change).total_seconds()
            if elapsed < self._battery_cooldown_sec:
                self.log("Battery lock skipped — cooldown ({:.0f}s remaining)".format(
                    self._battery_cooldown_sec - elapsed))
                return
        self.call_service("number/set_value",
                          entity_id="number.battery_maximum_discharging_power", value=0)
        self.call_service("select/select_option",
                          entity_id="select.battery_working_mode",
                          option="fixed_charge_discharge")
        self._last_battery_mode_change = now
        self.log("Battery locked")

    def _unlock_battery(self, force=False):
        now = datetime.now()
        if not force and self._last_battery_mode_change:
            elapsed = (now - self._last_battery_mode_change).total_seconds()
            if elapsed < self._battery_cooldown_sec:
                self.log("Battery unlock skipped — cooldown ({:.0f}s remaining)".format(
                    self._battery_cooldown_sec - elapsed))
                return
        self.call_service("select/select_option",
                          entity_id="select.battery_working_mode",
                          option="maximise_self_consumption")
        self.call_service("number/set_value",
                          entity_id="number.battery_maximum_discharging_power", value=5000)
        self._last_battery_mode_change = now
        self.log("Battery unlocked{}".format(" (forced)" if force else ""))

    # ── FALLBACK ──────────────────────────────────────────

    def _fallback_check(self, kwargs):
        mode = self.get_state("select.battery_working_mode")
        stav = self.get_state("sensor.ev_charger_stav")
        if mode == "fixed_charge_discharge" and stav == "Volny":
            self._unlock_battery(force=True)
            self._session_active = False
            self._vt_paused = False
            self._active_vehicle = None
            self.log("FALLBACK: Battery unlocked (car not connected)")

    # ── DYNAMIC LOAD MANAGEMENT ───────────────────────────

    def _start_dlm(self):
        if self._dlm_handle:
            return
        self._dlm_suspended = False
        self._last_current_change = None
        self._dlm_handle = self.run_every(self._dlm_check, "now+10", 30)
        self.log("DLM started")

    def _stop_dlm(self):
        if self._dlm_handle:
            self.cancel_timer(self._dlm_handle)
            self._dlm_handle = None
        self._dlm_suspended = False
        self.log("DLM stopped")

    def _dlm_check(self, kwargs):
        if not self._session_active:
            return

        phase_a = self._get_float("sensor.power_meter_phase_a_active_power")
        phase_b = self._get_float("sensor.power_meter_phase_b_active_power")
        phase_c = self._get_float("sensor.power_meter_phase_c_active_power")

        load_a = max(0, -phase_a) / 230
        load_b = max(0, -phase_b) / 230
        load_c = max(0, -phase_c) / 230
        max_load = max(load_a, load_b, load_c)

        current = self._get_float("input_number.ev_charger_proud") or 6
        now = datetime.now()
        h = now.hour
        max_current = self._night_amps if (h >= self._night_start or h < self._day_start) else self._day_amps

        if max_load > 24.0:
            if not self._dlm_suspended:
                self._dlm_suspended = True
                self.call_service("switch/turn_off", entity_id="switch.ev_charger_switch")
                self.log("DLM EMERGENCY: wallbox OFF, {:.1f}A".format(max_load))
                self._notify_push("Nabijeni zastaveno — pretizeni {:.1f}A".format(max_load))
            return

        if max_load > 21.0:
            if current > 6:
                self._set_current(6)
                self._last_current_change = now
                self.log("DLM CRITICAL: {}A->6A ({:.1f}A)".format(int(current), max_load))
            return

        if max_load > 18.0:
            self._dlm_adjust(max(6, current - 2), "WARNING", max_load)
            return

        if self._dlm_suspended and max_load < 15.0:
            self._dlm_suspended = False
            self._set_current(6)
            self._turn_on_switch()
            self._last_current_change = now
            self.log("DLM: restored at 6A ({:.1f}A)".format(max_load))
            return

        if max_load < 15.0 and current < max_current and not self._dlm_suspended:
            if self._last_current_change:
                if (now - self._last_current_change).total_seconds() < 120:
                    return
            self._dlm_adjust(min(max_current, current + 1), "SAFE", max_load)

    def _dlm_adjust(self, new_current, reason, load):
        now = datetime.now()
        if self._last_current_change:
            if (now - self._last_current_change).total_seconds() < 60:
                return
        current = self._get_float("input_number.ev_charger_proud") or 6
        new_current = round(new_current)
        if new_current == round(current):
            return
        self._set_current(new_current)
        self._last_current_change = now
        self.log("DLM {}: {}A->{}A ({:.1f}A)".format(reason, int(current), new_current, load))

    # ── HELPERS ────────────────────────────────────────────

    def _get_current_amps(self):
        h = datetime.now().hour
        if h >= self._night_start or h < self._day_start:
            return self._night_amps
        return self._day_amps

    def _is_vt(self):
        state = self.get_state(HDO_ENTITY)
        if state in ("unavailable", "unknown", None):
            self.log("HDO switch unavailable, assuming NT", level="WARNING")
            return False
        return state == "off"

    def _turn_on_switch(self):
        self.call_service("switch/turn_on", entity_id="switch.ev_charger_switch")
        self.run_in(self._verify_switch_on, 10)

    def _verify_switch_on(self, kwargs):
        if not self._session_active or self._vt_paused:
            return
        state = self.get_state("switch.ev_charger_switch")
        if state != "on":
            self.log("Switch verify: state={}, retrying".format(state), level="WARNING")
            self.call_service("switch/turn_on", entity_id="switch.ev_charger_switch")
            self.run_in(self._verify_switch_on_final, 10)

    def _verify_switch_on_final(self, kwargs):
        if not self._session_active or self._vt_paused:
            return
        state = self.get_state("switch.ev_charger_switch")
        if state != "on":
            self.log("Switch verify FAILED after retry: state={}".format(state), level="ERROR")
            self._notify_push("CHYBA: Wallbox switch se nezapnul ({})".format(state))

    def _set_current(self, amps):
        self.call_service("input_number/set_value",
                          entity_id="input_number.ev_charger_proud", value=float(amps))

    def _get_float(self, entity_id):
        try:
            v = self.get_state(entity_id)
            if v in (None, "unavailable", "unknown"):
                return 0.0
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    def _notify_push(self, message):
        try:
            svc = self._notify.replace("notify.", "notify/")
            self.call_service(svc, title="EV Charging", message=message)
        except Exception as e:
            self.log("Push error: {}".format(e))

    def _save_session_influx(self, vehicle, kwh, soc_start, soc_end, duration_min):
        try:
            line = ('ev_charging_sessions,'
                    'auto={} '
                    'kwh={:.2f},'
                    'soc_start={:.1f},'
                    'soc_end={:.1f},'
                    'duration_min={:.1f}').format(
                vehicle, kwh, soc_start, soc_end, duration_min)
            requests.post(
                "http://{}:{}/write".format(self._influx_host, self._influx_port),
                params={"db": self._influx_db},
                data=line.encode(),
                auth=self._influx_auth,
                timeout=5
            )
            self.log("Session saved: {} {:.1f} kWh".format(vehicle, kwh))
        except Exception as e:
            self.log("InfluxDB error: {}".format(e))
