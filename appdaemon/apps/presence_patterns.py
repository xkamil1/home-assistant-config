import appdaemon.plugins.hass.hassapi as hass
import requests
import json
from datetime import datetime, timedelta
from collections import defaultdict

PERSONS = {
    "Kamil": "device_tracker.iphone_19",
    "Romana": "device_tracker.unifi_default_c2_eb_91_20_3b_6d",
    "Nela": "device_tracker.unifi_default_de_f6_6b_c7_67_74",
    "Adela": "device_tracker.unifi_default_0e_c7_df_8a_66_f9",
}

MIN_AWAY_MINUTES = 30
LEARNING_WEEKS = 4
DAYS_CZ = ["Po", "Ut", "St", "Ct", "Pa", "So", "Ne"]


class PresencePatterns(hass.Hass):

    def initialize(self):
        self._ha_url = self.args.get("ha_url", "http://10.0.0.67:8123")
        self._ha_token = self.args.get("ha_token")

        # InfluxDB v1
        self._influx_url = "http://{}:{}".format(
            self.args.get("influxdb_host", "a0d7b954-influxdb"),
            self.args.get("influxdb_port", 8086))
        self._influx_db = self.args.get("influxdb_db", "homeassistant")
        self._influx_user = self.args.get("influxdb_user", "db_write")
        self._influx_pass = self.args.get("influxdb_password", "db_write_pass")
        self._influx_ok = False
        self._init_influxdb()

        # Track previous states for transition detection
        self._prev_state = {}
        self._prev_ts = {}

        # Listen for real-time state changes on all tracked persons
        for name, eid in PERSONS.items():
            self.listen_state(self._on_state_change, eid, name=name)
            # Initialize prev state
            current = self.get_state(eid)
            if current in ("home", "not_home"):
                self._prev_state[name] = current
                self._prev_ts[name] = datetime.now()

        # Daily recompute at 23:00
        self.run_daily(self._compute, datetime.now().replace(
            hour=23, minute=0, second=0, microsecond=0))

        # Startup: backfill from HA History, then compute
        self.run_in(self._startup, 15)
        self.log("PresencePatterns initialized (influxdb={})".format(
            "OK" if self._influx_ok else "UNAVAILABLE"))

    # ── InfluxDB v1 ────────────────────────────────────────────────────────

    def _init_influxdb(self):
        try:
            resp = requests.get("{}/ping".format(self._influx_url), timeout=5)
            if resp.status_code == 204:
                self._influx_ok = True
        except Exception as e:
            self.log("InfluxDB failed: {}".format(e), level="WARNING")

    def _influx_write(self, line):
        if not self._influx_ok:
            return False
        try:
            resp = requests.post(
                "{}/write?db={}".format(self._influx_url, self._influx_db),
                auth=(self._influx_user, self._influx_pass),
                data=line.encode("utf-8"), timeout=10)
            return resp.status_code == 204
        except Exception:
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
        except Exception:
            pass
        return []

    # ── Real-time transition tracking ──────────────────────────────────────

    def _on_state_change(self, entity, attribute, old, new, kwargs):
        if new == "unavailable" or old == "unavailable":
            return
        if new not in ("home", "not_home") or old not in ("home", "not_home"):
            return
        if new == old:
            return

        name = kwargs.get("name", "?")
        now = datetime.now()

        if old == "home" and new == "not_home":
            direction = "departure"
        elif old == "not_home" and new == "home":
            direction = "arrival"
        else:
            return

        # Write transition to InfluxDB
        ts_ns = int(now.timestamp() * 1e9)
        hour_min = now.hour * 60 + now.minute
        dow = now.weekday()
        line = (
            'presence_transitions,person={person},direction={dir} '
            'hour_min={hm}i,day_of_week={dow}i,'
            'hour={h}i,minute={m}i '
            '{ts}'.format(
                person=name, dir=direction,
                hm=hour_min, dow=dow,
                h=now.hour, m=now.minute,
                ts=ts_ns))
        self._influx_write(line)

        # Also write to presence_log (richer format for recorder independence)
        someone_after = any(
            self.get_state(e) == "home" for n, e in PERSONS.items() if n != name
        ) or (new == "home")
        is_workday = dow < 5
        log_line = (
            'presence_log,person={person},transition={trans} '
            'state="{st}",previous_state="{prev}",'
            'hour={h}i,day_of_week={dow}i,'
            'is_workday="{wd}",someone_home_after="{sha}" '
            '{ts}'.format(
                person=name.lower(), trans="arrived" if direction == "arrival" else "departed",
                st=new, prev=old, h=now.hour, dow=dow,
                wd="yes" if is_workday else "no",
                sha="yes" if someone_after else "no",
                ts=ts_ns))
        self._influx_write(log_line)

        self.log("{} {} at {}".format(name, direction, now.strftime("%H:%M")))

        self._prev_state[name] = new
        self._prev_ts[name] = now

    # ── Startup: backfill from HA History ──────────────────────────────────

    def _startup(self, kwargs):
        try:
            self._backfill_from_history()
        except Exception as e:
            self.log("Backfill error: {}".format(e), level="WARNING")
        try:
            self._do_compute()
        except Exception as e:
            self.log("Startup compute error: {}".format(e), level="ERROR")

    def _backfill_from_history(self):
        """Backfill transitions from HA History API into InfluxDB."""
        # Check if we already have data in InfluxDB
        series = self._influx_query(
            "SELECT COUNT(hour_min) FROM presence_transitions WHERE time > now() - 1d")
        existing = 0
        for s in series:
            existing = s.get("values", [[0, 0]])[0][1] or 0

        if existing > 10:
            self.log("InfluxDB already has {} recent transitions, skipping backfill".format(
                existing))
            return

        self.log("Backfilling transitions from HA History...")
        now = datetime.now()
        start = now - timedelta(days=14)  # HA recorder typically keeps ~10 days
        start_str = start.strftime("%Y-%m-%dT00:00:00")
        end_str = now.strftime("%Y-%m-%dT23:59:59")

        total_written = 0
        for name, eid in PERSONS.items():
            try:
                resp = requests.get(
                    "{}/api/history/period/{}".format(self._ha_url, start_str),
                    params={
                        "end_time": end_str,
                        "filter_entity_id": eid,
                        "minimal_response": "",
                        "no_attributes": "",
                    },
                    headers={"Authorization": "Bearer {}".format(self._ha_token)},
                    timeout=30)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                if not data or not data[0]:
                    continue
            except Exception as e:
                self.log("History fetch {} error: {}".format(name, e), level="WARNING")
                continue

            # Build transitions
            prev_state = None
            for e in data[0]:
                state = e.get("state", "")
                ts_str = e.get("last_changed", "")
                if not ts_str or state == "unavailable":
                    continue
                if state not in ("home", "not_home"):
                    continue

                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    ts = ts.replace(tzinfo=None) + timedelta(hours=1)
                except Exception:
                    continue

                if prev_state and prev_state != state:
                    if prev_state == "home" and state == "not_home":
                        direction = "departure"
                    elif prev_state == "not_home" and state == "home":
                        direction = "arrival"
                    else:
                        prev_state = state
                        continue

                    ts_ns = int(ts.timestamp() * 1e9)
                    dow = ts.weekday()
                    hm = ts.hour * 60 + ts.minute
                    line = (
                        'presence_transitions,person={person},direction={dir} '
                        'hour_min={hm}i,day_of_week={dow}i,'
                        'hour={h}i,minute={m}i '
                        '{ts}'.format(
                            person=name, dir=direction,
                            hm=hm, dow=dow, h=ts.hour, m=ts.minute, ts=ts_ns))
                    if self._influx_write(line):
                        total_written += 1

                prev_state = state

        self.log("Backfilled {} transitions to InfluxDB".format(total_written))

    # ── Daily computation (23:00) ──────────────────────────────────────────

    def _compute(self, kwargs):
        try:
            self._do_compute()
        except Exception as e:
            self.log("Compute error: {}".format(e), level="ERROR")

    def _do_compute(self):
        now = datetime.now()
        results = {}
        data_start = None

        for name in PERSONS:
            pattern = self._compute_person(name)
            results[name] = pattern
            ds = pattern.get("data_start")
            if ds and (data_start is None or ds < data_start):
                data_start = ds

        data_days = (now - data_start).days if data_start else 0
        learning = data_days < LEARNING_WEEKS * 7

        # Build sensor
        persons_data = {}
        for name, pattern in results.items():
            persons_data[name] = {
                "morning_departure": pattern.get("avg_departure", "-"),
                "afternoon_return": pattern.get("avg_return", "-"),
                "workday_trips": pattern.get("workday_count", 0),
                "mostly_home": "yes" if pattern.get("mostly_home") else "no",
            }

        self.set_state("sensor.presence_patterns", state="OK",
                       attributes={
                           "friendly_name": "Vzorce pritomnosti",
                           "icon": "mdi:account-clock",
                           "persons": persons_data,
                           "data_days": data_days,
                           "learning": "yes" if learning else "no",
                           "min_weeks": LEARNING_WEEKS,
                           "updated": now.strftime("%Y-%m-%d %H:%M"),
                       })

        status = "learning ({}/{} dni)".format(data_days, LEARNING_WEEKS * 7) if learning else "ready"
        self.log("Patterns: {} | {}".format(status, ", ".join(
            "{}: {}->{}".format(n, p.get("avg_departure", "-"), p.get("avg_return", "-"))
            for n, p in results.items())))

    def _compute_person(self, name):
        """Compute presence pattern for a person from InfluxDB transitions."""
        lookback = LEARNING_WEEKS * 7

        # Get morning departures (workdays, before 9:00)
        dep_series = self._influx_query(
            "SELECT hour, minute, day_of_week FROM presence_transitions "
            "WHERE person = '{}' AND direction = 'departure' "
            "AND hour < 9 AND day_of_week < 5 "
            "AND time > now() - {}d ORDER BY time ASC".format(name, lookback))

        # Get afternoon arrivals (matching return after morning departure)
        arr_series = self._influx_query(
            "SELECT hour, minute, day_of_week FROM presence_transitions "
            "WHERE person = '{}' AND direction = 'arrival' "
            "AND hour >= 10 AND hour <= 20 AND day_of_week < 5 "
            "AND time > now() - {}d ORDER BY time ASC".format(name, lookback))

        # Get earliest data point
        first_series = self._influx_query(
            "SELECT FIRST(hour_min) FROM presence_transitions "
            "WHERE person = '{}'".format(name))
        data_start = None
        for s in first_series:
            ts = s.get("values", [[None]])[0][0]
            if ts:
                try:
                    data_start = datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    pass

        # Parse departures
        dep_mins = []
        for s in dep_series:
            for row in s.get("values", []):
                h = row[1]
                m = row[2]
                if h is not None and m is not None:
                    dep_mins.append(int(h) * 60 + int(m))

        # Parse arrivals
        arr_mins = []
        for s in arr_series:
            for row in s.get("values", []):
                h = row[1]
                m = row[2]
                if h is not None and m is not None:
                    arr_mins.append(int(h) * 60 + int(m))

        workday_count = len(dep_mins)

        def fmt(mins_list):
            if not mins_list:
                return "-"
            avg = sum(mins_list) // len(mins_list)
            return "{:02d}:{:02d}".format(avg // 60, avg % 60)

        if workday_count == 0:
            return {"mostly_home": True, "workday_count": 0, "data_start": data_start}

        return {
            "avg_departure": fmt(dep_mins),
            "avg_return": fmt(arr_mins),
            "workday_count": workday_count,
            "mostly_home": workday_count < 2,
            "data_start": data_start,
        }
