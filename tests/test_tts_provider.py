import tempfile
import unittest
from pathlib import Path

from src.dictation.tts_provider import TtsRequest, TtsService


class FakeProvider:
    def __init__(self, name="fake", available=True, failure=None):
        self.name = name
        self.available = available
        self.failure = failure
        self.calls = 0

    def availability(self):
        return self.available, None if self.available else "not configured"

    def synthesize(self, request, should_abort):
        self.calls += 1
        if self.failure:
            raise RuntimeError(self.failure)
        return b"ID3" + request.text.encode("utf-8") + b"x" * 128


class TtsProviderTests(unittest.TestCase):
    def test_uncached_then_cached_uses_provider_once(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = FakeProvider()
            service = TtsService(Path(directory), provider)
            first = service.synthesize(TtsRequest("奇观"))
            second = service.synthesize(TtsRequest("奇观"))
            self.assertEqual(first.status, "ready")
            self.assertFalse(first.cached)
            self.assertEqual(second.status, "ready")
            self.assertTrue(second.cached)
            self.assertEqual(provider.calls, 1)

    def test_cloud_failure_uses_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            cloud = FakeProvider(name="cloud", failure="cloud timeout")
            fallback = FakeProvider(name="local")
            result = TtsService(
                Path(directory), cloud, fallback_provider=fallback
            ).synthesize(TtsRequest("听写"))
            self.assertEqual(result.status, "ready")
            self.assertEqual(result.provider, "local")

    def test_double_provider_failure_keeps_both_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            cloud = FakeProvider(name="cloud", failure="cloud timeout")
            fallback = FakeProvider(name="local", failure="local voice missing")
            result = TtsService(
                Path(directory), cloud, fallback_provider=fallback
            ).synthesize(TtsRequest("听写"))
            self.assertEqual(result.status, "error")
            self.assertIn("cloud timeout", result.reason)
            self.assertIn("local voice missing", result.reason)


if __name__ == "__main__":
    unittest.main()
