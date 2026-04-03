import appdaemon.plugins.hass.hassapi as hass


class AssistTTSRelay(hass.Hass):
    """Relay Assist conversation responses to soundbar via TTS."""

    def initialize(self):
        self._soundbar = self.args.get(
            "media_player", "media_player.q_series_soundbar_2")
        self._tts = self.args.get("tts_entity", "tts.google_cloud")
        self._voice = self.args.get("voice", "cs-CZ-Chirp3-HD-Aoede")
        self._last_response = ""

        # Listen for HA events
        self.listen_event(self._on_conversation, "tool_call")
        self.listen_event(self._on_assist, "assist_pipeline_run")

        # Listen for conversation entity state changes
        self.listen_state(self._on_claude_response,
                          "conversation.claude_conversation")

        self.log("AssistTTSRelay initialized (soundbar={})".format(
            self._soundbar))

    def _on_claude_response(self, entity, attribute, old, new, kwargs):
        """Claude conversation entity changed - new response available."""
        # The state is just a timestamp, not the response text
        # We need another approach
        pass

    def _on_conversation(self, event_name, data, kwargs):
        self.log("Tool call event: {}".format(str(data)[:200]))

    def _on_assist(self, event_name, data, kwargs):
        self.log("Assist event: {}".format(str(data)[:200]))

        # Try to extract TTS output URL
        result = data.get("result", {})
        tts_output = result.get("tts_output", {})
        url = tts_output.get("url")

        if url:
            self.log("Playing TTS URL on soundbar: {}".format(url[:100]))
            self.call_service("media_player/play_media",
                              entity_id=self._soundbar,
                              media_content_id=url,
                              media_content_type="music")
        else:
            # Try to get speech text from intent output
            intent_output = result.get("intent_output", {})
            response = intent_output.get("response", {})
            speech = response.get("speech", {}).get("plain", {}).get(
                "speech", "")

            if speech and speech != self._last_response:
                self._last_response = speech
                self.log("Speaking on soundbar: {}".format(speech[:100]))
                self.call_service("tts/speak",
                                  entity_id=self._tts,
                                  media_player_entity_id=self._soundbar,
                                  message=speech,
                                  options={"voice": self._voice})
