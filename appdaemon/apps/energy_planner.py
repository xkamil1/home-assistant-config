import appdaemon.plugins.hass.hassapi as hass
import requests
import json
import re
from datetime import datetime, timedelta, timezone

# Elroq parameters
EV_BATTERY_KWH = 77.0       # usable capacity
EV_SOC_PER_KM_DEFAULT = 0.20  # fallback: 1 km ≈ 0.20% SOC (from 64%/313km real data)
HOME_DAILY_KWH = 30.0       # average daily home consumption
MIN_SOC_SAFETY = 20         # minimum safe SOC %
MIN_SOC_CHARGE_TO = 30      # charge to at least this if below safety
DEFAULT_TARGET_SOC = 80
MIN_SAMPLES_RELIABLE = 2    # min data points for reliable day-of-week average

DAYS_CZ = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]

# FVE production estimates by weather condition (kWh/day for 7.4 kWp system)
# Calibrated from real FVE data (Mar 2026, 7.4 kWp system)
CONDITION_KWH = {
    "sunny": 35, "clear-night": 0, "partlycloudy": 18, "cloudy": 5,
    "rainy": 3, "pouring": 2, "lightning": 2, "lightning-rainy": 2,
    "snowy": 2, "snowy-rainy": 2, "fog": 4, "windy": 25,
    "windy-variant": 25, "hail": 2,
}
SUNNY_THRESHOLD_KWH = 35   # above this = sunny day
SUNNY_CONFIDENCE = 70       # min solar_confidence for sunny classification
GRID_CHARGE_THRESHOLD = 25  # SOC % below which we charge from grid


class EnergyPlanner(hass.Hass):

    def initialize(self):
        self._ha_url = self.args.get("ha_url", "http://10.0.0.67:8123")
        self._ha_token = self.args.get("ha_token")
        self._api_key = self.args.get("anthropic_api_key")

        if not self._ha_token:
            self.log("ERROR: ha_token not configured", level="ERROR")
            return

        self._soc_per_km = EV_SOC_PER_KM_DEFAULT

        # InfluxDB v1
        self._influx_url = "http://{}:{}".format(
            self.args.get("influxdb_host", "a0d7b954-influxdb"),
            self.args.get("influxdb_port", 8086))
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_user = self.args.get("influxdb_user", "db_write")
        self._influx_pass = self.args.get("influxdb_password", "db_write_pass")
        self._influx_ok = False
        self._init_influxdb()

        # Ensure helper entities exist
        self._ensure_helpers()

        # Schedule: 23:00 record daily km, 23:05 plan
        self.run_daily(self._record_daily_km, datetime.now().replace(
            hour=23, minute=0, second=0, microsecond=0))
        self.run_daily(self._daily_plan, datetime.now().replace(
            hour=23, minute=5, second=0, microsecond=0))

        # Watch user requests
        self.listen_state(self._on_user_request, "input_text.energy_planner_user_request")

        # Watch confirm/reject buttons
        self.listen_state(self._on_confirm, "input_button.energy_planner_confirm")
        self.listen_state(self._on_reject, "input_button.energy_planner_reject")

        # Initial recommendation on startup
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

    def _escape_field_str(self, val):
        return str(val).replace('"', '\\"')

    # ── Helper entities ────────────────────────────────────────────────────

    def _ensure_helpers(self):
        # input_boolean.ev_nocharge_tonight
        if self.get_state("input_boolean.ev_nocharge_tonight") is None:
            self.set_state("input_boolean.ev_nocharge_tonight", state="off",
                           attributes={"friendly_name": "EV nenabíjet dnes v noci",
                                       "icon": "mdi:ev-station"})
            self.log("Created input_boolean.ev_nocharge_tonight")

        # input_number.ev_target_soc_tomorrow
        if self.get_state("input_number.ev_target_soc_tomorrow") is None:
            self.set_state("input_number.ev_target_soc_tomorrow",
                           state=str(DEFAULT_TARGET_SOC),
                           attributes={"friendly_name": "EV cílový SOC zítra",
                                       "icon": "mdi:battery-charging-80",
                                       "min": 20, "max": 100, "step": 5,
                                       "unit_of_measurement": "%"})
            self.log("Created input_number.ev_target_soc_tomorrow")

        # input_text.energy_planner_user_request
        if self.get_state("input_text.energy_planner_user_request") is None:
            self.set_state("input_text.energy_planner_user_request", state="",
                           attributes={"friendly_name": "Instrukce pro plánování",
                                       "icon": "mdi:chat-processing",
                                       "max": 255})
            self.log("Created input_text.energy_planner_user_request")

        # input_button confirm/reject
        for btn, name, icon in [
            ("input_button.energy_planner_confirm", "Potvrdit plán", "mdi:check-circle"),
            ("input_button.energy_planner_reject", "Zamítnout plán", "mdi:close-circle"),
        ]:
            if self.get_state(btn) is None:
                self.set_state(btn, state="unknown",
                               attributes={"friendly_name": name, "icon": icon})

        # input_boolean.energy_planner_weekend_target
        if self.get_state("input_boolean.energy_planner_weekend_target") is None:
            self.set_state("input_boolean.energy_planner_weekend_target", state="off",
                           attributes={"friendly_name": "EV vikendove nabijeni z FVE",
                                       "icon": "mdi:weather-sunny"})

    def _get_soc_per_km(self):
        """Calculate SOC%/km from current SOC and range. Falls back to default."""
        soc = self._float("sensor.skoda_elroq_battery_percentage")
        rng = self._float("sensor.skoda_elroq_range")
        if soc > 5 and rng > 20:
            return soc / rng
        return EV_SOC_PER_KM_DEFAULT

    # ── HA entity helpers ──────────────────────────────────────────────────

    def _float(self, entity_id, default=0.0):
        try:
            v = self.get_state(entity_id)
            return float(v) if v not in (None, "unavailable", "unknown") else default
        except (ValueError, TypeError):
            return default

    def _float_influx_last(self, measurement, entity_id, hours_back=48):
        """Get last value from InfluxDB for an entity."""
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
        """Set input_boolean or input_number via HA API."""
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
    # PART 1 — Daily km recording (23:00)
    # ══════════════════════════════════════════════════════════════════════

    def _record_daily_km(self, kwargs):
        try:
            self._do_record_km()
        except Exception as e:
            self.log("Record km error: {}".format(e), level="ERROR")

    def _do_record_km(self):
        # Current mileage from HA or InfluxDB
        current_mileage = self._float("sensor.skoda_elroq_mileage")
        if current_mileage <= 0:
            current_mileage = self._float_influx_last("km", "skoda_elroq_mileage", 24)
        if not current_mileage or current_mileage <= 0:
            self.log("Cannot read mileage, skipping km recording", level="WARNING")
            return

        # Previous mileage from our ev_daily_km measurement
        prev_mileage = None
        series = self._influx_query(
            "SELECT LAST(mileage_end) FROM ev_daily_km WHERE time > now() - 3d")
        for s in series:
            for row in s.get("values", []):
                if row[1] is not None:
                    prev_mileage = float(row[1])

        if prev_mileage is None:
            # First run — no previous data, store baseline
            self.log("First km recording, storing baseline mileage={}".format(
                int(current_mileage)))
            delta = 0
        else:
            delta = current_mileage - prev_mileage

        # Anomaly check
        if delta < 0 or delta > 800:
            self.log("Anomaly: delta={}km (prev={}, curr={}), storing 0".format(
                delta, prev_mileage, current_mileage), level="WARNING")
            delta = 0

        today = datetime.now()
        dow = today.weekday()  # 0=Monday
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
    # PART 2 — Behavior model (average km per day of week)
    # ══════════════════════════════════════════════════════════════════════

    def _get_km_model(self):
        """Return dict: {day_of_week: avg_km} and 3-day forecast."""
        model = {}
        all_km = []

        series = self._influx_query(
            "SELECT km_driven, day_of_week FROM ev_daily_km WHERE time > now() - 60d")

        day_data = {i: [] for i in range(7)}
        for s in series:
            cols = s.get("columns", [])
            for row in s.get("values", []):
                rd = dict(zip(cols, row))
                km = rd.get("km_driven")
                dow = rd.get("day_of_week")
                if km is not None and dow is not None:
                    km = float(km)
                    dow = int(float(dow))
                    day_data[dow].append(km)
                    all_km.append(km)

        overall_avg = round(sum(all_km) / len(all_km), 1) if all_km else 30.0

        for dow in range(7):
            samples = day_data[dow]
            if len(samples) >= MIN_SAMPLES_RELIABLE:
                model[dow] = round(sum(samples) / len(samples), 1)
            else:
                model[dow] = overall_avg

        # 3-day forecast
        tomorrow = datetime.now() + timedelta(days=1)
        forecast_3d = []
        for offset in range(3):
            day = tomorrow + timedelta(days=offset)
            dow = day.weekday()
            forecast_3d.append({
                "date": day.strftime("%Y-%m-%d"),
                "day_name": DAYS_CZ[dow],
                "day_of_week": dow,
                "expected_km": model[dow],
                "samples": len(day_data[dow]),
            })

        return model, forecast_3d, overall_avg, day_data

    # ══════════════════════════════════════════════════════════════════════
    # PART 2b — 5-day FVE outlook
    # ══════════════════════════════════════════════════════════════════════

    def _get_5day_outlook(self):
        """Get 5-day solar/weather outlook for charging strategy."""
        outlook = []
        tomorrow = datetime.now() + timedelta(days=1)

        # Get daily forecast from Met.no
        daily_forecast = self._get_ha_forecast("weather.forecast_home", "daily")

        # Forecast Solar for tomorrow (day 1)
        fs_tomorrow = self._float("sensor.energy_production_tomorrow")
        fs_corr = 1.0
        try:
            attrs = self.get_state("sensor.solar_confidence_now", attribute="all")
            if attrs:
                fs_corr = float((attrs.get("attributes") or {}).get(
                    "forecast_solar_correction", 1.0))
        except Exception:
            pass

        # solar_confidence_tomorrow for day 1
        sc_tomorrow = self._float("sensor.solar_confidence_tomorrow")

        # Build per-forecast-entry lookup: date -> condition
        fc_by_date = {}
        for f in daily_forecast:
            dt_str = f.get("datetime", "")
            if dt_str:
                try:
                    s = dt_str.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(s)
                    local = dt.astimezone().replace(tzinfo=None)
                    fc_by_date[local.date()] = f.get("condition", "cloudy")
                except Exception:
                    pass

        # Behavior model for km
        model, _, overall_avg, _ = self._get_km_model()

        # Get current EV SOC for trajectory calculation
        ev_soc = self._float("sensor.skoda_elroq_battery_percentage")
        if ev_soc <= 0:
            val = self._float_influx_last("%", "skoda_elroq_battery_percentage", 24)
            ev_soc = val if val else 50
        running_soc = ev_soc

        for offset in range(5):
            day = tomorrow + timedelta(days=offset)
            day_date = day.date()
            dow = day.weekday()
            is_weekend = dow >= 5
            km_expected = model.get(dow, overall_avg)

            if offset == 0:
                # Day 1: use Forecast Solar + solar_confidence_tomorrow
                expected_kwh = fs_tomorrow * fs_corr
                solar_conf = sc_tomorrow
            else:
                # Days 2-5: estimate from weather condition
                condition = fc_by_date.get(day_date, "cloudy")
                base_kwh = CONDITION_KWH.get(condition, 12)
                # Reduce confidence for further days
                solar_conf = max(20, 80 - offset * 10)
                expected_kwh = base_kwh * (solar_conf / 100.0)

            surplus = max(0, expected_kwh - HOME_DAILY_KWH)
            is_sunny = solar_conf >= SUNNY_CONFIDENCE and expected_kwh >= SUNNY_THRESHOLD_KWH

            # Running SOC trajectory
            running_soc -= km_expected * self._soc_per_km
            if surplus > 0:
                fve_charge = (surplus / EV_BATTERY_KWH) * 100
                running_soc = min(100, running_soc + fve_charge)
            soc_after = round(running_soc, 1)

            # Charge needed classification
            if soc_after < 20:
                charge_needed = "critical"
            elif soc_after < 30 and surplus < 5:
                charge_needed = "recommended"
            elif soc_after < 40 and surplus < 5:
                charge_needed = "optional"
            else:
                charge_needed = "no"

            outlook.append({
                "date": day.strftime("%Y-%m-%d"),
                "day_name": DAYS_CZ[dow],
                "day_of_week": dow,
                "solar_confidence": round(solar_conf),
                "expected_kwh": round(expected_kwh, 1),
                "surplus_for_ev_kwh": str(round(surplus, 1)),
                "is_weekend": "yes" if is_weekend else "no",
                "is_sunny": "yes" if is_sunny else "no",
                "km_expected": round(km_expected, 1),
                "condition": fc_by_date.get(day_date, "unknown"),
                "soc_after": str(soc_after),
                "charge_needed": charge_needed,
            })

        return outlook

    def _get_ha_forecast(self, entity_id, ftype):
        """Fetch forecast from HA weather service."""
        try:
            resp = requests.post(
                "{}/api/services/weather/get_forecasts?return_response".format(
                    self._ha_url),
                headers={
                    "Authorization": "Bearer {}".format(self._ha_token),
                    "Content-Type": "application/json",
                },
                json={"entity_id": entity_id, "type": ftype},
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("service_response", {}).get(
                    entity_id, {}).get("forecast", [])
        except Exception as e:
            self.log("Forecast error: {}".format(e), level="WARNING")
        return []

    def _pick_strategy(self, ev_soc, outlook, km_model_avg):
        """Pick multi-day charging strategy based on 5-day outlook."""
        # Find nearest sunny window
        sunny_window = None
        weekend_window = None
        for i, day in enumerate(outlook):
            if day["is_sunny"] == "yes" and sunny_window is None:
                sunny_window = i
            if day["is_weekend"] == "yes" and day["solar_confidence"] > 60 and weekend_window is None:
                weekend_window = i

        total_surplus = sum(float(d.get("surplus_for_ev_kwh", 0)) for d in outlook)

        # Calculate cumulative SOC drain over gap days
        running_soc = ev_soc
        soc_trajectory = []
        for day in outlook:
            running_soc -= day["km_expected"] * self._soc_per_km
            # Add FVE charge during sunny days
            surplus_val = float(day.get("surplus_for_ev_kwh", 0))
            if surplus_val > 0:
                fve_soc = (surplus_val / EV_BATTERY_KWH) * 100
                running_soc = min(100, running_soc + fve_soc)
            soc_trajectory.append(round(running_soc, 1))

        result = {
            "sunny_window_day": sunny_window,
            "sunny_window_date": outlook[sunny_window]["date"] if sunny_window is not None else "none",
            "weekend_window_day": weekend_window,
            "weekend_window_date": outlook[weekend_window]["date"] if weekend_window is not None else "none",
            "total_surplus_5day": str(round(total_surplus, 1)),
            "soc_trajectory": soc_trajectory,
            "min_soc_5day": str(round(min(soc_trajectory), 1)) if soc_trajectory else str(round(ev_soc, 1)),
        }

        # STRATEGY 1: Sunny window tomorrow or day after
        if sunny_window is not None and sunny_window <= 1:
            surplus_kwh = float(outlook[sunny_window].get("surplus_for_ev_kwh", 0))
            if ev_soc - (outlook[0]["km_expected"] * self._soc_per_km) > GRID_CHARGE_THRESHOLD:
                result["strategy"] = "wait_for_fve"
                result["reason"] = (
                    "Cekam na FVE — {} ocekavam {:.0f} kWh prebytku.".format(
                        "zitra" if sunny_window == 0 else "pozitri",
                        surplus_kwh))
                result["nocharge"] = True
                result["target"] = DEFAULT_TARGET_SOC
                return result

        # STRATEGY 2: Sunny window in 3-5 days, gap days between
        if sunny_window is not None and sunny_window >= 2:
            gap_km = sum(outlook[i]["km_expected"] for i in range(sunny_window))
            gap_soc = gap_km * self._soc_per_km
            soc_at_sunny = ev_soc - gap_soc
            if soc_at_sunny < GRID_CHARGE_THRESHOLD:
                charge_to = min(60, int(gap_soc + 10))
                charge_to = max(MIN_SOC_CHARGE_TO, charge_to)
                result["strategy"] = "charge_for_gap"
                result["reason"] = (
                    "Nabijim na {}% — slunecno az {} ({}), "
                    "mezitim ~{:.0f} km.".format(
                        charge_to, outlook[sunny_window]["day_name"],
                        outlook[sunny_window]["date"],
                        gap_km))
                result["nocharge"] = False
                result["target"] = charge_to
                return result

        # STRATEGY 3: Weekend sunny window
        if weekend_window is not None and weekend_window <= 4:
            gap_km = sum(outlook[i]["km_expected"] for i in range(weekend_window))
            soc_at_weekend = ev_soc - (gap_km * self._soc_per_km)
            if soc_at_weekend > GRID_CHARGE_THRESHOLD:
                result["strategy"] = "wait_for_weekend"
                result["reason"] = (
                    "Rezervuji nabijeni na vikend — {} forecast {:.0f} kWh.".format(
                        outlook[weekend_window]["day_name"],
                        outlook[weekend_window]["expected_kwh"]))
                result["nocharge"] = True
                result["target"] = DEFAULT_TARGET_SOC
                return result

        # STRATEGY 4: No sunny window — fall through to daily logic
        result["strategy"] = "no_sun_window"
        result["reason"] = "Zadne slunecne okno v 5 dnech."
        result["nocharge"] = None  # let daily logic decide
        result["target"] = None
        return result

    # ══════════════════════════════════════════════════════════════════════
    # PART 3 — Daily planning (23:05)
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
        # Calculate SOC/km from current vehicle data
        self._soc_per_km = self._get_soc_per_km()

        # Check for active override
        override = self.get_state("sensor.energy_planner_active_override",
                                  attribute="all")
        if override and override.get("state") not in (None, "unknown", "unavailable", ""):
            valid_until = (override.get("attributes") or {}).get("valid_until", "")
            if valid_until:
                try:
                    vu_date = datetime.strptime(valid_until, "%Y-%m-%d").date()
                    if vu_date >= datetime.now().date():
                        self.log("Active override until {}, skipping auto-plan".format(
                            valid_until))
                        self._update_recommendation(
                            state="Override aktivní",
                            override_active=True,
                            override_until=valid_until)
                        return
                except ValueError:
                    pass

        # Collect inputs
        ev_soc = self._float("sensor.skoda_elroq_battery_percentage")
        if ev_soc <= 0:
            val = self._float_influx_last("%", "skoda_elroq_battery_percentage", 24)
            ev_soc = val if val else 0

        ev_range = self._float("sensor.skoda_elroq_range")
        if ev_range <= 0:
            val = self._float_influx_last("km", "skoda_elroq_range", 24)
            ev_range = val if val else 0

        solar_conf = self._float("sensor.solar_confidence_tomorrow")
        fve_kwh_raw = self._float("sensor.energy_production_tomorrow")

        # Apply FS correction from solar_confidence
        fs_corr = 1.0
        try:
            attrs = self.get_state("sensor.solar_confidence_now", attribute="all")
            if attrs:
                fs_corr = float((attrs.get("attributes") or {}).get(
                    "forecast_solar_correction", 1.0))
        except Exception:
            pass
        fve_kwh = fve_kwh_raw * fs_corr

        # Behavior model
        model, forecast_3d, overall_avg, _ = self._get_km_model()
        km_tomorrow = forecast_3d[0]["expected_km"] if forecast_3d else overall_avg
        km_day2 = forecast_3d[1]["expected_km"] if len(forecast_3d) > 1 else overall_avg
        km_day3 = forecast_3d[2]["expected_km"] if len(forecast_3d) > 2 else overall_avg

        # Calculations
        soc_after_tomorrow = ev_soc - (km_tomorrow * self._soc_per_km)
        fve_surplus = max(0, fve_kwh - HOME_DAILY_KWH)
        ev_charge_from_fve_kwh = fve_surplus * (solar_conf / 100.0)
        ev_charge_from_fve_soc = (ev_charge_from_fve_kwh / EV_BATTERY_KWH) * 100

        # 5-day outlook and multi-day strategy
        try:
            outlook = self._get_5day_outlook()
            strategy = self._pick_strategy(ev_soc, outlook, overall_avg)
        except Exception as e:
            self.log("5-day strategy error: {}, falling back to daily".format(e),
                     level="WARNING")
            outlook = []
            strategy = {"strategy": "no_sun_window", "nocharge": None, "target": None,
                        "reason": "Chyba vyhledu", "sunny_window_date": "none",
                        "weekend_window_date": "none", "total_surplus_5day": "0",
                        "soc_trajectory": [], "min_soc_5day": str(round(soc_after_tomorrow, 1))}

        strat_name = strategy.get("strategy", "no_sun_window")

        # Multi-day strategy overrides daily logic (unless critical SOC)
        if soc_after_tomorrow <= MIN_SOC_SAFETY:
            # CRITICAL: always charge regardless of strategy
            target = max(MIN_SOC_CHARGE_TO, int(km_tomorrow * self._soc_per_km) + 25)
            target = min(100, target)
            decision = "Nabíjet dnes v noci"
            reason = ("Varování: po zítřejších ~{:.0f}km zbude jen {:.0f}% SOC. "
                      "Nabíjím ze sítě na {}%.".format(
                          km_tomorrow, soc_after_tomorrow, target))
            nocharge = False
            strat_name = "critical_soc"

        elif strat_name in ("wait_for_fve", "wait_for_weekend") and strategy.get("nocharge"):
            # Strategy says wait for sun — don't charge from grid
            decision = strategy["reason"]
            reason = strategy["reason"]
            target = DEFAULT_TARGET_SOC
            nocharge = True

        elif strat_name == "charge_for_gap" and not strategy.get("nocharge"):
            # Strategy says charge for gap period
            target = strategy.get("target", MIN_SOC_CHARGE_TO)
            decision = "Nabíjet dnes v noci"
            reason = strategy["reason"]
            nocharge = False

        elif soc_after_tomorrow <= GRID_CHARGE_THRESHOLD:
            # Daily: Low SOC after driving
            soc_needed = int(km_tomorrow * self._soc_per_km) + 10
            target = max(MIN_SOC_CHARGE_TO, soc_needed)
            target = min(100, target)
            decision = "Nabíjet dnes v noci"
            reason = ("Zítra ~{:.0f}km, zbude jen {:.0f}% SOC (pod {}%). "
                      "Nabíjím ze sítě na {}%.".format(
                          km_tomorrow, soc_after_tomorrow,
                          GRID_CHARGE_THRESHOLD, target))
            nocharge = False

        else:
            # SOC sufficient, no multi-day concern
            decision = "Nenabíjet — SOC dostatečný"
            fve_note = ""
            if ev_charge_from_fve_soc > 1:
                fve_note = (" FVE přebytek ~{:.1f}kWh (+{:.0f}% SOC) "
                            "může doplnit přes den.".format(
                                ev_charge_from_fve_kwh, ev_charge_from_fve_soc))
            reason = ("Zítra ~{:.0f}km, zbude {:.0f}% SOC (nad {}%). "
                      "Nabíjení ze sítě není potřeba.{}".format(
                          km_tomorrow, soc_after_tomorrow,
                          GRID_CHARGE_THRESHOLD, fve_note))
            target = DEFAULT_TARGET_SOC
            nocharge = True

        # Weekend target flag
        weekend_target = strat_name == "wait_for_weekend"
        try:
            self._set_ha_input("input_boolean.energy_planner_weekend_target",
                               weekend_target)
        except Exception:
            pass

        # Apply
        self._set_ha_input("input_boolean.ev_nocharge_tonight", nocharge)
        self._set_ha_input("input_number.ev_target_soc_tomorrow", target)

        # Update recommendation sensor
        self._update_recommendation(
            state=decision,
            ev_soc_now=round(ev_soc, 1),
            ev_range_now=round(ev_range, 1),
            km_tomorrow=km_tomorrow,
            km_day2=km_day2,
            km_day3=km_day3,
            soc_after_tomorrow=round(soc_after_tomorrow, 1),
            fve_surplus_tomorrow_kwh=round(ev_charge_from_fve_kwh, 1),
            solar_confidence_tomorrow=round(solar_conf),
            fve_production_tomorrow_kwh=round(fve_kwh, 1),
            target_soc=target,
            nocharge_tonight=nocharge,
            decision=decision,
            reason=reason,
            strategy=strat_name,
            outlook_5day=outlook,
            sunny_window_date=strategy.get("sunny_window_date"),
            weekend_window_date=strategy.get("weekend_window_date"),
            total_surplus_5day=strategy.get("total_surplus_5day", 0),
            min_soc_5day=strategy.get("min_soc_5day", soc_after_tomorrow))

        prefix = "Startup" if startup else "Daily"
        self.log("{} plan: {} | SOC={}% range={}km | km_tom={} | "
                 "fve_surplus={:.1f}kWh | target={}% nocharge={}".format(
                     prefix, decision, round(ev_soc), round(ev_range),
                     km_tomorrow, ev_charge_from_fve_kwh, target, nocharge))

    def _update_recommendation(self, state, **attrs):
        attrs.setdefault("active_override", False)
        attrs.setdefault("override_until", None)
        attrs["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        attrs["friendly_name"] = "Energy Planner doporučení"
        attrs["icon"] = "mdi:car-electric-outline"
        self.set_state("sensor.energy_planner_recommendation",
                       state=state, attributes=attrs)

    # ══════════════════════════════════════════════════════════════════════
    # PART 4 — Interactive mode (user requests via Haiku)
    # ══════════════════════════════════════════════════════════════════════

    def _on_user_request(self, entity, attribute, old, new, kwargs):
        if not new or new == old or new.strip() == "":
            return
        try:
            self._process_user_request(new.strip())
        except Exception as e:
            self.log("User request error: {}".format(e), level="ERROR")
            self.set_state("sensor.energy_planner_pending",
                           state="error",
                           attributes={"friendly_name": "Planner čekající akce",
                                       "error": str(e)})

    def _process_user_request(self, user_text):
        self.log("User request: '{}'".format(user_text))

        if not self._api_key:
            self.log("No API key for Haiku, cannot process request", level="ERROR")
            return

        # Collect context
        ev_soc = self._float("sensor.skoda_elroq_battery_percentage")
        ev_range = self._float("sensor.skoda_elroq_range")

        try:
            _, forecast_3d, overall_avg, _ = self._get_km_model()
            km_tomorrow = forecast_3d[0]["expected_km"] if forecast_3d else overall_avg
        except Exception as e:
            self.log("km_model error: {}".format(e), level="WARNING")
            km_tomorrow = 30.0

        system_prompt = (
            "Jsi asistent pro plánování nabíjení elektromobilu Škoda Elroq. "
            "Uživatel zadal instrukci v přirozeném jazyce. Tvým úkolem je ji interpretovat "
            "a vrátit POUZE validní JSON (bez markdown, bez vysvětlení) s těmito poli:\n"
            '{{\n'
            '  "understood": true/false,\n'
            '  "summary_cz": "Co jsem pochopil: ...",\n'
            '  "action": "charge_tonight" / "skip_tonight" / "set_target_soc" / "multi_day" / "unknown",\n'
            '  "target_soc": 0-100 nebo null,\n'
            '  "valid_days": 1-7 nebo null,\n'
            '  "confirmation_prompt": "Mám udělat: ... Potvrdit?"\n'
            '}}\n'
            "Aktuální stav: SOC={}%, dojezd={}km, zítra očekáváno {}km.".format(
                int(ev_soc), int(ev_range), int(km_tomorrow)))

        try:
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 300,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_text}],
                },
                timeout=30,
            )
        except Exception as e:
            self.log("Haiku API error: {}".format(e), level="ERROR")
            return

        if response.status_code != 200:
            self.log("Haiku HTTP {}: {}".format(
                response.status_code, response.text[:300]), level="ERROR")
            return

        try:
            resp_json = response.json()
            raw = resp_json["content"][0]["text"].strip()
        except Exception as e:
            self.log("Cannot read Haiku response: {} | body={}".format(
                e, response.text[:300]), level="ERROR")
            return

        self.log("Haiku response: {}".format(raw))

        # Parse JSON — handle markdown code blocks
        clean = raw
        if "```" in clean:
            match_block = re.search(r'```(?:json)?\s*(.*?)```', clean, re.DOTALL)
            if match_block:
                clean = match_block.group(1).strip()

        try:
            result = json.loads(clean)
        except json.JSONDecodeError:
            match = re.search(r'\{[^{}]*\}', clean, re.DOTALL)
            if match:
                try:
                    result = json.loads(match.group())
                except json.JSONDecodeError as e2:
                    self.log("Cannot parse extracted JSON: {}".format(e2),
                             level="WARNING")
                    return
            else:
                self.log("No JSON found in Haiku response: {}".format(
                    clean[:200]), level="WARNING")
                return

        if not result.get("understood", False):
            self.set_state("sensor.energy_planner_pending",
                           state="nerozpoznáno",
                           attributes={
                               "friendly_name": "Planner čekající akce",
                               "icon": "mdi:help-circle",
                               "original_request": user_text,
                               "summary": result.get("summary_cz",
                                                      "Nerozuměl jsem instrukci."),
                               "confirmation_prompt": "Zkus instrukci přeformulovat.",
                               "action": "unknown",
                               "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                           })
            return

        # Store pending action
        self.set_state("sensor.energy_planner_pending",
                       state="waiting_confirmation",
                       attributes={
                           "friendly_name": "Planner čekající akce",
                           "icon": "mdi:clock-check-outline",
                           "original_request": user_text,
                           "summary": result.get("summary_cz", ""),
                           "confirmation_prompt": result.get("confirmation_prompt", ""),
                           "action": result.get("action", "unknown"),
                           "target_soc": result.get("target_soc"),
                           "valid_days": result.get("valid_days"),
                           "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       })
        self.log("Pending action: {} (target_soc={}, days={})".format(
            result.get("action"), result.get("target_soc"), result.get("valid_days")))

    def _on_confirm(self, entity, attribute, old, new, kwargs):
        try:
            self._apply_pending()
        except Exception as e:
            self.log("Confirm error: {}".format(e), level="ERROR")

    def _on_reject(self, entity, attribute, old, new, kwargs):
        self.log("User rejected pending action")
        self.set_state("sensor.energy_planner_pending", state="rejected",
                       attributes={"friendly_name": "Planner čekající akce",
                                   "icon": "mdi:close-circle"})

    def _apply_pending(self):
        pending = self.get_state("sensor.energy_planner_pending", attribute="all")
        if not pending or pending.get("state") != "waiting_confirmation":
            self.log("No pending action to confirm")
            return

        attrs = pending.get("attributes", {})
        action = attrs.get("action", "unknown")
        target_soc = attrs.get("target_soc")
        valid_days = attrs.get("valid_days", 1) or 1
        original = attrs.get("original_request", "")
        summary = attrs.get("summary", "")

        valid_until = (datetime.now() + timedelta(days=valid_days)).strftime("%Y-%m-%d")

        if action == "charge_tonight":
            self._set_ha_input("input_boolean.ev_nocharge_tonight", False)
            if target_soc:
                self._set_ha_input("input_number.ev_target_soc_tomorrow",
                                   min(100, max(20, int(target_soc))))
            desc = "Nabíjet dnes v noci" + (
                " na {}%".format(int(target_soc)) if target_soc else "")

        elif action == "skip_tonight":
            self._set_ha_input("input_boolean.ev_nocharge_tonight", True)
            desc = "Nenabíjet dnes v noci"

        elif action == "set_target_soc":
            if target_soc:
                self._set_ha_input("input_number.ev_target_soc_tomorrow",
                                   min(100, max(20, int(target_soc))))
            desc = "Cílový SOC nastaven na {}%".format(
                int(target_soc) if target_soc else "?")

        elif action == "multi_day":
            if target_soc:
                self._set_ha_input("input_number.ev_target_soc_tomorrow",
                                   min(100, max(20, int(target_soc))))
            self._set_ha_input("input_boolean.ev_nocharge_tonight", False)
            desc = "Vícedenní plán: nabít na {}%, platí {} dní".format(
                int(target_soc) if target_soc else DEFAULT_TARGET_SOC, valid_days)

        else:
            desc = "Neznámá akce: {}".format(action)

        # Store active override
        self.set_state("sensor.energy_planner_active_override",
                       state=desc,
                       attributes={
                           "friendly_name": "Aktivní override",
                           "icon": "mdi:account-cog",
                           "valid_until": valid_until,
                           "original_request": original,
                           "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       })

        # Clear pending
        self.set_state("sensor.energy_planner_pending", state="applied",
                       attributes={"friendly_name": "Planner čekající akce",
                                   "icon": "mdi:check-circle",
                                   "applied": desc})

        # Update recommendation
        self._update_recommendation(
            state="Override aktivní",
            decision=desc,
            reason="Uživatelská instrukce: {}".format(summary),
            active_override=True,
            override_until=valid_until)

        self.log("Applied: {} (until {})".format(desc, valid_until))

    # ══════════════════════════════════════════════════════════════════════
    # PART 5 — Forecast 3-day table for dashboard
    # ══════════════════════════════════════════════════════════════════════

    def _get_3day_forecast_table(self):
        """Return 3-day forecast with expected km and estimated SOC after driving."""
        self._soc_per_km = self._get_soc_per_km()
        ev_soc = self._float("sensor.skoda_elroq_battery_percentage")
        if ev_soc <= 0:
            val = self._float_influx_last("%", "skoda_elroq_battery_percentage", 24)
            ev_soc = val if val else 50

        _, forecast_3d, _, _ = self._get_km_model()
        running_soc = ev_soc
        table = []
        for day in forecast_3d:
            running_soc -= day["expected_km"] * self._soc_per_km
            table.append({
                "date": day["date"],
                "day": day["day_name"],
                "km": day["expected_km"],
                "soc_after": round(max(0, running_soc), 1),
            })
        return table
