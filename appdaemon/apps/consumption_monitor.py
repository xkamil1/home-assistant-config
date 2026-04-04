import appdaemon.plugins.hass.hassapi as hass
import requests
import json
from datetime import datetime, timedelta, timezone

BOJLER_SPIRALA_W = 1500
DAIKIN_CYCLE_SEC = 300  # 5 min cycle for delta calculation
PHASE_IMBALANCE_ALERT_W = 2000
PHASE_C_OVERLOAD_W = 3000
ALERT_SUSTAINED_CYCLES = 2  # 2 x 5min = 10 min sustained


class ConsumptionMonitor(hass.Hass):

    def initialize(self):
        self._ha_url = self.args.get("ha_url", "http://10.0.0.67:8123")
        self._ha_token = self.args.get("ha_token")

        # InfluxDB v1
        self._influx_url = "http://{}:{}".format(
            self.args.get("influxdb_host", "a0d7b954-influxdb"),
            self.args.get("influxdb_port", 8086))
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_user = self.args.get("influxdb_user", "db_write")
        self._influx_pass = self.args.get("influxdb_password", "db_write_pass")
        self._influx_ok = False
        self._init_influxdb()

        # Daikin previous energy values for delta calculation
        self._daikin_prev = {}
        self._daikin_prev_time = None

        # Phase imbalance alert counter
        self._imbalance_alert_count = 0

        # Ensure helpers
        if self.get_state("input_boolean.phase_imbalance_alert") is None:
            self.set_state("input_boolean.phase_imbalance_alert", state="off",
                           attributes={"friendly_name": "Fazova nerovnovaha alert",
                                       "icon": "mdi:flash-alert"})

        # Schedule: every 5 minutes
        self.run_every(self._cycle, "now+10", 300)

        # Daily aggregation at 23:55
        self.run_daily(self._daily_aggregate, datetime.now().replace(
            hour=23, minute=55, second=0, microsecond=0))

        # Log summary every 30 min (cycle 6)
        self._cycle_count = 0

        self.log("ConsumptionMonitor initialized (influxdb={})".format(
            "OK" if self._influx_ok else "UNAVAILABLE"))

    # ── InfluxDB v1 ────────────────────────────────────────────────────────

    def _init_influxdb(self):
        try:
            resp = requests.get("{}/ping".format(self._influx_url), timeout=5)
            if resp.status_code == 204:
                self._influx_ok = True
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

    # ── Helpers ────────────────────────────────────────────────────────────

    def _f(self, entity_id, default=0.0):
        try:
            v = self.get_state(entity_id)
            return float(v) if v not in (None, "unavailable", "unknown") else default
        except (ValueError, TypeError):
            return default

    def _daikin_estimated_w(self):
        """Estimate Daikin power from energy delta over 5 minutes."""
        rooms = {
            "adela": [
                "sensor.adela_pokoj_climatecontrol_heating_daily_electrical_consumption",
                "sensor.adela_pokoj_climatecontrol_cooling_daily_electrical_consumption",
            ],
            "nela": [
                "sensor.nela_pokoj_climatecontrol_heating_daily_electrical_consumption",
                "sensor.nela_pokoj_climatecontrol_cooling_daily_electrical_consumption",
            ],
            "pracovna": [
                "sensor.pracovna_climatecontrol_heating_daily_electrical_consumption",
                "sensor.pracovna_climatecontrol_cooling_daily_electrical_consumption",
            ],
            "loznice": [
                "sensor.loznice_climatecontrol_heating_daily_electrical_consumption",
                "sensor.loznice_climatecontrol_cooling_daily_electrical_consumption",
            ],
        }

        now = datetime.now()
        current = {}
        for room, entities in rooms.items():
            total = 0.0
            for eid in entities:
                total += self._f(eid)
            current[room] = total

        watts = {}
        if self._daikin_prev and self._daikin_prev_time:
            elapsed_h = (now - self._daikin_prev_time).total_seconds() / 3600.0
            if elapsed_h > 0:
                for room in rooms:
                    delta_kwh = current[room] - self._daikin_prev.get(room, current[room])
                    if delta_kwh < 0:
                        delta_kwh = 0  # midnight reset
                    watts[room] = round(delta_kwh / elapsed_h * 1000)
        else:
            for room in rooms:
                watts[room] = 0

        self._daikin_prev = current
        self._daikin_prev_time = now
        return watts

    # ── Main cycle (every 5 min) ───────────────────────────────────────────

    def _cycle(self, kwargs):
        try:
            self._do_cycle()
        except Exception as e:
            self.log("Cycle error: {}".format(e), level="ERROR")

    def _do_cycle(self):
        self._cycle_count += 1

        # ── Read all sensors ──────────────────────────────────────────────

        # FVE / Battery / Grid
        fve_w = self._f("sensor.inverter_input_power")
        battery_w = self._f("sensor.battery_charge_discharge_power")
        grid_w = self._f("sensor.power_meter_active_power")

        # Home consumption: FVE - grid_export - battery_charge
        # grid: positive=export, negative=import
        # battery: positive=charging, negative=discharging
        home_total_w = fve_w - grid_w - battery_w
        home_total_w = max(0, home_total_w)

        # Phase power (from grid meter — negative=import, positive=export)
        phase_a_grid = self._f("sensor.power_meter_phase_a_active_power")
        phase_b_grid = self._f("sensor.power_meter_phase_b_active_power")
        phase_c_grid = self._f("sensor.power_meter_phase_c_active_power")

        # TC (Shelly EM3 — direct measurement)
        tc_a = self._f("sensor.shellyem3_34945475ecce_channel_a_power")
        tc_b = self._f("sensor.shellyem3_34945475ecce_channel_b_power")
        tc_c = self._f("sensor.shellyem3_34945475ecce_channel_c_power")
        tc_total = tc_a + tc_b + tc_c

        # Bojler spirála
        bojler_active = self._f("sensor.bojler_aktivni")
        bojler_w = BOJLER_SPIRALA_W if bojler_active >= 1 else 0

        # Pračka + Sušička
        pracka_w = self._f("sensor.tz3000_hdopuwv6_ts011f_power")  # Zigbee zasuvka
        susicka_w = self._f("sensor.zasuvka_pracovna_u_dveri_power")  # Zigbee zasuvka
        mycka_w = self._f("sensor.zasuvka_mycka_power")  # Zigbee zasuvka

        # Daikin (estimated from kWh delta)
        daikin = self._daikin_estimated_w()
        daikin_adela = daikin.get("adela", 0)
        daikin_nela = daikin.get("nela", 0)
        daikin_pracovna = daikin.get("pracovna", 0)
        daikin_loznice = daikin.get("loznice", 0)
        daikin_total = daikin_adela + daikin_nela + daikin_pracovna + daikin_loznice

        # EV Charger (3-phase wallbox)
        ev_charger_kw = self._f("sensor.ev_charger_vykon")
        ev_charger_w = round(ev_charger_kw * 1000)
        ev_charger_phase_w = self._f("sensor.ev_charger_phase_power")

        # Zásuvky
        ups_w = self._f("sensor.zasuvka_kotelna_ups_active_power")
        tv_w = self._f("sensor.zasuvka_obyvak_tv_active_power_3")
        pracovna_z_w = self._f("sensor.zasuvka_pracovna_u_dveri_power")
        pergola_w = self._f("sensor.zasuvka_pergola_power")

        # ── Totals ────────────────────────────────────────────────────────

        tracked_total = (tc_total + bojler_w + pracka_w + susicka_w + mycka_w +
                         daikin_total + ev_charger_w +
                         ups_w + tv_w + pracovna_z_w + pergola_w)
        untracked_w = max(0, home_total_w - tracked_total)

        # Phase tracked consumption
        # EV charger is 3-phase balanced: per_phase ≈ total/3
        ev_per_phase = round(ev_charger_w / 3) if ev_charger_w > 0 else 0
        phase_a_tracked = tc_a + ev_per_phase
        phase_b_tracked = tc_b + bojler_w + ev_per_phase
        phase_c_tracked = tc_c + pracka_w + susicka_w + mycka_w + daikin_total + ev_per_phase
        phase_unknown = ups_w + tv_w + pracovna_z_w + pergola_w

        # Phase imbalance (grid import per phase — use absolute import values)
        # More negative = more import
        phase_imports = [abs(min(0, phase_a_grid)),
                         abs(min(0, phase_b_grid)),
                         abs(min(0, phase_c_grid))]
        imbalance = max(phase_imports) - min(phase_imports)

        most_loaded_idx = phase_imports.index(max(phase_imports))
        least_loaded_idx = phase_imports.index(min(phase_imports))
        phase_names = ["A", "B", "C"]

        # ── Phase imbalance alert ─────────────────────────────────────────

        alert_condition = (imbalance > PHASE_IMBALANCE_ALERT_W or
                           abs(min(0, phase_c_grid)) > PHASE_C_OVERLOAD_W)
        if alert_condition:
            self._imbalance_alert_count += 1
        else:
            self._imbalance_alert_count = 0

        alert_on = self._imbalance_alert_count >= ALERT_SUSTAINED_CYCLES
        try:
            current_alert = self.get_state("input_boolean.phase_imbalance_alert")
            if alert_on and current_alert != "on":
                self.set_state("input_boolean.phase_imbalance_alert", state="on")
                self.log("ALERT: Phase imbalance {}W sustained for {} cycles".format(
                    round(imbalance), self._imbalance_alert_count), level="WARNING")
            elif not alert_on and current_alert == "on":
                self.set_state("input_boolean.phase_imbalance_alert", state="off")
        except Exception:
            pass

        # ── Update HA sensors ─────────────────────────────────────────────

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        self.set_state("sensor.consumption_home_total", state=str(round(home_total_w)),
                       attributes={
                           "friendly_name": "Spotreba domu celkem",
                           "unit_of_measurement": "W",
                           "icon": "mdi:home-lightning-bolt",
                           "fve_w": round(fve_w),
                           "battery_w": round(battery_w),
                           "grid_w": round(grid_w),
                           "updated": now_str,
                       })

        self.set_state("sensor.consumption_tracked", state=str(round(tracked_total)),
                       attributes={
                           "friendly_name": "Sledovana spotreba",
                           "unit_of_measurement": "W",
                           "icon": "mdi:eye",
                           "tc_total_w": round(tc_total),
                           "tc_a_w": round(tc_a),
                           "tc_b_w": round(tc_b),
                           "tc_c_w": round(tc_c),
                           "bojler_spirala_w": bojler_w,
                           "pracka_w": round(pracka_w),
                           "susicka_w": round(susicka_w),
                           "mycka_w": round(mycka_w),
                           "daikin_total_w": round(daikin_total),
                           "daikin_adela_w": daikin_adela,
                           "daikin_nela_w": daikin_nela,
                           "daikin_pracovna_w": daikin_pracovna,
                           "daikin_loznice_w": daikin_loznice,
                           "ev_charger_w": ev_charger_w,
                           "ev_charger_phase_w": round(ev_charger_phase_w),
                           "ups_w": round(ups_w),
                           "tv_w": round(tv_w),
                           "pracovna_zasuvka_w": round(pracovna_z_w),
                           "pergola_w": round(pergola_w),
                           "updated": now_str,
                       })

        untracked_pct = round(untracked_w / home_total_w * 100) if home_total_w > 0 else 1
        self.set_state("sensor.consumption_untracked",
                       state=str(round(untracked_w)),
                       attributes={
                           "friendly_name": "Nesledovana spotreba",
                           "unit_of_measurement": "W",
                           "icon": "mdi:eye-off",
                           "percentage": str(untracked_pct),
                           "updated": now_str,
                       })

        self.set_state("sensor.consumption_tc_total", state=str(round(tc_total)),
                       attributes={
                           "friendly_name": "TC spotreba",
                           "unit_of_measurement": "W",
                           "icon": "mdi:heat-pump",
                           "channel_a": round(tc_a),
                           "channel_b": round(tc_b),
                           "channel_c": round(tc_c),
                       })

        self.set_state("sensor.consumption_daikin_total",
                       state=str(round(daikin_total)),
                       attributes={
                           "friendly_name": "Daikin spotreba (odhad)",
                           "unit_of_measurement": "W",
                           "icon": "mdi:air-conditioner",
                           "adela_w": daikin_adela,
                           "nela_w": daikin_nela,
                           "pracovna_w": daikin_pracovna,
                           "loznice_w": daikin_loznice,
                       })

        self.set_state("sensor.consumption_phase_imbalance",
                       state=str(round(imbalance)),
                       attributes={
                           "friendly_name": "Fazova nerovnovaha",
                           "unit_of_measurement": "W",
                           "icon": "mdi:scale-unbalanced",
                           "phase_a_w": round(phase_a_grid),
                           "phase_b_w": round(phase_b_grid),
                           "phase_c_w": round(phase_c_grid),
                           "phase_a_import_w": round(phase_imports[0]),
                           "phase_b_import_w": round(phase_imports[1]),
                           "phase_c_import_w": round(phase_imports[2]),
                           "most_loaded": phase_names[most_loaded_idx],
                           "least_loaded": phase_names[least_loaded_idx],
                           "alert": alert_on,
                           "updated": now_str,
                       })

        # ── Write to InfluxDB ─────────────────────────────────────────────

        fields = (
            "home_total_w={htw},fve_w={fve},battery_w={bat},grid_w={grid},"
            "tc_total_w={tct},tc_a_w={tca},tc_b_w={tcb},tc_c_w={tcc},"
            "pracka_w={pr},susicka_w={su},mycka_w={my},"
            "daikin_adela_w={da},daikin_nela_w={dn},daikin_pracovna_w={dp},"
            "daikin_loznice_w={dl},daikin_total_w={dt},"
            "bojler_spirala_w={boj},ev_charger_w={evc},"
            "ups_w={ups},tv_w={tv},pracovna_zasuvka_w={pz},pergola_w={per},"
            "tracked_total_w={trk},untracked_w={unt},"
            "phase_a_grid_w={pa},phase_b_grid_w={pb},phase_c_grid_w={pc},"
            "phase_a_tracked_w={pat},phase_b_tracked_w={pbt},phase_c_tracked_w={pct},"
            "phase_unknown_w={pu},phase_imbalance_w={imb}"
        ).format(
            htw=round(home_total_w), fve=round(fve_w), bat=round(battery_w),
            grid=round(grid_w),
            tct=round(tc_total), tca=round(tc_a), tcb=round(tc_b), tcc=round(tc_c),
            pr=round(pracka_w), su=round(susicka_w), my=round(mycka_w),
            da=daikin_adela, dn=daikin_nela, dp=daikin_pracovna, dl=daikin_loznice,
            dt=round(daikin_total),
            boj=bojler_w, evc=ev_charger_w,
            ups=round(ups_w), tv=round(tv_w), pz=round(pracovna_z_w),
            per=round(pergola_w),
            trk=round(tracked_total), unt=round(untracked_w),
            pa=round(phase_a_grid), pb=round(phase_b_grid), pc=round(phase_c_grid),
            pat=round(phase_a_tracked), pbt=round(phase_b_tracked),
            pct=round(phase_c_tracked),
            pu=round(phase_unknown), imb=round(imbalance),
        )
        self._influx_write("consumption_breakdown {}".format(fields))

        # ── Log summary every 30 min ──────────────────────────────────────

        if self._cycle_count % 6 == 1:
            self.log(
                "Home={:.0f}W (FVE={:.0f} Grid={:.0f} Bat={:.0f}) | "
                "TC={:.0f} Boj={} Pr={:.0f} Su={:.0f} Dai={:.0f} EV={:.0f} | "
                "Tracked={:.0f} Untracked={:.0f} ({}%) | "
                "PhA={:.0f} PhB={:.0f} PhC={:.0f} Imb={:.0f}W".format(
                    home_total_w, fve_w, grid_w, battery_w,
                    tc_total, bojler_w, pracka_w, susicka_w, daikin_total,
                    ev_charger_w,
                    tracked_total, untracked_w, untracked_pct,
                    phase_a_grid, phase_b_grid, phase_c_grid, imbalance))

    # ── Daily aggregate (23:55) ────────────────────────────────────────────

    def _daily_aggregate(self, kwargs):
        try:
            self._do_daily_aggregate()
        except Exception as e:
            self.log("Daily aggregate error: {}".format(e), level="ERROR")

    def _do_daily_aggregate(self):
        if not self._influx_ok:
            return

        # Query today's consumption_breakdown averages
        q = ('SELECT MEAN(home_total_w) AS home, MEAN(tc_total_w) AS tc, '
             'MEAN(bojler_spirala_w) AS boj, MEAN(pracka_w) AS pr, '
             'MEAN(susicka_w) AS su, MEAN(daikin_total_w) AS dai, '
             'MEAN(ev_charger_w) AS evc, '
             'MEAN(ups_w) AS ups, MEAN(tv_w) AS tv, '
             'MEAN(tracked_total_w) AS trk, MEAN(untracked_w) AS unt, '
             'MEAN(phase_imbalance_w) AS imb, MEAN(fve_w) AS fve, '
             'MEAN(grid_w) AS grid, MEAN(battery_w) AS bat '
             'FROM consumption_breakdown WHERE time > now() - 24h')
        series = self._influx_query(q)

        if not series:
            self.log("No consumption data for daily aggregate")
            return

        row = series[0].get("values", [[]])[0]
        cols = series[0].get("columns", [])
        data = dict(zip(cols, row))

        # Convert average W to kWh (24 hours)
        def to_kwh(w_val):
            return round((w_val or 0) * 24 / 1000, 2)

        # Daikin: use actual daily sensor values (more accurate)
        daikin_kwh = 0
        for room in ["adela_pokoj", "nela_pokoj", "pracovna", "loznice"]:
            for mode in ["heating", "cooling"]:
                eid = "sensor.{}_climatecontrol_{}_daily_electrical_consumption".format(
                    room, mode)
                daikin_kwh += self._f(eid)
        daikin_kwh = round(daikin_kwh, 2)

        date_str = datetime.now().strftime("%Y-%m-%d")
        fields = (
            'home_kwh={home},tc_kwh={tc},bojler_kwh={boj},'
            'pracka_kwh={pr},susicka_kwh={su},'
            'daikin_kwh={dai},daikin_sensor_kwh={dais},'
            'ev_charger_kwh={evc},'
            'ups_kwh={ups},tv_kwh={tv},'
            'tracked_kwh={trk},untracked_kwh={unt},'
            'fve_kwh={fve},grid_kwh={grid},battery_kwh={bat},'
            'avg_imbalance_w={imb},date="{date}"'
        ).format(
            home=to_kwh(data.get("home")),
            tc=to_kwh(data.get("tc")),
            boj=to_kwh(data.get("boj")),
            pr=to_kwh(data.get("pr")),
            su=to_kwh(data.get("su")),
            dai=to_kwh(data.get("dai")),
            dais=daikin_kwh,
            evc=to_kwh(data.get("evc")),
            ups=to_kwh(data.get("ups")),
            tv=to_kwh(data.get("tv")),
            trk=to_kwh(data.get("trk")),
            unt=to_kwh(data.get("unt")),
            fve=to_kwh(data.get("fve")),
            grid=to_kwh(data.get("grid")),
            bat=to_kwh(data.get("bat")),
            imb=round(data.get("imb") or 0),
            date=date_str,
        )

        if self._influx_write("consumption_daily {}".format(fields)):
            self.log("Daily aggregate saved for {}: home={:.1f}kWh tc={:.1f}kWh "
                     "daikin={:.1f}kWh untracked={:.1f}kWh".format(
                         date_str, to_kwh(data.get("home")), to_kwh(data.get("tc")),
                         daikin_kwh, to_kwh(data.get("unt"))))
