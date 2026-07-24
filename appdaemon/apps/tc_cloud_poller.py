"""Poll Tuya Cloud shadow API for DPs not available locally (e.g. DC bus voltage)."""

import hassapi as hass
import hashlib
import hmac
import json
import time
from urllib.request import Request, urlopen
from urllib.error import URLError


class TCCloudPoller(hass.Hass):

    TUYA_BASE = "https://openapi.tuyaeu.com"

    def initialize(self):
        self._client_id = self.args["tuya_client_id"]
        self._client_secret = self.args["tuya_client_secret"]
        self._device_id = self.args["tuya_device_id"]
        self._poll_interval = self.args.get("poll_interval", 30)
        self._token = None
        self._token_expire = 0

        dp_map = self.args.get("dp_map", {})
        self._dp_map = {int(k): v for k, v in dp_map.items()}

        self.run_every(self._poll, "now+5", self._poll_interval)
        self.log("TCCloudPoller started, polling every %ds, DPs: %s",
                 self._poll_interval, list(self._dp_map.keys()))

    def _sign(self, method, path, token=""):
        t = str(int(time.time() * 1000))
        content_hash = hashlib.sha256(b"").hexdigest()
        string_to_sign = method + "\n" + content_hash + "\n\n" + path
        str_to_sign = self._client_id + token + t + string_to_sign
        sig = hmac.new(
            self._client_secret.encode(),
            str_to_sign.encode(),
            hashlib.sha256
        ).hexdigest().upper()
        return t, sig

    def _http_get(self, url, headers):
        req = Request(url, headers=headers)
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def _get_token(self):
        now = time.time()
        if self._token and now < self._token_expire:
            return self._token

        path = "/v1.0/token?grant_type=1"
        t, sig = self._sign("GET", path)
        data = self._http_get(self.TUYA_BASE + path, {
            "client_id": self._client_id,
            "sign": sig,
            "t": t,
            "sign_method": "HMAC-SHA256"
        })
        if not data.get("success"):
            self.log("Token error: %s", data.get("msg", "unknown"))
            return None

        result = data["result"]
        self._token = result["access_token"]
        self._token_expire = now + result.get("expire_time", 7200) - 60
        return self._token

    def _poll(self, kwargs):
        token = self._get_token()
        if not token:
            return

        path = "/v2.0/cloud/thing/{}/shadow/properties".format(self._device_id)
        t, sig = self._sign("GET", path, token)
        try:
            data = self._http_get(self.TUYA_BASE + path, {
                "client_id": self._client_id,
                "sign": sig,
                "t": t,
                "sign_method": "HMAC-SHA256",
                "access_token": token
            })
        except URLError as e:
            self.log("Shadow API error: %s", e)
            return

        if not data.get("success"):
            if data.get("code") == 1010:
                self._token = None
                self._token_expire = 0
            self.log("Shadow error: %s", data.get("msg", "unknown"))
            return

        for prop in data["result"]["properties"]:
            dp_id = prop.get("dp_id")
            if dp_id not in self._dp_map:
                continue

            value = prop.get("value")
            if value is None:
                continue
            entity_id = self._dp_map[dp_id]
            attrs = {
                "source": "tuya_cloud",
                "dp_id": dp_id,
                "unit_of_measurement": "V" if "voltage" in entity_id else "",
                "device_class": "voltage" if "voltage" in entity_id else None,
                "friendly_name": entity_id.replace("sensor.", "").replace("_", " ").title()
            }
            attrs = {k: v for k, v in attrs.items() if v is not None and v != ""}
            # state jako string: AppDaemon zahazuje falsy state (int 0) → HTTP 400
            self.set_state(entity_id, state=str(value), attributes=attrs)
