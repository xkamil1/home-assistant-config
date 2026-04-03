import appdaemon.plugins.hass.hassapi as hass
import requests
import json
import re
from datetime import datetime


class NotificationLogger(hass.Hass):
    """Log all push notifications to InfluxDB and a HA sensor."""

    def initialize(self):
        self._influx_host = self.args.get("influxdb_host", "a0d7b954-influxdb")
        self._influx_port = self.args.get("influxdb_port", 8086)
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_auth = (
            self.args.get("influxdb_user", "db_write"),
            self.args.get("influxdb_password", "db_write_pass"))

        self.listen_event(self._on_notify, "call_service", domain="notify")
        self.log("NotificationLogger started")

    def _on_notify(self, event_name, data, kwargs):
        try:
            service = data.get("service", "")
            service_data = data.get("service_data", {})

            title = service_data.get("title", "")
            message = service_data.get("message", "")

            if not message:
                return

            self._save_to_influxdb(service, title, message)
            self._update_sensor(service, title, message)
            self.log("Notification logged: {} - {}".format(
                title[:40], message[:60]))

        except Exception as e:
            self.log("NotificationLogger error: {}".format(e), level="ERROR")

    def _esc(self, text):
        s = str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        return re.sub(r"[^\x20-\x7E\u00C0-\u024F\u0400-\u04FF]", "", s)

    def _save_to_influxdb(self, service, title, message):
        try:
            line = 'push_notifications,service={} title="{}",message="{}"'.format(
                self._esc(service) or "unknown",
                self._esc(title),
                self._esc(message))
            requests.post(
                "http://{}:{}/write".format(self._influx_host, self._influx_port),
                params={"db": self._influx_db},
                auth=self._influx_auth,
                data=line.encode("utf-8"),
                timeout=5)
        except Exception as e:
            self.log("InfluxDB write error: {}".format(e), level="WARNING")

    def _update_sensor(self, service, title, message):
        current = self.get_state(
            "sensor.notification_log", attribute="notifications") or []

        entry = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "title": title,
            "message": message,
            "service": service,
        }

        current.insert(0, entry)
        current = current[:50]

        self.set_state(
            "sensor.notification_log",
            state=datetime.now().strftime("%Y-%m-%d %H:%M"),
            attributes={
                "notifications": current,
                "count": len(current),
                "friendly_name": "Log notifikaci",
                "icon": "mdi:bell-outline",
            })
