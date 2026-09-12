"""gain（应用端每周奖励领取）配置解析、校验与领奖流程单元测试。"""
import asyncio
import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from tests.helpers import load_weread_bot


def _make_response(payload, status_code=200, url="https://i.weread.qq.com/x"):
    request = httpx.Request("POST", url)
    response = httpx.Response(
        status_code, json=payload, request=request
    )
    return response


class FakeHttpClient:
    """按顺序回放预置响应的 HttpClient 替身。"""

    def __init__(self, script):
        # script: list of (payload, status_code) 或 Exception 实例
        self.script = list(script)
        self.requests = []

    async def post_raw(self, url, headers=None, cookies=None,
                       json_data=None, data=None):
        self.requests.append({"url": url, "headers": headers, "json": json_data})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        payload, status_code = item
        response = _make_response(payload, status_code, url)
        if status_code >= 400:
            raise httpx.HTTPStatusError(
                f"client error {status_code}",
                request=response.request,
                response=response,
            )
        return response, 0.01

    async def close(self):
        pass


class GainConfigParsingTests(unittest.TestCase):
    def setUp(self):
        self.bot = load_weread_bot()

    def _config_file(self, text):
        temp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        temp.write(text)
        temp.close()
        self.addCleanup(lambda: Path(temp.name).unlink(missing_ok=True))
        return temp.name

    def test_gain_section_defaults(self):
        path = self._config_file("network:\n  timeout: 10\n")
        config = self.bot.ConfigManager(path).config
        self.assertFalse(config.gain.enabled)
        self.assertEqual(config.gain.gain_type, 1)
        self.assertEqual(config.gain.refresh_token, "")
        self.assertEqual(config.gain.device_id, "")

    def test_gain_section_from_yaml(self):
        path = self._config_file(
            "gain:\n"
            "  enabled: true\n"
            "  gain_type: 2\n"
            "  refresh_token: rt-yaml\n"
            "  device_id: dev-yaml\n"
        )
        config = self.bot.ConfigManager(path).config
        self.assertTrue(config.gain.enabled)
        self.assertEqual(config.gain.gain_type, 2)
        self.assertEqual(config.gain.refresh_token, "rt-yaml")
        self.assertEqual(config.gain.device_id, "dev-yaml")

    def test_gain_env_override(self):
        path = self._config_file("gain:\n  enabled: false\n")
        with patch.dict(os.environ, {
            "GAIN_ENABLED": "true",
            "GAIN_REFRESH_TOKEN": "rt-env",
            "GAIN_DEVICE_ID": "dev-env",
            "GAIN_TYPE": "2",
        }):
            config = self.bot.ConfigManager(path).config
        self.assertTrue(config.gain.enabled)
        self.assertEqual(config.gain.refresh_token, "rt-env")
        self.assertEqual(config.gain.device_id, "dev-env")
        self.assertEqual(config.gain.gain_type, 2)

    def test_enabled_without_credentials_raises(self):
        path = self._config_file("gain:\n  enabled: true\n")
        with self.assertRaises(self.bot.ConfigError) as captured:
            self.bot.ConfigManager(path)
        self.assertIn("gain.refresh_token", str(captured.exception))

    def test_enabled_without_device_id_raises(self):
        path = self._config_file(
            "gain:\n  enabled: true\n  refresh_token: rt\n"
        )
        with self.assertRaises(self.bot.ConfigError) as captured:
            self.bot.ConfigManager(path)
        self.assertIn("gain.device_id", str(captured.exception))

    def test_invalid_gain_type_raises(self):
        path = self._config_file(
            "gain:\n  enabled: true\n  gain_type: 3\n"
            "  refresh_token: rt\n  device_id: dev\n"
        )
        with self.assertRaises(self.bot.ConfigError) as captured:
            self.bot.ConfigManager(path)
        self.assertIn("gain.gain_type", str(captured.exception))


class GainManagerFlowTests(unittest.TestCase):
    def setUp(self):
        self.bot = load_weread_bot()

    def _manager(self, script):
        config = self.bot.WeReadConfig()
        config.gain = self.bot.GainConfig(
            enabled=True,
            gain_type=1,
            refresh_token="rt-secret",
            device_id="dev-secret",
        )
        client = FakeHttpClient(script)
        return self.bot.GainManager(config, http_client=client), client

    def test_claims_only_claimable_awards(self):
        manager, client = self._manager([
            ({"accessToken": "tok-123", "vid": 42}, 200),
            ({
                "readtimeAwards": [
                    {"awardLevelId": 1, "awardStatus": 2},
                    {"awardLevelId": 2, "awardStatus": 1},
                ],
                "readdayAwards": [
                    {"awardLevelId": 11, "awardStatus": 0},
                    {"awardLevelId": 12, "awardStatus": 1},
                ],
                "readgoalAwards": [],
            }, 200),
            ({"succ": 1}, 200),
            ({"succ": 1}, 200),
        ])
        summary = asyncio.run(manager.run())

        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["claimable"], 2)
        self.assertEqual(summary["claimed"], [2, 12])

        exchange_bodies = [
            r["json"] for r in client.requests
            if r["url"].endswith("/weekly/exchange")
        ]
        self.assertEqual(len(exchange_bodies), 3)
        self.assertEqual(
            exchange_bodies[0],
            {
                "awardLevelId": 0,
                "awardChoiceType": 0,
                "isExchangeAward": 0,
                "isVisitReadGoal": 1,
                "unread": 1,
                "pf": self.bot.GAIN_PF,
            },
        )
        self.assertEqual(exchange_bodies[1]["awardLevelId"], 2)
        self.assertEqual(exchange_bodies[1]["isExchangeAward"], 1)
        self.assertEqual(exchange_bodies[1]["awardChoiceType"], 1)
        self.assertEqual(exchange_bodies[2]["awardLevelId"], 12)

        login_request = client.requests[0]
        self.assertEqual(login_request["json"]["refreshToken"], "rt-secret")
        self.assertEqual(login_request["json"]["deviceId"], "dev-secret")
        self.assertNotIn("accesstoken", login_request["headers"])

        exchange_with_token = [
            r for r in client.requests
            if r["url"].endswith("/weekly/exchange")
        ]
        self.assertTrue(exchange_with_token)
        for exchange_request in exchange_with_token:
            self.assertEqual(
                exchange_request["headers"]["accesstoken"], "tok-123"
            )
            self.assertEqual(exchange_request["headers"]["vid"], "42")

    def test_no_claimable_awards_skips_claim(self):
        manager, client = self._manager([
            ({"accessToken": "tok-123", "vid": 42}, 200),
            ({
                "readtimeAwards": [{"awardLevelId": 1, "awardStatus": 2}],
                "readdayAwards": [],
                "readgoalAwards": [],
            }, 200),
        ])
        summary = asyncio.run(manager.run())

        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["claimable"], 0)
        self.assertEqual(summary["claimed"], [])
        self.assertEqual(
            len([r for r in client.requests
                 if r["url"].endswith("/weekly/exchange")]),
            1,
        )

    def test_login_failure_reports_error_without_secrets(self):
        manager, _ = self._manager([
            ({"errcode": -2013, "errmsg": "鉴权失败"}, 401),
        ])
        summary = asyncio.run(manager.run())

        self.assertEqual(summary["status"], "failed")
        self.assertIn("-2013", summary["error"])
        rendered = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("rt-secret", rendered)
        self.assertNotIn("dev-secret", rendered)

    def test_partial_claim_failure_is_partial_success(self):
        manager, _ = self._manager([
            ({"accessToken": "tok-123", "vid": 42}, 200),
            ({
                "readtimeAwards": [
                    {"awardLevelId": 2, "awardStatus": 1},
                    {"awardLevelId": 5, "awardStatus": 1},
                ],
                "readdayAwards": [],
                "readgoalAwards": [],
            }, 200),
            ({"succ": 1}, 200),
            ({"errcode": -3002, "errmsg": "奖励已兑换"}, 200),
        ])
        summary = asyncio.run(manager.run())

        self.assertEqual(summary["status"], "partial_success")
        self.assertEqual(summary["claimed"], [2])

    def test_summary_never_contains_access_token(self):
        manager, _ = self._manager([
            ({"accessToken": "tok-secret", "vid": 42}, 200),
            ({
                "readtimeAwards": [],
                "readdayAwards": [],
                "readgoalAwards": [],
            }, 200),
        ])
        summary = asyncio.run(manager.run())
        self.assertNotIn("tok-secret", json.dumps(summary, ensure_ascii=False))


class GainResultReportingTests(unittest.TestCase):
    def setUp(self):
        self.bot = load_weread_bot()

    def test_run_result_summary_includes_gain(self):
        result = self.bot.RunResult(
            final_status="success", user_count=1, successful_users=1,
            gain={"status": "success", "claimed": [2]},
        )
        summary = result.to_summary_dict()
        self.assertEqual(summary["gain"]["claimed"], [2])

    def test_run_result_summary_omits_gain_when_absent(self):
        result = self.bot.RunResult(final_status="success", user_count=1)
        self.assertNotIn("gain", result.to_summary_dict())

    def test_run_history_record_includes_gain(self):
        config = self.bot.WeReadConfig()
        config.history = self.bot.HistoryConfig(enabled=True, file=":memory:")
        record = self.bot.build_run_history_record(
            config=config,
            execution_type="normal",
            run_summary={"gain": {"status": "success", "claimed": [2]}},
        )
        self.assertEqual(record["gain"]["claimed"], [2])

    def test_format_last_run_summary_renders_gain(self):
        text = self.bot.format_last_run_summary({
            "recorded_at": "2026-09-12T12:00:00+08:00",
            "execution_type": "normal",
            "startup_mode": "immediate",
            "final_status": "success",
            "gain": {"status": "success", "claimed": [2, 12], "gain_type": 1},
        })
        self.assertIn("领奖: 已领取档位 [2, 12]", text)


if __name__ == "__main__":
    unittest.main()
