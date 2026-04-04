import appdaemon.plugins.hass.hassapi as hass
import requests
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

# Calibrated from real data (Mar 2026): actual_kwh/peak_kw * 100
# Peak = 7.4 kW. Sunny hour ~5 kWh = 68%, cloudy ~0.7 kWh = 10%
CONDITION_CONFIDENCE = {
    "sunny": 75,
    "clear-night": 0,
    "partlycloudy": 40,
    "cloudy": 10,
    "rainy": 5,
    "pouring": 3,
    "lightning": 3,
    "lightning-rainy": 3,
    "snowy": 5,
    "snowy-rainy": 5,
    "fog": 8,
    "windy": 65,
    "windy-variant": 65,
    "hail": 3,
}

HOURLY_WEIGHTS = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3]

INSTALLED_PEAK_KW = 7.4  # 7400 W installed capacity

DEFAULT_METNO_WEIGHT = 0.6
DEFAULT_OWM_WEIGHT = 0.4
DEFAULT_FS_CORRECTION = 1.0
DEFAULT_CONFIDENCE_CORRECTION = 1.0
MIN_CALIBRATION_DAYS = 3


def _parse_dt(dt_str):
    """Parse ISO datetime string to UTC-aware datetime."""
    if not dt_str:
        return None
    try:
        s = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _cond_conf(condition):
    return CONDITION_CONFIDENCE.get(condition, 70)


def _to_local(dt_utc):
    """Convert UTC datetime to local time (using system timezone)."""
    try:
        return dt_utc.astimezone().replace(tzinfo=None)
    except Exception:
        return dt_utc.replace(tzinfo=None) + timedelta(hours=1)


class SolarConfidence(hass.Hass):

    def initialize(self):
        self._ha_url = self.args.get("ha_url", "http://10.0.0.67:8123")
        self._ha_token = self.args.get("ha_token")
        if not self._ha_token:
            self.log("ERROR: ha_token not configured in apps.yaml", level="ERROR")
            return

        # InfluxDB v1 configuration
        self._influx_url = "http://{}:{}".format(
            self.args.get("influxdb_host", "a0d7b954-influxdb"),
            self.args.get("influxdb_port", 8086))
        self._influx_db = self.args.get("influxdb_database", "homeassistant")
        self._influx_user = self.args.get("influxdb_username", "db_write")
        self._influx_pass = self.args.get("influxdb_password", "db_write_pass")
        self._influx_ok = False
        self._init_influxdb()

        # Calibration state — loaded from sensor or defaults
        self._metno_weight = DEFAULT_METNO_WEIGHT
        self._owm_weight = DEFAULT_OWM_WEIGHT
        self._fs_correction = DEFAULT_FS_CORRECTION
        self._calibration_samples = 0
        self._last_calibrated = "never"
        self._metno_mae = 0.0
        self._owm_mae = 0.0
        self._confidence_correction = DEFAULT_CONFIDENCE_CORRECTION
        self._load_calibration_weights()

        self.run_in(self.update, 5)                     # first run after 5s
        self.run_every(self.update, "now+10", 1800)  # then every 30 minutes

        # Verification: every hour at :05
        self.run_hourly(self._verify_predictions, datetime.now().replace(
            minute=5, second=0, microsecond=0))

        # Daily calibration at 23:30
        self.run_daily(self._daily_calibration, datetime.now().replace(
            hour=23, minute=30, second=0, microsecond=0))

        self.log("SolarConfidence initialized (feedback loop enabled, "
                 "influxdb={})".format("OK" if self._influx_ok else "UNAVAILABLE"))

    # ── InfluxDB v1 helpers ────────────────────────────────────────────────

    def _init_influxdb(self):
        try:
            resp = requests.get("{}/ping".format(self._influx_url), timeout=5)
            if resp.status_code == 204:
                self._influx_ok = True
                self.log("InfluxDB connected: {}".format(self._influx_url))
            else:
                self.log("InfluxDB ping returned {}, feedback loop disabled".format(
                    resp.status_code), level="WARNING")
        except Exception as e:
            self.log("InfluxDB connection failed: {} — feedback loop disabled".format(e),
                     level="WARNING")

    def _influx_write(self, line_protocol):
        """Write line protocol string to InfluxDB v1."""
        if not self._influx_ok:
            return False
        try:
            resp = requests.post(
                "{}/write?db={}".format(self._influx_url, self._influx_db),
                auth=(self._influx_user, self._influx_pass),
                data=line_protocol.encode("utf-8"),
                timeout=10)
            if resp.status_code == 204:
                return True
            self.log("InfluxDB write error {}: {}".format(resp.status_code, resp.text),
                     level="WARNING")
        except Exception as e:
            self.log("InfluxDB write failed: {}".format(e), level="WARNING")
        return False

    def _influx_query(self, query):
        """Execute InfluxQL query, return list of series results."""
        if not self._influx_ok:
            return []
        try:
            resp = requests.get(
                "{}/query".format(self._influx_url),
                params={"db": self._influx_db, "q": query},
                auth=(self._influx_user, self._influx_pass),
                timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                if results:
                    return results[0].get("series", [])
        except Exception as e:
            self.log("InfluxDB query failed: {}".format(e), level="WARNING")
        return []

    def _escape_tag(self, val):
        """Escape special characters for InfluxDB line protocol tag values."""
        return str(val).replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")

    def _escape_field_str(self, val):
        """Escape string field value for line protocol."""
        return str(val).replace('"', '\\"')

    # ── Calibration weights ────────────────────────────────────────────────

    def _load_calibration_weights(self):
        try:
            attrs = self.get_state("sensor.solar_confidence_weights", attribute="all")
            if attrs and attrs.get("attributes"):
                a = attrs["attributes"]
                self._metno_weight = float(a.get("metno_weight", DEFAULT_METNO_WEIGHT))
                self._owm_weight = float(a.get("owm_weight", DEFAULT_OWM_WEIGHT))
                self._fs_correction = float(a.get("forecast_solar_correction", DEFAULT_FS_CORRECTION))
                self._calibration_samples = int(a.get("samples_count", 0))
                self._last_calibrated = a.get("calibrated_at", "never")
                self._metno_mae = float(a.get("metno_mae", 0.0))
                self._owm_mae = float(a.get("owm_mae", 0.0))
                self._confidence_correction = float(a.get(
                    "confidence_correction", DEFAULT_CONFIDENCE_CORRECTION))
                self.log("Loaded calibration: metno={:.2f} owm={:.2f} fs_corr={:.2f} "
                         "samples={}".format(self._metno_weight, self._owm_weight,
                                             self._fs_correction, self._calibration_samples))
        except Exception as e:
            self.log("Could not load calibration weights, using defaults: {}".format(e),
                     level="WARNING")

    def _update_weights_sensor(self, samples, status):
        self.set_state("sensor.solar_confidence_weights", state=status, attributes={
            "friendly_name": "Solar Confidence Kalibrace",
            "icon": "mdi:tune-vertical",
            "metno_weight": self._metno_weight,
            "owm_weight": self._owm_weight,
            "metno_mae": self._metno_mae,
            "owm_mae": self._owm_mae,
            "forecast_solar_correction": self._fs_correction,
            "confidence_correction": self._confidence_correction,
            "samples_count": samples,
            "calibrated_at": self._last_calibrated,
        })

    # ── Update trigger ─────────────────────────────────────────────────────

    def update(self, kwargs):
        try:
            self._calculate()
        except Exception as e:
            self.log("update error: {}".format(e), level="ERROR")

    # ── Prediction storage (InfluxDB v1) ───────────────────────────────────

    def _store_predictions(self, hourly_data, metno_future, owm_parsed, now_utc):
        if not self._influx_ok:
            return
        try:
            lines = []
            ts_ns = int(now_utc.timestamp() * 1e9)

            for i, (dt, metno_entry) in enumerate(metno_future[:8]):
                horizon = max(1, round((dt - now_utc).total_seconds() / 3600))
                predicted_for = dt.strftime("%Y-%m-%dT%H:00:00")
                created_at = now_utc.strftime("%Y-%m-%dT%H:%M:%S")

                metno_cond = metno_entry.get("condition", "unknown")
                metno_conf = _cond_conf(metno_cond)

                # Use unique timestamp per point (offset by index to avoid collisions)
                pt_ts = ts_ns + i * 3

                lines.append(
                    'solar_prediction,source=metno,horizon_hours={h} '
                    'predicted_condition="{cond}",predicted_confidence={conf},'
                    'predicted_for_hour="{pfh}",created_at="{cat}" {ts}'.format(
                        h=horizon, cond=self._escape_field_str(metno_cond),
                        conf=metno_conf, pfh=predicted_for, cat=created_at,
                        ts=pt_ts))

                # OWM
                if owm_parsed:
                    closest_dt, closest_f = min(
                        owm_parsed, key=lambda x: abs((x[0] - dt).total_seconds()))
                    if abs((closest_dt - dt).total_seconds()) <= 7200:
                        owm_cond = closest_f.get("condition", "unknown")
                        owm_conf = _cond_conf(owm_cond)
                        lines.append(
                            'solar_prediction,source=owm,horizon_hours={h} '
                            'predicted_condition="{cond}",predicted_confidence={conf},'
                            'predicted_for_hour="{pfh}",created_at="{cat}" {ts}'.format(
                                h=horizon, cond=self._escape_field_str(owm_cond),
                                conf=owm_conf, pfh=predicted_for, cat=created_at,
                                ts=pt_ts + 1))

                # Combined
                if i < len(hourly_data):
                    combined_conf = hourly_data[i]["confidence"]
                    lines.append(
                        'solar_prediction,source=combined,horizon_hours={h} '
                        'predicted_condition="{cond}",predicted_confidence={conf},'
                        'predicted_for_hour="{pfh}",created_at="{cat}" {ts}'.format(
                            h=horizon, cond=self._escape_field_str(metno_cond),
                            conf=combined_conf, pfh=predicted_for, cat=created_at,
                            ts=pt_ts + 2))

            if lines:
                payload = "\n".join(lines)
                if self._influx_write(payload):
                    self.log("Stored {} prediction points to InfluxDB".format(len(lines)))
        except Exception as e:
            self.log("Failed to store predictions: {}".format(e), level="WARNING")

    # ── Prediction verification (hourly at :05) ───────────────────────────

    def _verify_predictions(self, kwargs):
        try:
            self._do_verify()
        except Exception as e:
            self.log("Verification error: {}".format(e), level="WARNING")

    def _do_verify(self):
        if not self._influx_ok:
            return

        now_utc = datetime.now(timezone.utc)
        current_hour = now_utc.replace(minute=0, second=0, microsecond=0)
        hour_str = current_hour.strftime("%Y-%m-%dT%H:00:00")
        local_hour = _to_local(current_hour)

        # Skip night hours (before 6 or after 21)
        if local_hour.hour < 6 or local_hour.hour > 21:
            return

        # Get actual production for this hour
        actual_kwh = self._get_actual_production_kwh(current_hour)
        if actual_kwh is None:
            self.log("Could not determine actual production for {}, skipping".format(hour_str))
            return

        actual_confidence = min(100.0, max(0.0, (actual_kwh / INSTALLED_PEAK_KW) * 100))

        # Query predictions made for this hour (look back 9 hours)
        window_start = (current_hour - timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
        q = ('SELECT source, horizon_hours, predicted_confidence, predicted_condition '
             'FROM solar_prediction '
             "WHERE predicted_for_hour = '{}' AND time > '{}' "
             'GROUP BY source, horizon_hours'.format(hour_str, window_start))

        series = self._influx_query(q)

        lines = []
        count = 0
        ts_ns = int(current_hour.timestamp() * 1e9)

        for s in series:
            tags = s.get("tags", {})
            source = tags.get("source", "unknown")
            horizon = tags.get("horizon_hours", "0")
            values = s.get("values", [])
            columns = s.get("columns", [])

            for row in values:
                row_dict = dict(zip(columns, row))
                predicted_conf = row_dict.get("predicted_confidence")
                predicted_cond = row_dict.get("predicted_condition", "unknown")

                if predicted_conf is not None:
                    error = round(actual_confidence - predicted_conf, 1)
                    lines.append(
                        'solar_prediction_accuracy,source={src},horizon_hours={h} '
                        'predicted_confidence={pc},actual_confidence={ac},'
                        'error={err},predicted_condition="{cond}",'
                        'hour_of_day={hod}i,day_of_week={dow}i {ts}'.format(
                            src=source, h=horizon,
                            pc=predicted_conf, ac=round(actual_confidence, 1),
                            err=error,
                            cond=self._escape_field_str(predicted_cond or "unknown"),
                            hod=local_hour.hour, dow=local_hour.weekday(),
                            ts=ts_ns + count))
                    count += 1
                break  # one record per series (latest prediction)

        if lines:
            payload = "\n".join(lines)
            if self._influx_write(payload):
                self.log("Verification: stored {} accuracy records for {} "
                         "(actual={:.1f}%, {:.2f} kWh)".format(
                             count, hour_str, actual_confidence, actual_kwh))
        else:
            self.log("No predictions found for hour {}, nothing to verify".format(hour_str))

    def _get_actual_production_kwh(self, current_hour):
        """Get actual solar production for the given hour in kWh."""
        prev_hour = current_hour - timedelta(hours=1)
        prev_start = (prev_hour - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        prev_end = (prev_hour + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Method 1: Delta of inverter_daily_yield between hours
        try:
            q = ('SELECT LAST(value) FROM "kWh" '
                 "WHERE entity_id = 'inverter_daily_yield' "
                 "AND time >= '{}' AND time <= '{}'".format(prev_start, prev_end))
            series = self._influx_query(q)
            prev_yield = None
            for s in series:
                for row in s.get("values", []):
                    prev_yield = float(row[1])

            if prev_yield is not None:
                curr_yield = self._float("sensor.inverter_daily_yield")
                if curr_yield > 0:
                    delta = curr_yield - prev_yield
                    if delta >= 0:
                        self.log("Actual production (yield delta): {:.3f} kWh".format(delta))
                        return delta
        except Exception as e:
            self.log("Yield delta method failed: {}".format(e), level="WARNING")

        # Method 2: Average inverter_input_power over the hour
        try:
            q = ('SELECT MEAN(value) FROM "W" '
                 "WHERE entity_id = 'inverter_input_power' "
                 "AND time >= '{}' AND time <= '{}'".format(
                     prev_hour.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     current_hour.strftime("%Y-%m-%dT%H:%M:%SZ")))
            series = self._influx_query(q)
            for s in series:
                for row in s.get("values", []):
                    avg_w = float(row[1])
                    kwh = avg_w / 1000.0
                    self.log("Actual production (avg power): {:.0f}W = {:.3f} kWh".format(
                        avg_w, kwh))
                    return kwh
        except Exception as e:
            self.log("Avg power method failed: {}".format(e), level="WARNING")

        return None

    # ── Daily calibration (23:30) ──────────────────────────────────────────

    def _daily_calibration(self, kwargs):
        try:
            self._do_calibration()
        except Exception as e:
            self.log("Calibration error: {}".format(e), level="ERROR")

    def _do_calibration(self):
        if not self._influx_ok:
            self.log("InfluxDB not available, skipping calibration")
            return

        self.log("=== Daily calibration started ===")

        # ── Part 1: Calibrate Met.no / OWM weights ────────────────────────
        metno_mae = self._compute_mae("metno")
        owm_mae = self._compute_mae("owm")
        samples = self._count_accuracy_days()

        if samples < MIN_CALIBRATION_DAYS:
            self.log("Calibration pending: only {} days of data (need {})".format(
                samples, MIN_CALIBRATION_DAYS))
            self._update_weights_sensor(samples, "calibration pending")
            return

        if metno_mae is not None and owm_mae is not None and metno_mae > 0 and owm_mae > 0:
            inv_metno = 1.0 / metno_mae
            inv_owm = 1.0 / owm_mae
            total_inv = inv_metno + inv_owm
            self._metno_weight = round(inv_metno / total_inv, 3)
            self._owm_weight = round(inv_owm / total_inv, 3)
            self._metno_mae = round(metno_mae, 2)
            self._owm_mae = round(owm_mae, 2)
            self.log("Weights calibrated: metno={:.3f} (MAE={:.1f}%) "
                     "owm={:.3f} (MAE={:.1f}%)".format(
                         self._metno_weight, metno_mae, self._owm_weight, owm_mae))
        else:
            self.log("Could not compute MAE (metno={}, owm={}), keeping current weights".format(
                metno_mae, owm_mae), level="WARNING")

        # ── Part 2: Forecast Solar correction factor ──────────────────────
        fs_correction = self._compute_forecast_solar_correction()
        if fs_correction is not None:
            self._fs_correction = round(fs_correction, 3)
            self.log("Forecast Solar correction factor: {:.3f}".format(self._fs_correction))
        else:
            self.log("Could not compute Forecast Solar correction, keeping {:.3f}".format(
                self._fs_correction))

        # Part 3: Confidence bias correction
        conf_correction = self._compute_confidence_correction()
        if conf_correction is not None:
            self._confidence_correction = conf_correction
            self.log("Confidence correction: {:.4f}".format(
                self._confidence_correction))
        else:
            self.log("No confidence correction data, keeping {:.4f}".format(
                self._confidence_correction))

        self._calibration_samples = samples
        self._last_calibrated = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._update_weights_sensor(samples, "calibrated")

        self.log("=== Daily calibration complete: metno_w={:.3f} owm_w={:.3f} "
                 "fs_corr={:.3f} conf_corr={:.4f} samples={} ===".format(
                     self._metno_weight, self._owm_weight,
                     self._fs_correction, self._confidence_correction,
                     samples))

    def _compute_mae(self, source):
        """Compute Mean Absolute Error for a source over last 14 days."""
        try:
            q = ('SELECT MEAN(abs_error) FROM '
                 '(SELECT ABS(error) AS abs_error FROM solar_prediction_accuracy '
                 "WHERE source = '{}' AND time > now() - 14d)".format(source))
            series = self._influx_query(q)
            for s in series:
                for row in s.get("values", []):
                    val = row[1]
                    if val is not None:
                        return float(val)
        except Exception as e:
            self.log("MAE query failed for {}: {}".format(source, e), level="WARNING")
        return None

    def _count_accuracy_days(self):
        """Count distinct days with accuracy data in last 14 days."""
        try:
            q = ('SELECT COUNT(actual_confidence) FROM solar_prediction_accuracy '
                 "WHERE time > now() - 14d GROUP BY time(1d)")
            series = self._influx_query(q)
            days = 0
            for s in series:
                for row in s.get("values", []):
                    if row[1] is not None and row[1] > 0:
                        days += 1
            return days
        except Exception as e:
            self.log("Count days query failed: {}".format(e), level="WARNING")
        return 0

    def _compute_forecast_solar_correction(self):
        """Compare Forecast Solar predictions vs actual daily yield over 14 days."""
        try:
            # Get daily last value of energy_production_today (Forecast Solar)
            q1 = ('SELECT LAST(value) FROM "kWh" '
                   "WHERE entity_id = 'energy_production_today' AND time > now() - 14d "
                   "GROUP BY time(1d)")
            pred_series = self._influx_query(q1)
            predictions = {}
            for s in pred_series:
                for row in s.get("values", []):
                    if row[1] is not None and row[1] > 0:
                        day = row[0][:10]  # "2026-03-23T..."  → "2026-03-23"
                        predictions[day] = float(row[1])

            # Get daily last value of inverter_daily_yield (actual)
            q2 = ('SELECT LAST(value) FROM "kWh" '
                   "WHERE entity_id = 'inverter_daily_yield' AND time > now() - 14d "
                   "GROUP BY time(1d)")
            actual_series = self._influx_query(q2)
            actuals = {}
            for s in actual_series:
                for row in s.get("values", []):
                    if row[1] is not None:
                        day = row[0][:10]
                        actuals[day] = float(row[1])

            ratios = []
            for day in predictions:
                if day in actuals and predictions[day] > 0:
                    ratio = actuals[day] / predictions[day]
                    ratios.append(ratio)
                    self.log("  FS correction {}: pred={:.1f} actual={:.1f} ratio={:.3f}".format(
                        day, predictions[day], actuals[day], ratio))

            if ratios:
                correction = sum(ratios) / len(ratios)
                self.log("Forecast Solar correction: {:.3f} from {} days".format(
                    correction, len(ratios)))
                return correction
        except Exception as e:
            self.log("Forecast Solar correction failed: {}".format(e), level="WARNING")
        return None

    def _compute_confidence_correction(self):
        """Compute rolling bias correction from prediction accuracy data (7d)."""
        try:
            q = ('SELECT MEAN(actual_confidence) as avg_actual, '
                 'MEAN(predicted_confidence) as avg_predicted '
                 'FROM solar_prediction_accuracy '
                 "WHERE source = 'combined' AND time > now() - 7d")
            series = self._influx_query(q)
            for s in series:
                cols = s.get("columns", [])
                for row in s.get("values", []):
                    d = dict(zip(cols, row))
                    avg_actual = d.get("avg_actual")
                    avg_predicted = d.get("avg_predicted")
                    if (avg_actual is not None and avg_predicted is not None
                            and avg_predicted > 5):
                        raw_ratio = avg_actual / avg_predicted
                        # First calibration: use raw ratio directly
                        if self._confidence_correction >= 0.95:
                            new_corr = raw_ratio
                        else:
                            # EMA: 70% old + 30% new
                            new_corr = (0.7 * self._confidence_correction
                                        + 0.3 * raw_ratio)
                        result = max(0.05, min(1.0, round(new_corr, 4)))
                        self.log(
                            "Confidence correction: avg_actual={:.1f}% "
                            "avg_predicted={:.1f}% raw_ratio={:.3f} "
                            "new={:.4f} (was {:.4f})".format(
                                avg_actual, avg_predicted, raw_ratio,
                                result, self._confidence_correction))
                        return result
        except Exception as e:
            self.log("Confidence correction failed: {}".format(e),
                     level="WARNING")
        return None

    # ── Forecast fetch ────────────────────────────────────────────────────

    def _get_forecast(self, entity_id, ftype):
        try:
            resp = requests.post(
                "{}/api/services/weather/get_forecasts?return_response".format(self._ha_url),
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
            self.log("forecast error {} {}: {}".format(entity_id, ftype, e), level="WARNING")
        return []

    # ── Main calculation ──────────────────────────────────────────────────

    def _calculate(self):
        now_utc = datetime.now(timezone.utc)
        tomorrow_date = (now_utc + timedelta(days=1)).date()

        # ── Hourly confidence (next 8 hours) ──────────────────────────────

        metno_h = self._get_forecast("weather.forecast_home", "hourly")
        owm_h = self._get_forecast("weather.openweathermap", "hourly")

        # Parse OWM into sorted list of (dt, entry)
        owm_parsed = sorted(
            [(dt, f) for f in owm_h
             for dt in [_parse_dt(f.get("datetime"))] if dt is not None],
            key=lambda x: x[0]
        )

        # Take first 8 Met.no entries that start from now (allow 30min in past)
        cutoff = now_utc - timedelta(minutes=30)
        metno_future = []
        for f in metno_h:
            dt = _parse_dt(f.get("datetime"))
            if dt and dt >= cutoff:
                metno_future.append((dt, f))
            if len(metno_future) >= 8:
                break

        # Fall back to OWM if Met.no unavailable
        if not metno_future and owm_parsed:
            self.log("Met.no unavailable, using OWM as primary", level="WARNING")
            metno_future = [(dt, f) for dt, f in owm_parsed[:8]]

        hourly = []
        metno_confs = []
        owm_confs = []

        for dt, metno_entry in metno_future[:8]:
            metno_cond = metno_entry.get("condition")
            metno_conf = _cond_conf(metno_cond)
            metno_confs.append(metno_conf)

            # Nearest OWM entry within 2 hours
            owm_cond = None
            owm_conf = None
            if owm_parsed:
                closest_dt, closest_f = min(
                    owm_parsed, key=lambda x: abs((x[0] - dt).total_seconds())
                )
                if abs((closest_dt - dt).total_seconds()) <= 7200:
                    owm_cond = closest_f.get("condition")
                    # Use cloud_coverage % directly if available
                    owm_cloud = closest_f.get("cloud_coverage")
                    if owm_cloud is not None:
                        owm_conf = max(3, round(75 * (100 - owm_cloud) / 100))
                    else:
                        owm_conf = _cond_conf(owm_cond)
                    owm_confs.append(owm_conf)

            if owm_conf is not None:
                confidence = round(metno_conf * self._metno_weight +
                                   owm_conf * self._owm_weight)
            else:
                confidence = metno_conf

            local_dt = _to_local(dt)
            hourly.append({
                "hour": local_dt.strftime("%H:%M"),
                "condition_metno": metno_cond or "unknown",
                "condition_owm": owm_cond or "N/A",
                "confidence": confidence,
            })

        # Weighted average
        if hourly:
            weights = HOURLY_WEIGHTS[:len(hourly)]
            total_w = sum(weights)
            confidence_now = round(
                sum(h["confidence"] * w for h, w in zip(hourly, weights)) / total_w
            )
        else:
            confidence_now = 70

        metno_avg = round(sum(metno_confs) / len(metno_confs)) if metno_confs else 0
        owm_avg = round(sum(owm_confs) / len(owm_confs)) if owm_confs else 0

        # Apply bias correction
        confidence_now_raw = confidence_now
        confidence_now = max(0, min(100, round(
            confidence_now * self._confidence_correction)))

        self.set_state("sensor.solar_confidence_now", state=str(confidence_now), attributes={
            "friendly_name": "Solar Confidence Nyni",
            "unit_of_measurement": "%",
            "icon": "mdi:solar-power",
            "hourly": hourly,
            "metno_avg": str(metno_avg),
            "owm_avg": str(owm_avg),
            "confidence_raw": confidence_now_raw,
            "confidence_correction": self._confidence_correction,
            "metno_weight": self._metno_weight,
            "owm_weight": self._owm_weight,
            "forecast_solar_correction": self._fs_correction,
            "calibration_samples": self._calibration_samples,
            "last_calibrated": self._last_calibrated,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

        # Store predictions to InfluxDB for feedback loop
        self._store_predictions(hourly, metno_future, owm_parsed, now_utc)

        # ── Tomorrow confidence ───────────────────────────────────────────

        metno_daily = self._get_forecast("weather.forecast_home", "daily")

        # Met.no: find tomorrow's daily entry
        metno_tom_cond = None
        for f in metno_daily:
            dt = _parse_dt(f.get("datetime"))
            if dt and _to_local(dt).date() == tomorrow_date:
                metno_tom_cond = f.get("condition")
                break

        # OWM: aggregate hourly entries for tomorrow
        owm_tom_confs = []
        owm_tom_cond = None
        for dt, f in owm_parsed:
            if _to_local(dt).date() == tomorrow_date:
                owm_tom_confs.append(_cond_conf(f.get("condition")))
                if owm_tom_cond is None:
                    owm_tom_cond = f.get("condition")

        metno_tom_conf = _cond_conf(metno_tom_cond) if metno_tom_cond else 70
        owm_tom_conf = (round(sum(owm_tom_confs) / len(owm_tom_confs))
                        if owm_tom_confs else None)

        if owm_tom_conf is not None:
            weather_conf_tom = round(metno_tom_conf * self._metno_weight +
                                     owm_tom_conf * self._owm_weight)
        else:
            weather_conf_tom = metno_tom_conf

        # Forecast Solar DISABLED - unreliable (avg error 11.6 kWh over 14 days)
        # Confidence based purely on calibrated weather conditions
        fs_kwh_raw = self._float("sensor.energy_production_tomorrow")
        fs_kwh = fs_kwh_raw * self._fs_correction
        solar_factor = 0  # kept for logging only

        confidence_tom_raw = round(weather_conf_tom)
        confidence_tom_raw = max(0, min(100, confidence_tom_raw))
        confidence_tom = max(0, min(100, round(
            confidence_tom_raw * self._confidence_correction)))

        if confidence_tom >= 80:
            rec = "Výborný den pro FVE, nabíjení baterie a EV z přebytku očekáváno"
        elif confidence_tom >= 60:
            rec = "Dobrý den pro FVE, částečné nabíjení z přebytku možné"
        elif confidence_tom >= 40:
            rec = "Průměrný výkon FVE, spoléhej na síť pro EV nabíjení"
        else:
            rec = "Slabý výkon FVE, EV nabíjení z přebytku nepravděpodobné"

        self.set_state("sensor.solar_confidence_tomorrow", state=confidence_tom, attributes={
            "friendly_name": "Solar Confidence Zítra",
            "unit_of_measurement": "%",
            "icon": "mdi:solar-power-variant",
            "confidence_raw": confidence_tom_raw,
            "confidence_correction": self._confidence_correction,
            "forecast_solar_kwh": round(fs_kwh, 1),
            "forecast_solar_kwh_raw": round(fs_kwh_raw, 1),
            "forecast_solar_correction": self._fs_correction,
            "weather_condition": metno_tom_cond or "unknown",
            "owm_condition": owm_tom_cond or "N/A",
            "recommendation": rec,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

        self.log("Updated: now={}% (raw {}%, corr={:.3f}) | "
                 "tomorrow={}% (raw {}%, weather={}% solar={}% "
                 "fs={:.1f}kWh) metno/owm={:.2f}/{:.2f}".format(
                     confidence_now, confidence_now_raw,
                     self._confidence_correction,
                     confidence_tom, confidence_tom_raw,
                     weather_conf_tom, solar_factor, fs_kwh,
                     self._metno_weight, self._owm_weight))

    # ── Helper ────────────────────────────────────────────────────────────

    def _float(self, entity_id, default=0.0):
        try:
            v = self.get_state(entity_id)
            return float(v) if v not in (None, "unavailable", "unknown") else default
        except (ValueError, TypeError):
            return default
