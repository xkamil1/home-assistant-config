import appdaemon.plugins.hass.hassapi as hass
import requests
import json
from datetime import datetime, timedelta


class DailyEnergyReport(hass.Hass):
    """Daily energy report at 21:00 via push notification."""

    def initialize(self):
        self._notify = self.args.get("notify_entity", "notify.mobile_app_iphone_17")
        self._influx_host = self.args.get("influxdb_host", "a0d7b954-influxdb")
        self._influx_port = self.args.get("influxdb_port", 8086)
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_auth = (
            self.args.get("influxdb_user", "db_write"),
            self.args.get("influxdb_password", "db_write_pass")
        )
        self._kwh_price_nt = float(self.args.get("kwh_price_nt", 4.20))
        self._kwh_price_vt = float(self.args.get("kwh_price_vt", 5.30))
        self._kwh_export_price = float(self.args.get("kwh_export_price", 0.50))

        # Daily report at 21:00
        self.run_daily(self._send_report,
                       datetime.now().replace(hour=21, minute=0, second=0))

        # Manual trigger (delayed to ensure HA entity exists)
        self.run_in(self._register_manual_trigger, 30)
        self.log("DailyEnergyReport initialized")

    def _register_manual_trigger(self, kwargs):
        self.listen_state(self._on_manual,
                          "input_button.energy_report_generate")
        self.log("Manual trigger registered")

    def _on_manual(self, entity, attribute, old, new, kwargs):
        self.log("Manual report triggered")
        self._send_report()

    def _send_report(self, kwargs=None):
        try:
            report = self._build_report()
            svc = self._notify.replace("notify.", "notify/")
            self.call_service(svc, title="Energie dnes", message=report)
            self.log("Daily report sent")
        except Exception as e:
            self.log("Report error: {}".format(e), level="ERROR")

    def _build_report(self):
        # Current values from HA entities
        fve_yield = self._f("sensor.inverter_daily_yield")
        bat_soc = self._f("sensor.battery_state_of_capacity")
        bat_charge = self._f("sensor.battery_day_charge")
        bat_discharge = self._f("sensor.battery_day_discharge")
        tc_energy = self._f("sensor.shelly_3em_daily_energy")
        forecast_tomorrow = self._f("sensor.energy_production_tomorrow")
        solar_conf = self._f("sensor.solar_confidence_tomorrow")

        # Daikin daily consumption
        daikin_total = 0
        for room in ["adela_pokoj", "nela_pokoj", "pracovna", "loznice"]:
            for mode in ["heating", "cooling"]:
                eid = "sensor.{}_climatecontrol_{}_daily_electrical_consumption".format(room, mode)
                daikin_total += self._f(eid)

        # Grid import/export from InfluxDB (today)
        grid_export, grid_import = self._get_grid_daily()

        # Estimate cost
        # Rough: import in NT (80%) and VT (20%) split
        import_cost = grid_import * (self._kwh_price_nt * 0.8 + self._kwh_price_vt * 0.2)
        export_revenue = grid_export * self._kwh_export_price
        net_cost = import_cost - export_revenue

        # Home consumption estimate
        home_consumption = fve_yield + grid_import - grid_export + bat_discharge - bat_charge

        lines = []
        lines.append("FVE vyroba: {:.1f} kWh".format(fve_yield))
        lines.append("Spotreba domu: ~{:.1f} kWh".format(max(0, home_consumption)))
        lines.append("Export do site: {:.1f} kWh".format(grid_export))
        lines.append("Import ze site: {:.1f} kWh".format(grid_import))
        lines.append("Baterie: +{:.1f} / -{:.1f} kWh (SOC {:.0f}%)".format(
            bat_charge, bat_discharge, bat_soc))
        lines.append("TC spotreba: {:.1f} kWh".format(tc_energy))
        lines.append("Daikin: {:.1f} kWh".format(daikin_total))
        lines.append("---")
        lines.append("Naklady: ~{:.0f} Kc".format(net_cost))
        lines.append("Zitra: {:.1f} kWh ({:.0f}% confidence)".format(
            forecast_tomorrow, solar_conf))

        return "\n".join(lines)

    def _get_grid_daily(self):
        """Get today's grid export and import from InfluxDB."""
        today = datetime.now().strftime("%Y-%m-%dT00:00:00Z")
        export = 0.0
        imp = 0.0
        try:
            # Export: positive grid values (mean per 5min * hours)
            q_exp = ("SELECT integral(value, 1h) FROM \"W\" "
                     "WHERE \"entity_id\" = 'power_meter_active_power' "
                     "AND value > 0 AND time >= '{}' GROUP BY time(1d)").format(today)
            r = self._influx_query(q_exp)
            if r and r[0].get("values"):
                val = r[0]["values"][0][1]
                if val:
                    export = val / 1000.0  # Wh to kWh

            # Import: negative grid values
            q_imp = ("SELECT integral(value, 1h) FROM \"W\" "
                     "WHERE \"entity_id\" = 'power_meter_active_power' "
                     "AND value < 0 AND time >= '{}' GROUP BY time(1d)").format(today)
            r2 = self._influx_query(q_imp)
            if r2 and r2[0].get("values"):
                val = r2[0]["values"][0][1]
                if val:
                    imp = abs(val) / 1000.0  # negative to positive kWh
        except Exception as e:
            self.log("Grid daily query error: {}".format(e))
        return export, imp

    def _influx_query(self, query):
        try:
            resp = requests.get(
                "http://{}:{}/query".format(self._influx_host, self._influx_port),
                params={"db": self._influx_db, "q": query},
                auth=self._influx_auth, timeout=10)
            data = resp.json()
            return data.get("results", [{}])[0].get("series", [])
        except Exception:
            return []

    def _f(self, entity_id):
        try:
            v = self.get_state(entity_id)
            if v in (None, "unavailable", "unknown"):
                return 0.0
            return float(v)
        except (ValueError, TypeError):
            return 0.0
