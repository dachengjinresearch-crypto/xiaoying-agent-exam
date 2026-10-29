"""T4 验收：RuleEngine。

通过标准（docs/06-task-plan.md T4）：
- M04 命中 R2 且 level=block
- M01 / M02 无命中（「无风险」素材不能被误报）
- 返回全部命中项，不只第一条
- 结果携带 rules_version
- M06 注入样本不改变规则判定（注入只影响模型，不影响结构化规则）
"""

import tempfile
import unittest
from pathlib import Path

from src.repository import Repository
from src.services import RuleEngine

RULES = [
    {
        "rule_id": "R1",
        "desc": "四要素齐全",
        "level": "block",
        "check": "metadata_completeness",
    },
    {
        "rule_id": "R2",
        "desc": "不得包含未核实效果数据",
        "level": "block",
        "check": "content_flags",
        "match_flags": ["contains_unverified_effect_claim"],
    },
    {
        "rule_id": "R3",
        "desc": "30 分钟内不得连发",
        "level": "warn",
        "check": "channel_interval",
        "interval_minutes": 30,
    },
    {
        "rule_id": "R4",
        "desc": "发布须在 09:00-22:00",
        "level": "block",
        "check": "publish_window",
        "window": ["09:00", "22:00"],
    },
]

FULL_META = {
    "has_cover": True,
    "has_body": True,
    "has_authorization": True,
    "duration_sec": 30,
}


def make_asset(asset_id="M01", ts="2026-11-05T18:00", flags=(), meta=None, hard=True):
    return {
        "asset_id": asset_id,
        "title": "测试素材",
        "channel": "douyin",
        "requested_publish_at": ts,
        "hard_requirement": hard,
        "metadata": dict(meta or FULL_META),
        "content_flags": list(flags),
    }


DEFAULT_CHANNELS = {
    "douyin": {
        "2026-11-05": {
            "18:00": {"status": "occupied", "asset_id": "M00"},
            "19:00": {"status": "free", "asset_id": None},
            "20:00": {"status": "occupied", "asset_id": "M00"},
            "20:10": {"status": "free", "asset_id": None},
            "23:30": {"status": "free", "asset_id": None},
        }
    }
}


class _TempRepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make_repo(self, assets, channels=None):
        repo = Repository(self.root)
        repo.dump("assets", {"version": "t", "assets": assets})
        repo.dump("asset_extra", {"version": "t", "extra": {}})
        repo.dump("rules", {"version": "t-rules", "rules": RULES})
        repo.dump("schedule", {"version": "t", "channels": channels or DEFAULT_CHANNELS})
        repo.dump("failure_config", {"version": "t", "save_draft_fail_for": []})
        return repo


class TestRuleEngineRealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = RuleEngine(Repository())

    def test_m01_has_no_risk(self):
        result = self.engine.check("M01")
        self.assertEqual(result["risk_level"], "none")
        self.assertEqual(result["hits"], [])

    def test_m02_has_no_risk(self):
        self.assertEqual(self.engine.check("M02")["risk_level"], "none")

    def test_m03_blocked_by_r1(self):
        result = self.engine.check("M03")
        self.assertEqual(result["risk_level"], "block")
        self.assertEqual([h["rule_id"] for h in result["hits"]], ["R1"])
        self.assertIn("has_cover", result["hits"][0]["reason"])

    def test_m04_blocked_by_r2(self):
        result = self.engine.check("M04")
        self.assertEqual(result["risk_level"], "block")
        self.assertEqual([h["rule_id"] for h in result["hits"]], ["R2"])

    def test_m06_injection_does_not_change_rule_result(self):
        # 素材标题里的指令不能影响结构化规则判定
        self.assertEqual(self.engine.check("M06")["risk_level"], "none")

    def test_result_carries_rules_version(self):
        self.assertEqual(self.engine.check("M01")["rules_version"], "2026-10-01")

    def test_is_blocked_helper(self):
        self.assertTrue(self.engine.is_blocked(self.engine.check("M04")))
        self.assertFalse(self.engine.is_blocked(self.engine.check("M01")))


class TestRuleEngineBranches(_TempRepoCase):
    def test_r3_warns_when_too_close(self):
        # 20:00 已占用，素材要求 20:10，间隔 10 分钟 < 30
        repo = self.make_repo([make_asset(ts="2026-11-05T20:10")])
        result = RuleEngine(repo).check("M01")
        self.assertEqual(result["risk_level"], "warn")
        self.assertEqual([h["rule_id"] for h in result["hits"]], ["R3"])

    def test_r3_ignores_same_slot(self):
        # 同槽冲突归 ScheduleService，不应被 R3 重复报告
        repo = self.make_repo([make_asset(ts="2026-11-05T20:00")])
        result = RuleEngine(repo).check("M01")
        self.assertEqual(result["risk_level"], "none")

    def test_r4_blocks_out_of_window(self):
        repo = self.make_repo([make_asset(ts="2026-11-05T23:30")])
        result = RuleEngine(repo).check("M01")
        self.assertEqual(result["risk_level"], "block")
        self.assertIn("R4", [h["rule_id"] for h in result["hits"]])

    def test_returns_all_hits_not_just_first(self):
        # 同时缺字段 + 未核实效果 + 间隔过近 → 三条命中都要返回
        broken_meta = dict(FULL_META, has_cover=False)
        repo = self.make_repo(
            [
                make_asset(
                    ts="2026-11-05T20:10",
                    flags=["contains_unverified_effect_claim"],
                    meta=broken_meta,
                )
            ]
        )
        result = RuleEngine(repo).check("M01")
        self.assertEqual(result["risk_level"], "block")
        self.assertEqual(
            sorted(h["rule_id"] for h in result["hits"]), ["R1", "R2", "R3"]
        )
        for hit in result["hits"]:
            self.assertTrue(hit["reason"])

    def test_warn_does_not_outrank_block(self):
        broken_meta = dict(FULL_META, has_body=False)
        repo = self.make_repo(
            [make_asset(ts="2026-11-05T20:10", meta=broken_meta)]
        )
        result = RuleEngine(repo).check("M01")
        self.assertEqual(result["risk_level"], "block")


if __name__ == "__main__":
    unittest.main()
