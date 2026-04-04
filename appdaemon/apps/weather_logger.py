import appdaemon.plugins.hass.hassapi as hass
import requests
import json
from datetime import datetime, timedelta, timezone


class WeatherLogger(hass.Hass):
    """Log raw weather forecasts and actual conditions to InfluxDB.

    Stores:
    - weather_forecast: hourly predictions from Met.no and OWM
    - weather_actual: actual conditions every 30 min

    This data enables building a prediction model based on
    historical forecast accuracy (condition, temperature, cloud%).
    """

    def initialize(self):
        self._ha_url = self.args.get("ha_url", "http://10.0.0.67:8123")
        self._ha_token = self.args.get("ha_token")
        self._influx_url = "http://{}:{}".format(
            self.args.get("influxdb_host", "a0d7b954-influxdb"),
            self.args.get("influxdb_port", 8086))
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_user = self.args.get("influxdb_user", "db_write")
        self._influx_pass = self.args.get("influxdb_password", "db_write_pass")

        # Log forecasts every 30 min
        self.run_every(self._log_forecasts, "now+30", 1800)

        # Log actual conditions every 30 min
        self.run_every(self._log_actual, "now+60", 1800)

        self.log("WeatherLogger initialized")

    def _influx_write(self, lines):
        try:
            resp = requests.post(
                "{}/write?db={}".format(self._influx_url, self._influx_db),
                auth=(self._influx_user, self._influx_pass),
                data="\n".join(lines).encode("utf-8"),
                timeout=10)
            return resp.status_code == 204
        except Exception as e:
            self.log("InfluxDB write failed: {}".format(e), level="WARNING")
            return False

    def _get_forecast(self, entity_id, ftype):
        try:
            resp = requests.post(
                "{}/api/services/weather/get_forecasts?return_response".format(
                    self._ha_url),
                headers={
                    "Authorization": "Bearer {}".format(self._ha_token),
                    "Content-Type": "application/json",
                },
                json={"entity_id": entity_id, "type": ftype},
                timeout=10)
            if resp.status_code == 200:
                return resp.json().get("service_response", {}).get(
                    entity_id, {}).get("forecast", [])
        except Exception as e:
            self.log("Forecast error: {}".format(e), level="WARNING")
        return []

    def _log_forecasts(self, kwargs):
        try:
            self._do_log_forecasts()
        except Exception as e:
            self.log("Forecast log error: {}".format(e), level="WARNING")

    def _do_log_forecasts(self):
        now_utc = datetime.now(timezone.utc)
        now_str = now_utc.strftime("%Y-%m-%dT%H:%M:%S")
        ts_ns = int(now_utc.timestamp() * 1e9)

        lines = []

        # Met.no hourly forecast
        metno_h = self._get_forecast("weather.forecast_home", "hourly")
        for i, entry in enumerate(metno_h[:24]):
            dt_str = entry.get("datetime", "")
            condition = entry.get("condition", "unknown")
            temp = entry.get("temperature")
            humidity = entry.get("humidity")
            wind_speed = entry.get("wind_speed")
            precipitation = entry.get("precipitation")

            if not dt_str:
                continue

            # Hours ahead
            try:
                s = dt_str.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                hours_ahead = max(0, round((dt - now_utc).total_seconds() / 3600))
            except Exception:
                hours_ahead = i

            forecast_for = dt_str[:19]

            fields = []
            fields.append('condition="{}"'.format(condition))
            if temp is not None:
                fields.append("temperature={}".format(temp))
            if humidity is not None:
                fields.append("humidity={}".format(humidity))
            if wind_speed is not None:
                fields.append("wind_speed={}".format(wind_speed))
            if precipitation is not None:
                fields.append("precipitation={}".format(precipitation))
            fields.append("hours_ahead={}i".format(hours_ahead))
            fields.append('forecast_for="{}"'.format(forecast_for))
            fields.append('created_at="{}"'.format(now_str))

            line = "weather_forecast,source=metno,hours_ahead={} {} {}".format(
                hours_ahead, ",".join(fields), ts_ns + i)
            lines.append(line)

        # OWM hourly forecast
        owm_h = self._get_forecast("weather.openweathermap", "hourly")
        for i, entry in enumerate(owm_h[:24]):
            dt_str = entry.get("datetime", "")
            condition = entry.get("condition", "unknown")
            temp = entry.get("temperature")
            humidity = entry.get("humidity")
            wind_speed = entry.get("wind_speed")
            precipitation = entry.get("precipitation")
            cloud_coverage = entry.get("cloud_coverage")

            if not dt_str:
                continue

            try:
                s = dt_str.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                hours_ahead = max(0, round((dt - now_utc).total_seconds() / 3600))
            except Exception:
                hours_ahead = i

            forecast_for = dt_str[:19]

            fields = []
            fields.append('condition="{}"'.format(condition))
            if temp is not None:
                fields.append("temperature={}".format(temp))
            if humidity is not None:
                fields.append("humidity={}".format(humidity))
            if wind_speed is not None:
                fields.append("wind_speed={}".format(wind_speed))
            if precipitation is not None:
                fields.append("precipitation={}".format(precipitation))
            if cloud_coverage is not None:
                fields.append("cloud_coverage={}".format(cloud_coverage))
            fields.append("hours_ahead={}i".format(hours_ahead))
            fields.append('forecast_for="{}"'.format(forecast_for))
            fields.append('created_at="{}"'.format(now_str))

            line = "weather_forecast,source=owm,hours_ahead={} {} {}".format(
                hours_ahead, ",".join(fields), ts_ns + 100 + i)
            lines.append(line)

        if lines:
            if self._influx_write(lines):
                self.log("Stored {} forecast points (metno={}, owm={})".format(
                    len(lines), len(metno_h[:24]), len(owm_h[:24])))

    def _log_actual(self, kwargs):
        try:
            self._do_log_actual()
        except Exception as e:
            self.log("Actual log error: {}".format(e), level="WARNING")

    def _do_log_actual(self):
        now_utc = datetime.now(timezone.utc)
        ts_ns = int(now_utc.timestamp() * 1e9)

        # Current weather from OWM (has cloud_coverage)
        owm_condition = self.get_state("weather.openweathermap_2") or "unknown"
        owm_temp = self._f("sensor.openweathermap_temperature_2")
        owm_humidity = self._f("sensor.openweathermap_humidity_2")
        owm_cloud = self._f("sensor.openweathermap_cloud_coverage_2")
        owm_wind = self._f("sensor.openweathermap_wind_speed_2")

        # Current weather from Met.no
        metno_condition = self.get_state("weather.forecast_home") or "unknown"

        # Actual FVE production
        fve_power = self._f("sensor.inverter_active_power")
        fve_daily = self._f("sensor.inverter_daily_yield")

        # Outdoor temperature from our sensor
        outdoor_temp = self._f("sensor.venkovni_teplota_temperature")

        fields = [
            'owm_condition="{}"'.format(owm_condition),
            "owm_temperature={}".format(owm_temp),
            "owm_humidity={}".format(owm_humidity),
            "owm_cloud_coverage={}".format(owm_cloud),
            "owm_wind_speed={}".format(owm_wind),
            'metno_condition="{}"'.format(metno_condition),
            "outdoor_temperature={}".format(outdoor_temp),
            "fve_power_w={}".format(fve_power),
            "fve_daily_kwh={}".format(fve_daily),
        ]

        line = "weather_actual {} {}".format(",".join(fields), ts_ns)

        if self._influx_write([line]):
            self.log("Actual: {}°C cloud={}% fve={}W condition={}/{}".format(
                outdoor_temp, owm_cloud, round(fve_power),
                metno_condition, owm_condition))

    def _f(self, entity_id, default=0.0):
        try:
            v = self.get_state(entity_id)
            return float(v) if v not in (None, "unavailable", "unknown") else default
        except (ValueError, TypeError):
            return default
