import appdaemon.plugins.hass.hassapi as hass
import requests
import json
import os
from datetime import datetime

TRACKING_FILE = '/homeassistant/appdaemon/apps/.appliance_tracking.json'


class ApplianceTracker(hass.Hass):

    def initialize(self):
        self._kwh_price = float(self.args.get("kwh_price_czk", 4.53))
        self._water_price_m3 = float(self.args.get("water_price_czk_m3", 138))

        # InfluxDB v1
        self._influx_url = "http://{}:{}".format(
            self.args.get("influxdb_host", "a0d7b954-influxdb"),
            self.args.get("influxdb_port", 8086))
        self._influx_db = self.args.get("influxdb_database", "homeassistant")
        self._influx_user = self.args.get("influxdb_username", "db_write")
        self._influx_pass = self.args.get("influxdb_password", "db_write_pass")
        self._influx_ok = False
        try:
            r = requests.get("{}/ping".format(self._influx_url), timeout=5)
            self._influx_ok = r.status_code == 204
        except Exception:
            pass

        # Tracking state per appliance
        self._tracking = {}
        self._load_tracking()

        # Appliance definitions
        self._appliances = {
            "pracka": {
                "machine_state": "sensor.pracka_machine_state",
                "energy": "sensor.pracka_energy",
                "water": "sensor.pracka_water_consumption",
                "sensor": "sensor.pracka_last_cycle",
            },
            "susicka": {
                "machine_state": "sensor.susicka_machine_state",
                "energy": "sensor.susicka_energy",
                "water": None,
                "sensor": "sensor.susicka_last_cycle",
            },
        }

        for name, cfg in self._appliances.items():
            self.listen_state(self._on_state, cfg["machine_state"], appliance=name)

        # Recover tracking for appliances that are currently running
        for name, cfg in self._appliances.items():
            current = self.get_state(cfg["machine_state"])
            if current == "run" and name not in self._tracking:
                energy_start = self._f(cfg["energy"])
                water_start = self._f(cfg["water"]) if cfg["water"] else 0
                self._tracking[name] = {
                    "started_at": datetime.now().isoformat(),
                    "energy_start": energy_start,
                    "water_start": water_start,
                }
                self._save_tracking()
                self.log("{}: recovered running cycle (energy={:.1f})".format(name, energy_start))

        self.log("ApplianceTracker initialized (influxdb={}, el={} CZK/kWh, water={} CZK/m3)".format(
            "OK" if self._influx_ok else "OFF", self._kwh_price, self._water_price_m3))

    def _save_tracking(self):
        try:
            with open(TRACKING_FILE, "w") as f:
                json.dump(self._tracking, f)
        except Exception as e:
            self.log("Save tracking error: {}".format(e), level="WARNING")

    def _load_tracking(self):
        try:
            if os.path.exists(TRACKING_FILE):
                with open(TRACKING_FILE) as f:
                    self._tracking = json.load(f)
                self.log("Loaded tracking: {}".format(list(self._tracking.keys())))
        except Exception as e:
            self.log("Load tracking error: {}".format(e), level="WARNING")

    def _f(self, entity_id, default=0.0):
        try:
            v = self.get_state(entity_id)
            return float(v) if v not in (None, "unavailable", "unknown") else default
        except (ValueError, TypeError):
            return default

    def _on_state(self, entity, attribute, old, new, kwargs):
        if new in ("unavailable", "unknown") or old == new:
            return

        name = kwargs["appliance"]
        cfg = self._appliances[name]

        if new == "run" and old != "run":
            # Cycle started
            energy_start = self._f(cfg["energy"])
            water_start = self._f(cfg["water"]) if cfg["water"] else 0
            self._tracking[name] = {
                "started_at": datetime.now().isoformat(),
                "energy_start": energy_start,
                "water_start": water_start,
            }
            self._save_tracking()
            self.log("{}: cycle started (energy={:.1f} kWh, water={:.0f} L)".format(
                name, energy_start, water_start))

        elif old == "run" and new in ("stop", "end"):
            # Cycle ended
            track = self._tracking.pop(name, None)
            self._save_tracking()
            if not track:
                self.log("{}: cycle ended but no start data".format(name))
                return

            now = datetime.now()
            started = datetime.fromisoformat(track["started_at"]) if isinstance(track["started_at"], str) else track["started_at"]
            duration_min = round((now - started).total_seconds() / 60)
            energy_now = self._f(cfg["energy"])
            energy_kwh = round(energy_now - track["energy_start"], 2)
            if energy_kwh < 0:
                energy_kwh = 0
            water_l = 0
            water_cost = 0
            if cfg["water"]:
                water_now = self._f(cfg["water"])
                water_l = round(water_now - track["water_start"])
                if water_l < 0:
                    water_l = 0
                water_cost = round(water_l / 1000 * self._water_price_m3, 2)

            cost_czk = round(energy_kwh * self._kwh_price + water_cost, 2)

            finished_at = now.strftime("%Y-%m-%dT%H:%M:%S")

            # Update HA sensor
            attrs = {
                "friendly_name": "{} poslední cyklus".format(
                    "Pračka" if name == "pracka" else "Sušička"),
                "icon": "mdi:washing-machine" if name == "pracka" else "mdi:tumble-dryer",
                "duration_min": duration_min,
                "energy_kwh": energy_kwh,
                "cost_czk": cost_czk,
                "finished_at": finished_at,
            }
            if cfg["water"]:
                attrs["water_l"] = water_l

            self.set_state(cfg["sensor"],
                           state="{:.2f}".format(energy_kwh),
                           attributes=attrs)

            # InfluxDB
            fields = "duration_min={},energy_kwh={},cost_czk={}".format(
                duration_min, energy_kwh, cost_czk)
            if cfg["water"]:
                fields += ",water_l={}".format(water_l)
            line = "appliance_cycles,appliance={} {}".format(name, fields)
            self._influx_write(line)

            water_str = " water={}L".format(water_l) if cfg["water"] else ""
            self.log("{}: cycle ended ({}min, {:.2f}kWh,{} {:.2f}CZK)".format(
                name, duration_min, energy_kwh, water_str, cost_czk))

    def _influx_write(self, line):
        if not self._influx_ok:
            return
        try:
            requests.post("{}/write?db={}".format(self._influx_url, self._influx_db),
                          auth=(self._influx_user, self._influx_pass),
                          data=line.encode("utf-8"), timeout=5)
        except Exception:
            pass
