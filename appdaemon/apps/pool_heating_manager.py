"""Pool Heating Manager — rizeni TC bazenu z prebytku FVE.

Logika:
- Filtrace jede podle sveho rozvrhu (10-17, automatizace bazen_filtrace_*) — NErizena.
- TC bazenu (switch.bazen_tepelne_cerpadlo) smi bezet JEN kdyz bezi filtrace
  (bez prutoku TC fyzicky nenabehne, ale relé drzime vypnute pro jistotu).
- ON:  vyroba FVE > FVE_ON_W po ON_SUSTAIN_CYCLES cyklu (TC bere ~886 W,
  muze castecne jet i z baterky — proto vyroba, ne export).
- OFF: vyroba FVE < FVE_OFF_W po OFF_SUSTAIN_CYCLES cyklu (10 min).
- Ochrana kompresoru: min. doba behu/klidu MIN_ON_MIN / MIN_OFF_MIN.
- Master vypinac: input_boolean.bazen_tc_auto (off = app nic nedela, plna rucni kontrola).

Pozn.: Sonoff TC je napajeny AZ ZA POWR316 filtrace → bez filtrace je unavailable
(hardwarovy interlock). PowerOnState po rannim nahozeni resi automatika.
"""
import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime

TC_SWITCH = "switch.bazen_tepelne_cerpadlo"
FILTRACE_SWITCH = "switch.tasmota_bazen_cerpadlo"
AUTO_TOGGLE = "input_boolean.bazen_tc_auto"
FVE_SENSOR = "sensor.inverter_input_power"

FVE_ON_W = 2000         # vyroba pro zapnuti TC
FVE_OFF_W = 2000        # pokles vyroby pod → vypnout (po OFF_SUSTAIN_CYCLES)
ON_SUSTAIN_CYCLES = 3   # 3 x 60 s
OFF_SUSTAIN_CYCLES = 10  # 10 min pod prahem
MIN_ON_MIN = 10         # ochrana kompresoru
MIN_OFF_MIN = 10


class PoolHeatingManager(hass.Hass):

    def initialize(self):
        self._on_count = 0
        self._off_count = 0
        self._last_change = None
        self.run_every(self._cycle, "now+15", 60)
        self.log("PoolHeatingManager initialized (on: FVE>{}W {}min, off: FVE<{}W {}min)".format(
            FVE_ON_W, ON_SUSTAIN_CYCLES, FVE_OFF_W, OFF_SUSTAIN_CYCLES))

    def _f(self, entity_id, default=0.0):
        try:
            v = self.get_state(entity_id)
            return float(v) if v not in (None, "unavailable", "unknown") else default
        except (ValueError, TypeError):
            return default

    def _minutes_since_change(self):
        if self._last_change is None:
            return 999
        return (datetime.now() - self._last_change).total_seconds() / 60.0

    def _cycle(self, kwargs):
        try:
            self._do_cycle()
        except Exception as e:
            self.log("Cycle error: {}".format(e), level="ERROR")

    def _do_cycle(self):
        tc_on = self.get_state(TC_SWITCH) == "on"
        filtrace_on = self.get_state(FILTRACE_SWITCH) == "on"
        auto = self.get_state(AUTO_TOGGLE) == "on"
        fve = self._f(FVE_SENSOR)

        # Interlock plati VZDY (i v manualu): filtrace off => TC off
        if tc_on and not filtrace_on:
            self._switch(False, "Filtrace nebezi — interlock")
            self._publish(False, filtrace_on, auto, fve, "interlock")
            return

        if not auto:
            self._on_count = self._off_count = 0
            self._publish(tc_on, filtrace_on, auto, fve, "manualni rezim")
            return

        if not tc_on:
            # kandidat na zapnuti
            if filtrace_on and fve > FVE_ON_W:
                self._on_count += 1
            else:
                self._on_count = 0
            if (self._on_count >= ON_SUSTAIN_CYCLES
                    and self._minutes_since_change() >= MIN_OFF_MIN):
                self._switch(True, "Vyroba FVE {}W".format(int(fve)))
                self._on_count = 0
            self._publish(tc_on, filtrace_on, auto, fve,
                          "cekam na vyrobu ({}/{})".format(self._on_count, ON_SUSTAIN_CYCLES))
        else:
            # kandidat na vypnuti
            if fve < FVE_OFF_W:
                self._off_count += 1
            else:
                self._off_count = 0
            if (self._off_count >= OFF_SUSTAIN_CYCLES
                    and self._minutes_since_change() >= MIN_ON_MIN):
                self._switch(False, "Vyroba {}W pod {}W po {} min".format(
                    int(fve), FVE_OFF_W, OFF_SUSTAIN_CYCLES))
                self._off_count = 0
            self._publish(tc_on, filtrace_on, auto, fve,
                          "bezi ({}/{} k vypnuti)".format(self._off_count, OFF_SUSTAIN_CYCLES))

    def _switch(self, on, reason):
        self.call_service("switch/turn_{}".format("on" if on else "off"),
                          entity_id=TC_SWITCH)
        self._last_change = datetime.now()
        self.log("TC bazen {} — {}".format("ON" if on else "OFF", reason))

    def _publish(self, tc_on, filtrace_on, auto, fve, status):
        self.set_state("sensor.pool_heating_status",
                       state=status[:250],
                       attributes={
                           "friendly_name": "Bazen TC rizeni",
                           "icon": "mdi:pool",
                           "tc_on": "on" if tc_on else "off",
                           "filtrace_on": "on" if filtrace_on else "off",
                           "auto": "on" if auto else "off",
                           "fve_w": round(fve),
                           "updated": datetime.now().strftime("%H:%M:%S"),
                       })
