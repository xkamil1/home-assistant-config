import appdaemon.plugins.hass.hassapi as hass
import requests
import time
import re


class HALogForwarder(hass.Hass):
    """Forward HA core logs to Loki via supervisor API."""

    def initialize(self):
        self._loki_url = self.args.get("loki_url", "http://10.0.0.55:3100")
        self._sup_token = self.args.get("supervisor_token", "")
        self._interval = int(self.args.get("interval_seconds", 60))
        self._host_label = self.args.get("host_label", "ha-server")
        self._last_hashes = set()

        self.run_every(self._forward_logs, "now+15", self._interval)
        self.log("HALogForwarder initialized (loki={}, interval={}s)".format(
            self._loki_url, self._interval))

    def _forward_logs(self, kwargs):
        try:
            self._do_forward()
        except Exception as e:
            self.log("Forward error: {}".format(e), level="WARNING")

    def _do_forward(self):
        # Get logs via supervisor API
        resp = requests.get(
            "http://supervisor/core/logs",
            headers={"Authorization": "Bearer {}".format(self._sup_token)},
            timeout=15)

        if resp.status_code != 200:
            self.log("Supervisor logs error: {}".format(resp.status_code),
                     level="WARNING")
            return

        lines = resp.text.strip().split("\n")

        # Filter new lines only
        new_lines = []
        new_hashes = set()
        for line in lines[-200:]:
            h = hash(line)
            new_hashes.add(h)
            if h not in self._last_hashes and line.strip():
                new_lines.append(line)

        self._last_hashes = new_hashes

        if not new_lines:
            return

        # Parse and send to Loki
        ts_base = int(time.time() * 1e9)
        values = []
        for i, line in enumerate(new_lines):
            # Strip ANSI color codes
            clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
            if not clean.strip():
                continue

            values.append([str(ts_base + i), clean])

        if not values:
            return

        payload = {
            "streams": [{
                "stream": {
                    "job": "homeassistant",
                    "host": self._host_label,
                    "source": "core",
                },
                "values": values,
            }]
        }

        try:
            r = requests.post(
                "{}/loki/api/v1/push".format(self._loki_url),
                json=payload,
                timeout=10)
            if r.status_code == 204:
                self.log("Forwarded {} lines to Loki".format(len(values)))
            else:
                self.log("Loki push {}: {}".format(
                    r.status_code, r.text[:80]), level="WARNING")
        except Exception as e:
            self.log("Loki push failed: {}".format(e), level="WARNING")
