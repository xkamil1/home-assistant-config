import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime

CHECK_INTERVAL = 30       # seconds
LOG_INTERVAL = 300        # 5 minutes status log
HOUR_START = 7
HOUR_END = 17
TEMP_MAX = 58.0           # stop at this temperature
POWER_ON_THRESHOLD = 1800  # W export on phase B to turn on
POWER_OFF_THRESHOLD = 1300  # W hysteresis to turn off


class BoilerSurplus(hass.Hass):

    def initialize(self):
        self._last_log_time = 0
        self._dnes_start = None        # datetime when spirala turned on today
        self._dnes_celkem_min = 0      # accumulated minutes today
        self._last_action = "—"
        self._last_action_time = "—"
        self._action_log = []          # last 5 messages
        self._today = datetime.now().date()

        self._init_sensor()
        self.run_every(self.check, "now+5", CHECK_INTERVAL)
        self.log("BoilerSurplus started (interval: {}s)".format(CHECK_INTERVAL))

    # ── Main loop ─────────────────────────────────────────────────────────────

    def check(self, kwargs):
        now = datetime.now()

        # Reset daily counter on new day
        if now.date() != self._today:
            self._today = now.date()
            self._dnes_celkem_min = 0
            self._dnes_start = None

        phase_b = self._float("sensor.power_meter_phase_b_active_power")
        teplota = self._float("sensor.teplota_bojler_spodni_teplota")
        phase_b_ok = self._available("sensor.power_meter_phase_b_active_power")
        teplota_ok = self._available("sensor.teplota_bojler_spodni_teplota")
        spirala_on = self.get_state("switch.tepelnecerpadlo_bojler") == "on"

        # Accumulate running time
        if spirala_on and self._dnes_start is not None:
            elapsed = (now - self._dnes_start).total_seconds() / 60
            self._dnes_celkem_min_current = self._dnes_celkem_min + elapsed
        else:
            self._dnes_celkem_min_current = self._dnes_celkem_min

        # Sensor fault — safe state: off
        if not phase_b_ok or not teplota_ok:
            status = "Chyba_senzoru"
            if spirala_on:
                self._turn_off("Chyba senzoru — bezpecnostni vypnuti")
            self._update_sensor(status, phase_b, teplota, spirala_on)
            self._status_log(now, phase_b, teplota, spirala_on, force=True)
            return

        hour = now.hour
        in_window = HOUR_START <= hour < HOUR_END

        # Determine desired state
        if not in_window:
            status = "Mimo_cas"
            if spirala_on:
                self._turn_off("Mimo cas ({}:00-{}:00)".format(HOUR_START, HOUR_END))
        elif teplota >= TEMP_MAX:
            status = "Blokovano_teplota"
            if spirala_on:
                self._turn_off("Teplota dosazena {:.1f}°C".format(teplota))
        elif not spirala_on and phase_b > POWER_ON_THRESHOLD:
            status = "Nabiji"
            self._turn_on("Prebytok {}W na fazi B".format(int(phase_b)))
            spirala_on = True
        elif spirala_on and phase_b < POWER_OFF_THRESHOLD:
            status = "Ceka"
            self._turn_off("Prebytok klesl na {}W (< {}W)".format(int(phase_b), POWER_OFF_THRESHOLD))
            spirala_on = False
        elif spirala_on:
            status = "Nabiji"
        else:
            status = "Ceka"

        self._update_sensor(status, phase_b, teplota, spirala_on)
        self._status_log(now, phase_b, teplota, spirala_on)

    # ── Switch control ────────────────────────────────────────────────────────

    def _turn_on(self, reason):
        self.call_service("switch/turn_on", entity_id="switch.tepelnecerpadlo_bojler")
        self._dnes_start = datetime.now()
        msg = "Zapnuto — {}".format(reason)
        self._record_action(msg)
        self.log("ZAPNUTO: {}".format(reason))

    def _turn_off(self, reason):
        self.call_service("switch/turn_off", entity_id="switch.tepelnecerpadlo_bojler")
        if self._dnes_start is not None:
            elapsed = (datetime.now() - self._dnes_start).total_seconds() / 60
            self._dnes_celkem_min += elapsed
            self._dnes_start = None
        msg = "Vypnuto — {}".format(reason)
        self._record_action(msg)
        self.log("VYPNUTO: {}".format(reason))

    # ── Logging helpers ───────────────────────────────────────────────────────

    def _record_action(self, msg):
        ts = datetime.now().strftime("%H:%M")
        self._last_action = msg
        self._last_action_time = ts
        entry = "{} {}".format(ts, msg)
        self._action_log.insert(0, entry)
        self._action_log = self._action_log[:5]

    def _status_log(self, now, phase_b, teplota, spirala_on, force=False):
        elapsed = now.timestamp() - self._last_log_time
        if force or elapsed >= LOG_INTERVAL:
            self._last_log_time = now.timestamp()
            self.log("Status: faze_B={}W teplota={:.1f}°C spirala={} dnes={}min".format(
                int(phase_b), teplota,
                "ON" if spirala_on else "OFF",
                int(self._dnes_celkem_min_current)))

    # ── HA sensor ─────────────────────────────────────────────────────────────

    def _init_sensor(self):
        self.set_state("sensor.boiler_surplus_status",
                       state="Ceka",
                       attributes={
                           "friendly_name": "Spirala bojleru — stav",
                           "phase_b_power": 0,
                           "teplota": 0,
                           "spirala": "off",
                           "last_action": "—",
                           "last_action_time": "—",
                           "dnes_celkem_minut": 0,
                           "log": [],
                       })

    def _update_sensor(self, status, phase_b, teplota, spirala_on):
        self.set_state("sensor.boiler_surplus_status",
                       state=status,
                       attributes={
                           "friendly_name": "Spirala bojleru — stav",
                           "phase_b_power": int(phase_b),
                           "teplota": round(teplota, 1),
                           "spirala": "on" if spirala_on else "off",
                           "last_action": self._last_action,
                           "last_action_time": self._last_action_time,
                           "dnes_celkem_minut": int(self._dnes_celkem_min_current),
                           "log": self._action_log,
                       })

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _float(self, entity_id, default=0.0):
        try:
            v = self.get_state(entity_id)
            return float(v) if v not in (None, "unavailable", "unknown") else default
        except (ValueError, TypeError):
            return default

    def _available(self, entity_id):
        v = self.get_state(entity_id)
        return v not in (None, "unavailable", "unknown")
