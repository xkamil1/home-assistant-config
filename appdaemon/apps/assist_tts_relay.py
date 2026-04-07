import appdaemon.plugins.hass.hassapi as hass
import re
import time
from datetime import datetime


class AssistTTSRelay(hass.Hass):
    """Relay voice assistant responses to soundbar.

    At processing->responding: mute VoicePE, poll log for new response,
    then immediately tts/speak on soundbar.
    """

    def initialize(self):
        self._soundbar = self.args.get(
            "media_player", "media_player.q_series_soundbar_2")
        self._tts = self.args.get("tts_entity", "tts.google_cloud")
        self._voice = self.args.get("voice", "cs-CZ-Chirp3-HD-Aoede")
        self._log_file = "/share/home-assistant.log"
        self._ansi_re = re.compile(r'\x1b\[[0-9;]*m')
        self._last_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._prev_content = None
        self._prev_volume = None
        self._was_playing = False
        self._radio_started = False

        self.listen_state(self._on_satellite,
                          "assist_satellite.viocepe_assist_satellite")

        self.log("AssistTTSRelay initialized (soundbar={})".format(
            self._soundbar))

    def _on_satellite(self, entity, attribute, old, new, kwargs):
        if old == "idle" and new == "listening":
            # Save current soundbar state before anything changes
            self._was_playing = self.get_state(self._soundbar) == "playing"
            self._prev_content = self.get_state(
                self._soundbar, attribute="media_content_id")
            self._prev_volume = float(
                self.get_state(self._soundbar, attribute="volume_level") or 0.10)
            if self._was_playing:
                self.log("Saved: playing={} vol={} content={}".format(
                    self._was_playing, self._prev_volume,
                    str(self._prev_content)[:60]))

        elif old == "processing" and new == "responding":
            # Check if radio was started by voice command before TTS overwrites it
            self._radio_started = (
                not self._was_playing
                and self.get_state(self._soundbar) == "playing")
            # Mute VoicePE immediately
            self.call_service("media_player/volume_set",
                              entity_id="media_player.viocepe_media_player",
                              volume_level=0)
            # Poll for new response (up to 4s)
            # Wait for final response - Claude may emit intermediate
            # AssistantContent (e.g. "Zjistím stav") before the real answer.
            # Strategy: keep polling and always take the latest response.
            text = None
            stable_count = 0
            for i in range(20):
                latest = self._read_new_response()
                if latest:
                    text = latest
                    stable_count = 0
                elif text:
                    stable_count += 1
                    if stable_count >= 3:
                        break  # no new response for 0.6s, use what we have
                time.sleep(0.2)

            if text:
                self.log("Speaking: {}".format(text[:80]))
                vol = float(self.get_state(self._soundbar,
                                           attribute="volume_level") or 0)
                if vol < 0.05 and self._prev_volume:
                    self.call_service("media_player/volume_set",
                                      entity_id=self._soundbar,
                                      volume_level=self._prev_volume)
                self.call_service("tts/speak",
                                  entity_id=self._tts,
                                  media_player_entity_id=self._soundbar,
                                  message=text,
                                  options={"voice": self._voice})
            else:
                self.log("No new response after 2s", level="WARNING")

        elif old == "responding" and new == "idle":
            # After TTS finishes on soundbar, restore radio if needed
            self.listen_state(self._on_soundbar_idle,
                              self._soundbar, new="idle",
                              oneshot=True, timeout=30)

    def _on_soundbar_idle(self, entity, attribute, old, new, kwargs):
        """Soundbar finished TTS - restore stream if applicable."""
        # Check last_radio_station - this is always up to date
        # (radio scripts update it, and it persists)
        radio_url = self.get_state("input_text.last_radio_station")
        if not radio_url:
            return

        # Was radio playing before, or did Claude just start it?
        # Either way, if prev was radio OR last_radio matches prev, restore it
        should_restore = False
        if self._was_playing and self._prev_content:
            if "tts_proxy" not in str(self._prev_content):
                should_restore = True
        # Also check if Claude started radio during this interaction
        # (prev_content would be TTS but last_radio_station was updated)
        current_content = self.get_state(
            self._soundbar, attribute="media_content_id") or ""
        if "tts_proxy" in current_content or self.get_state(self._soundbar) == "idle":
            # Soundbar is idle or playing TTS remnant - check if radio should play
            if self._was_playing:
                should_restore = True

        # Radio started by voice command during this interaction
        if not should_restore and radio_url and self._radio_started:
            should_restore = True
            self.log("Radio started by voice command: {}".format(
                radio_url[:60]))

        if should_restore:
            self.log("Restoring radio: vol={} url={}".format(
                self._prev_volume, radio_url[:60]))
            self.call_service("media_player/play_media",
                              entity_id=self._soundbar,
                              media_content_id=radio_url,
                              media_content_type="music")
            self.call_service("media_player/volume_set",
                              entity_id=self._soundbar,
                              volume_level=self._prev_volume)
        self._was_playing = False
        # Restore VoicePE volume
        self.call_service("media_player/volume_set",
                          entity_id="media_player.viocepe_media_player",
                          volume_level=0.10)

    def _read_new_response(self):
        """Read the newest AssistantContent from the log that is newer than _last_ts."""
        try:
            with open(self._log_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 100000))
                tail = f.read().decode("utf-8", errors="replace")

            lines = tail.split("\n")
            # Search from newest to oldest, return the latest new response
            for line in reversed(lines):
                clean = self._ansi_re.sub("", line)
                if "AssistantContent" not in clean:
                    continue
                if "tool_calls=[ToolInput" in clean:
                    continue
                # Skip entries with content=None (tool-call-only responses)
                if "content=None" in clean or "content='None'" in clean:
                    continue
                ts_match = re.match(
                    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", clean)
                if not ts_match:
                    continue
                ts = ts_match.group(1)
                if ts <= self._last_ts:
                    return None  # no new entry
                m = re.search(
                    r"AssistantContent\(role='assistant'.*?content='([^']+)'",
                    clean)
                if m:
                    self._last_ts = ts
                    return m.group(1)
            return None
        except Exception:
            return None
