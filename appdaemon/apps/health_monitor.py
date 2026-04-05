import appdaemon.plugins.hass.hassapi as hass
import requests
from datetime import datetime, timedelta


class HealthMonitor(hass.Hass):

    def initialize(self):
        self._notify = self.args.get("notify_service", "notify/mobile_app_iphone_17")
        self._interval = int(self.args.get("check_interval", 300))
        self._cooldown_min = int(self.args.get("cooldown_minutes", 60))

        self._influx_url = "http://{}:{}".format(
            self.args.get("influxdb_host", "a0d7b954-influxdb"),
            self.args.get("influxdb_port", 8086))
        self._influx_db = self.args.get("influxdb_database", "homeassistant")
        self._influx_user = self.args.get("influxdb_username", "db_write")
        self._influx_pass = self.args.get("influxdb_password", "db_write_pass")

        self._entity_checks = self.args.get("entity_checks", [])
        self._addon_restart_map = self.args.get("addon_restart_map", {})

        self._last_alert = {}
        self._consecutive_fails = {}

        self.run_every(self._check_all, "now+90", self._interval)
        self.log("HealthMonitor initialized (interval={}s, cooldown={}min, checks={})".format(
            self._interval, self._cooldown_min, len(self._entity_checks)))

    def _check_all(self, kwargs):
        results = []

        for check in self._entity_checks:
            entity_id = check["entity_id"]
            name = check["name"]
            expected = check.get("expected", "on")
            bad_states = check.get("bad_states", [])
            auto_restart_slug = check.get("auto_restart_slug", None)

            state = self.get_state(entity_id)
            if bad_states:
                ok = state not in bad_states and state is not None
            else:
                ok = state == expected
            results.append({"component": name, "ok": ok, "state": state})

            if not ok:
                self._consecutive_fails[name] = self._consecutive_fails.get(name, 0) + 1
                if auto_restart_slug and self._consecutive_fails[name] >= 2:
                    self._restart_addon_via_ha(auto_restart_slug, name)
                    self._consecutive_fails[name] = 0
                self._alert(name, "{} is {} (expected {})".format(
                    name, state, expected if not bad_states else "not " + str(bad_states)))
            else:
                if name in self._consecutive_fails:
                    del self._consecutive_fails[name]

        # Log to InfluxDB
        ok_count = sum(1 for r in results if r["ok"])
        fail_count = len(results) - ok_count
        failed_names = ",".join(r["component"] for r in results if not r["ok"]) or "none"

        line = 'health_check ok_count={},fail_count={},failed="{}",total={}'.format(
            ok_count, fail_count, failed_names, len(results))
        self._influx_write(line)

        if fail_count > 0:
            self.log("Health check: {}/{} OK, failed: {}".format(
                ok_count, len(results), failed_names), level="WARNING")
        else:
            self.log("Health check: all {} components OK".format(len(results)))

    def _restart_addon_via_ha(self, slug, name):
        """Restart addon via HA hassio.addon_restart service."""
        try:
            self.log("Auto-restarting addon: {} ({})".format(name, slug), level="WARNING")
            self.call_service("hassio/addon_restart", addon=slug)
            self._alert(name, "Auto-restarted: {}".format(name))
        except Exception as e:
            self.log("Addon restart failed for {}: {}".format(name, e), level="ERROR")

    def _alert(self, component, message):
        now = datetime.now()
        last = self._last_alert.get(component)
        if last and (now - last) < timedelta(minutes=self._cooldown_min):
            return

        self._last_alert[component] = now
        try:
            self.call_service(self._notify,
                              title="HA Health Alert",
                              message=message)
            self.log("Alert sent: {}".format(message))
        except Exception as e:
            self.log("Alert send failed: {}".format(e), level="ERROR")

    def _influx_write(self, line):
        try:
            requests.post("{}/write?db={}".format(self._influx_url, self._influx_db),
                          auth=(self._influx_user, self._influx_pass),
                          data=line.encode("utf-8"), timeout=5)
        except Exception:
            pass
