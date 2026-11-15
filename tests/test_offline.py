"""离线单元测试：不联网、不发消息、不导入 AstrBot SDK、不触碰生产数据。

运行：
    python -m unittest discover -s tests -p "test_*.py" -v
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from douyin_core import (  # noqa: E402
    AsyncRateGate,
    DouyinError,
    DouyinStore,
    GateConfig,
    WriteGate,
    clamp,
    extract_aweme_id,
    extract_sec_uid,
    identity_key,
    jitter_ms,
    json_safe,
    map_error_text,
    normalize_aweme,
    normalize_code,
    normalize_search_payload,
    redact,
    redact_text,
)
from douyin_core.backend import extract_envelope  # noqa: E402
from douyin_core.media import clean_aweme_id  # noqa: E402


class ErrorTests(unittest.TestCase):
    def test_map_error_text(self):
        self.assertEqual(map_error_text("请先登录后再试"), "not_authenticated")
        self.assertEqual(map_error_text("verify_check"), "challenge_required")
        self.assertEqual(map_error_text("429 too many requests"), "rate_limited")
        self.assertEqual(map_error_text("视频不存在"), "not_found")
        self.assertEqual(map_error_text("JSON 解析失败"), "parse_failed")
        self.assertEqual(map_error_text("some random noise"), "backend_unavailable")

    def test_normalize_code_falls_back(self):
        self.assertEqual(normalize_code("rate_limited"), "rate_limited")
        self.assertEqual(normalize_code("brand_new_code", "请求频繁"), "rate_limited")
        self.assertEqual(normalize_code(None, ""), "backend_unavailable")

    def test_unknown_code_downgraded(self):
        error = DouyinError("mystery", "boom")
        self.assertEqual(error.code, "backend_unavailable")
        self.assertIn("backend_unavailable", str(error))


class RedactTests(unittest.TestCase):
    def test_string_patterns(self):
        text = "sessionid=abc123; Cookie: ttwid=xyz; authorization: Bearer tok"
        out = redact_text(text)
        self.assertNotIn("abc123", out)
        self.assertNotIn("Bearer tok", out)
        self.assertIn("<redacted>", out)

    def test_dict_keys(self):
        out = redact({"cookies": ["a=1"], "storage_state": {"cookies": []}, "title": "ok"})
        self.assertEqual(out["cookies"], "<redacted>")
        self.assertEqual(out["storage_state"], "<redacted>")
        self.assertEqual(out["title"], "ok")


class NormalizeTests(unittest.TestCase):
    def test_missing_is_null_not_zero(self):
        video = normalize_aweme({"aweme_id": "7123456789012345678"})
        self.assertEqual(video["title"], None)
        self.assertEqual(video["stats"]["likes"], None)
        self.assertNotEqual(video["stats"]["likes"], 0)
        self.assertEqual(video["author"]["nickname"], None)
        self.assertEqual(video["url"], "https://www.douyin.com/video/7123456789012345678")

    def test_invalid_input_returns_none(self):
        self.assertIsNone(normalize_aweme(None))
        self.assertIsNone(normalize_aweme("oops"))

    def test_search_payload_skips_user_cards(self):
        payload = normalize_search_payload({
            "result": {"data": [
                {"aweme_info": {"aweme_id": "7123456789012345678", "desc": "hi"}},
                {"user_list": [{"nickname": "someone"}]},
            ]},
            "has_more": True,
            "next_offset": 10,
            "logid": "abc",
        })
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["skipped"][0]["reason"], "user_card")
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["logid"], "abc")

    def test_extract_ids(self):
        self.assertEqual(extract_aweme_id("7123456789012345678"), "7123456789012345678")
        self.assertEqual(extract_aweme_id("https://www.douyin.com/video/7123456789012345678"),
                         "7123456789012345678")
        self.assertEqual(extract_aweme_id("https://v.douyin.com/iABCxyz/"), None)
        self.assertEqual(extract_sec_uid("https://www.douyin.com/user/MS4wLjABAAAA_test"),
                         "MS4wLjABAAAA_test")
        self.assertEqual(clean_aweme_id("https://www.iesdouyin.com/share/video/7123456789012345678/"),
                         "7123456789012345678")

    def test_json_safe_drops_non_finite(self):
        self.assertEqual(json_safe({"a": float("nan"), "b": 1, "c": None}),
                         {"a": None, "b": 1, "c": None})


class EnvelopeTests(unittest.TestCase):
    def test_picks_last_valid_envelope_after_progress_lines(self):
        stdout = (
            "\x1b[32mFetching...\x1b[0m\n"
            '{"ok": false, "error": {"code": "parse_failed"}}\n'
            '{"ok": true, "data": {"value": 42}}\n'
        )
        envelope = extract_envelope(stdout)
        self.assertIsNotNone(envelope)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["value"], 42)

    def test_no_envelope(self):
        self.assertIsNone(extract_envelope("just some log lines"))


class GateTests(unittest.TestCase):
    def test_clamp_and_jitter_bounds(self):
        self.assertEqual(clamp("999", 1, 20, 10), 20)
        self.assertEqual(clamp(None, 1, 20, 10), 10)
        self.assertEqual(jitter_ms(100, 100), 100)
        for _ in range(50):
            self.assertTrue(10 <= jitter_ms(10, 20) <= 20)

    def test_write_gate_three_checks(self):
        gate = WriteGate(writes_enabled=True, allow_targets=("张三",))
        self.assertEqual(gate.evaluate("张三", False)[1], "dry_run")
        self.assertEqual(WriteGate(False).evaluate("张三", True)[1], "writes_disabled")
        self.assertEqual(gate.evaluate("李四", True)[1], "target_not_allowed")
        self.assertEqual(gate.evaluate("张三", True)[1], "allowed")
        self.assertEqual(gate.describe()["shipped_write_tools"], 0)

    def test_hourly_quota(self):
        config = GateConfig(hourly_request_budget=2, call_delay_min_ms=0, call_delay_max_ms=0)

        async def scenario():
            gate = AsyncRateGate(config)
            calls = []

            async def ok():
                calls.append(1)
                return "ok"

            await gate.guarded("t", ok)
            await gate.guarded("t", ok)
            with self.assertRaises(DouyinError) as ctx:
                await gate.guarded("t", ok)
            self.assertEqual(ctx.exception.code, "rate_limited")
            self.assertEqual(len(calls), 2)
            self.assertEqual(gate.quota()["used"], 2)
            self.assertEqual(gate.quota()["remaining"], 0)

        asyncio.run(scenario())

    def test_backoff_on_rate_limited(self):
        config = GateConfig(hourly_request_budget=10, call_delay_min_ms=0, call_delay_max_ms=0)

        async def scenario():
            gate = AsyncRateGate(config)

            async def boom():
                raise DouyinError("rate_limited", "限流")

            with self.assertRaises(DouyinError):
                await gate.guarded("t", boom)
            self.assertGreater(gate.backoff_seconds(), 0)

        asyncio.run(scenario())


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = DouyinStore(Path(self._tmp.name) / "test.sqlite3")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_identity_isolation(self):
        alice = identity_key({"scope": {"installation": "main", "bot": "b1"}, "user": "alice"})
        bob = identity_key({"scope": {"installation": "main", "bot": "b1"}, "user": "bob"})
        self.assertNotEqual(alice, bob)
        self.store.put_video(alice, "7123456789012345678", title="一起看的猫猫视频", author="某个博主")
        self.assertEqual(len(self.store.search_videos(alice, "猫猫", 5, 1000)), 1)
        self.assertEqual(len(self.store.search_videos(bob, "猫猫", 5, 1000)), 0)

    def test_budget_skips_whole_item(self):
        key = identity_key({"user": "alice"})
        self.store.put_video(key, "7123456789012345678", title="很长很长的标题" * 10)
        self.assertEqual(self.store.search_videos(key, "", 5, 10), [])

    def test_idempotent_record_upsert(self):
        record = {
            "source_plugin": "astrbot_plugin_private_companion",
            "memory_id": "journal-2026-11-15",
            "kind": "persona_life",
            "content": "今天一起看了视频",
            "context": {"scope": {"installation": "main"}, "role": "r1"},
        }
        self.assertEqual(self.store.upsert_record(record), "journal-2026-11-15")
        self.assertEqual(self.store.upsert_record(record), "journal-2026-11-15")
        counts = self.store.counts(identity_key(record["context"]))
        self.assertEqual(counts["total"], 1)
        self.assertEqual(set(counts), {"profile", "preference", "relationship",
                                       "private_conversation", "other", "total"})

    def test_reject_unowned_record(self):
        self.assertIsNone(self.store.upsert_record({"source_plugin": "x", "memory_id": "y"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
