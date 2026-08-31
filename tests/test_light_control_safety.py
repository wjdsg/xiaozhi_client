import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import WebBridge


class LightControlSafetyTests(unittest.IsolatedAsyncioTestCase):
    def make_bridge(self, level=0):
        bridge = object.__new__(WebBridge)
        bridge._light_level = level
        bridge._intent_texts = {
            "light_on": "灯已打开",
            "light_off": "灯已关闭",
            "brightness_low": "灯光已调到低档",
            "brightness_mid": "灯光已调到中档",
            "brightness_high": "灯光已调到高档",
        }
        bridge._keep_listening = False
        bridge._skip_tts = False
        bridge._intent_feedback_lock = asyncio.Lock()
        bridge._speech_start_time = 0.0
        bridge._subtitle_turn_id = 0
        bridge._current_opus_buf = []
        bridge._current_tts_text = ""
        bridge._replies = []
        bridge._broadcasts = []
        bridge._feedback = []

        async def reply(msg_id, result=None, error_msg=None):
            bridge._replies.append((msg_id, result, error_msg))

        async def broadcast(payload):
            bridge._broadcasts.append(payload)

        async def feedback(response_key, subtitle, discard_server_tts=True):
            bridge._feedback.append((response_key, subtitle, discard_server_tts))

        async def set_state(_state):
            return None

        async def play_intent_response(_response_key):
            return None

        bridge._mcp_reply = reply
        bridge._broadcast_json = broadcast
        bridge._play_local_intent_feedback = feedback
        bridge._play_intent_response = play_intent_response
        bridge._set_state = set_state
        return bridge

    async def test_tool_schema_requires_explicit_state(self):
        bridge = self.make_bridge()
        await bridge._mcp_tools_list("schema")
        tools = bridge._replies[0][1]["tools"]
        light_toggle = next(tool for tool in tools if tool["name"] == "light_toggle")
        self.assertEqual(light_toggle["inputSchema"]["required"], ["state"])

    async def test_mcp_missing_or_invalid_state_is_noop(self):
        for arguments in ({}, {"state": "unknown"}):
            with self.subTest(arguments=arguments):
                bridge = self.make_bridge(level=2)
                await bridge._mcp_tools_call(
                    "call", {"name": "light_toggle", "arguments": arguments}
                )
                self.assertEqual(bridge._light_level, 2)
                self.assertTrue(bridge._replies[-1][2])
                self.assertEqual(bridge._broadcasts, [])

    async def test_mcp_on_and_off_are_idempotent(self):
        bridge = self.make_bridge(level=2)
        await bridge._mcp_tools_call(
            "on", {"name": "light_toggle", "arguments": {"state": "on"}}
        )
        self.assertEqual(bridge._light_level, 2)
        await bridge._mcp_tools_call(
            "on-again", {"name": "light_toggle", "arguments": {"state": "on"}}
        )
        self.assertEqual(bridge._light_level, 2)
        await bridge._mcp_tools_call(
            "off", {"name": "light_toggle", "arguments": {"state": "off"}}
        )
        self.assertEqual(bridge._light_level, 0)
        await bridge._mcp_tools_call(
            "off-again", {"name": "light_toggle", "arguments": {"state": "off"}}
        )
        self.assertEqual(bridge._light_level, 0)

    async def test_light_mcp_uses_paired_local_feedback_only(self):
        bridge = self.make_bridge(level=0)
        await bridge._mcp_tools_call(
            "on", {"name": "light_toggle", "arguments": {"state": "on"}}
        )
        self.assertEqual(
            bridge._feedback[-1], ("light_on", "灯已打开", True)
        )

        await bridge._mcp_tools_call(
            "brightness", {"name": "set_brightness", "arguments": {"level": 2}}
        )
        self.assertEqual(
            bridge._feedback[-1], ("brightness_mid", "灯光已调到中档", True)
        )

    async def test_local_feedback_clears_tts_suppression(self):
        bridge = self.make_bridge()
        await WebBridge._play_local_intent_feedback(
            bridge, "light_on", "灯已打开", discard_server_tts=True
        )
        self.assertFalse(bridge._skip_tts)

    async def test_local_feedback_audio_is_serialized_with_subtitle(self):
        bridge = self.make_bridge()
        events = []

        async def play_intent_response(response_key):
            events.append(("start", response_key))
            await asyncio.sleep(0.01)
            events.append(("stop", response_key))

        bridge._play_intent_response = play_intent_response
        await asyncio.gather(
            WebBridge._play_local_intent_feedback(
                bridge, "light_on", "灯已打开", discard_server_tts=True
            ),
            WebBridge._play_local_intent_feedback(
                bridge, "light_off", "灯已关闭", discard_server_tts=True
            ),
        )
        self.assertEqual(
            events,
            [
                ("start", "light_on"),
                ("stop", "light_on"),
                ("start", "light_off"),
                ("stop", "light_off"),
            ],
        )

    async def test_browser_button_keeps_no_argument_toggle(self):
        bridge = self.make_bridge(level=0)
        await bridge._on_browser_cmd({"type": "light_toggle"})
        self.assertEqual(bridge._light_level, 3)
        await bridge._on_browser_cmd({"type": "light_toggle"})
        self.assertEqual(bridge._light_level, 0)

    async def test_legacy_function_call_rejects_ambiguous_arguments(self):
        bridge = self.make_bridge(level=2)
        bridge._try_light_command("light_toggle", {})
        bridge._try_light_command("light_toggle", {"state": "unknown"})
        bridge._try_light_command("set_brightness", {})
        bridge._try_light_command("adjust_brightness", {})
        await asyncio.sleep(0)
        self.assertEqual(bridge._light_level, 2)
        self.assertEqual(bridge._broadcasts, [])

    async def test_legacy_function_call_accepts_structured_arguments(self):
        bridge = self.make_bridge(level=0)
        bridge._try_light_command("light_toggle", {"state": "on"})
        await asyncio.sleep(0)
        self.assertEqual(bridge._light_level, 3)
        bridge._try_light_command("set_brightness", {"level": 2})
        await asyncio.sleep(0)
        self.assertEqual(bridge._light_level, 2)
        bridge._try_light_command("adjust_brightness", {"direction": "down"})
        await asyncio.sleep(0)
        self.assertEqual(bridge._light_level, 1)

    async def test_tts_fallback_accepts_only_explicit_light_result_phrases(self):
        bridge = self.make_bridge(level=0)
        bridge._try_parse_light_from_text("灯光已调到高档")
        await asyncio.sleep(0)
        self.assertEqual(bridge._light_level, 3)

        bridge._try_parse_light_from_text("好的，已帮你打开台灯")
        await asyncio.sleep(0)
        self.assertEqual(bridge._light_level, 3)

        bridge._try_parse_light_from_text("台灯已关闭")
        await asyncio.sleep(0)
        self.assertEqual(bridge._light_level, 0)

        bridge._try_parse_light_from_text("已将台灯打开")
        await asyncio.sleep(0)
        self.assertEqual(bridge._light_level, 3)

        bridge._try_parse_light_from_text("中国历史和小朋友的故事，关闭这个话题")
        await asyncio.sleep(0)
        self.assertEqual(bridge._light_level, 3)

    async def test_tts_fallback_rejects_commands_and_questions_without_result(self):
        bridge = self.make_bridge(level=0)
        for text in ("怎么打开台灯", "请把台灯调到最高", "是否关闭灯光", "正在打开台灯"):
            with self.subTest(text=text):
                bridge._try_parse_light_from_text(text)
                await asyncio.sleep(0)
                self.assertEqual(bridge._light_level, 0)

    async def test_tts_fallback_supports_relative_light_result(self):
        bridge = self.make_bridge(level=1)
        bridge._try_parse_light_from_text("灯光已调亮一点")
        await asyncio.sleep(0)
        self.assertEqual(bridge._light_level, 2)
        bridge._try_parse_light_from_text("灯光已调暗一点")
        await asyncio.sleep(0)
        self.assertEqual(bridge._light_level, 1)

    async def test_tts_sentence_start_updates_simulated_light(self):
        bridge = self.make_bridge(level=0)
        bridge._on_xiaozhi_json({
            "type": "tts",
            "state": "sentence_start",
            "text": "灯光已调到高档",
        })
        await asyncio.sleep(0)
        self.assertEqual(bridge._light_level, 3)


if __name__ == "__main__":
    unittest.main()
