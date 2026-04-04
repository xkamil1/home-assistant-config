import appdaemon.plugins.hass.hassapi as hass
import requests
import time
import re


class AppDaemonLogForwarder(hass.Hass):
    """Forward AppDaemon addon logs to Loki via Supervisor API."""

    def initialize(self):
        self._loki_url = self.args.get("loki_url", "http://10.0.0.55:3100")
        self._sup_token = self.args.get("supervisor_token", "")
        self._interval = int(self.args.get("interval_seconds", 60))
        self._host_label = self.args.get("host_label", "ha-server")
        self._last_hashes = set()

        self.run_every(self._forward_logs, "now+20", self._interval)
        self.log("AppDaemonLogForwarder initialized (loki={}, interval={}s)".format(
            self._loki_url, self._interval))

    def _forward_logs(self, kwargs):
        try:
            self._do_forward()
        except Exception as e:
            self.log("Forward error: {}".format(e), level="WARNING")

    def _do_forward(self):
        resp = requests.get(
            "http://supervisor/addons/a0d7b954_appdaemon/logs",
            headers={"Authorization": "Bearer {}".format(self._sup_token)},
            timeout=15)

        if resp.status_code != 200:
            self.log("Supervisor addon logs error: {}".format(resp.status_code),
                     level="WARNING")
            return

        lines = resp.text.strip().split("\n")

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

        ts_base = int(time.time() * 1e9)
        values = []
        for i, line in enumerate(new_lines):
            clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
            if not clean.strip():
                continue
            values.append([str(ts_base + i), clean])

        if not values:
            return

        payload = {
            "streams": [{
                "stream": {
                    "job": "appdaemon",
                    "host": self._host_label,
                    "source": "addon",
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
                self.log("Forwarded {} AppDaemon lines to Loki".format(len(values)))
            else:
                self.log("Loki push {}: {}".format(
                    r.status_code, r.text[:80]), level="WARNING")
        except Exception as e:
            self.log("Loki push failed: {}".format(e), level="WARNING")
