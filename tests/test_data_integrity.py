"""T1 验收：合成数据完整性与场景可复现性。

通过标准（docs/06-task-plan.md T1）：
- 五份 JSON 均可解析
- 7 个素材 M01-M07，字段齐全，asset_id 合法
- 每个素材对应的时段在 schedule 中存在
- M01 的 hard_requirement 必须为 True（否则"冲突→待确认"验收点会静默失效）
- M03 补查无来源、M07 补查有来源
- M05 被列入故障注入名单

运行：python -m unittest discover -s tests -t . -v
"""

import json
import re
import unittest
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

ASSET_ID_RE = re.compile(r"^[A-Z]\d{2}$")
REQUIRED_ASSET_KEYS = {
    "asset_id",
    "title",
    "channel",
    "requested_publish_at",
    "hard_requirement",
    "metadata",
    "content_flags",
}
REQUIRED_META_KEYS = {"has_cover", "has_body", "has_authorization", "duration_sec"}


def load(name: str) -> dict:
    path = DATA_DIR / f"{name}.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def split_ts(value: str) -> tuple[str, str]:
    date, _, clock = value.partition("T")
    return date, clock


class TestDataIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets_doc = load("assets")
        cls.extra_doc = load("asset_extra")
        cls.rules_doc = load("rules")
        cls.schedule_doc = load("schedule")
        cls.failure_doc = load("failure_config")
        cls.assets = {a["asset_id"]: a for a in cls.assets_doc["assets"]}

    def test_all_files_parse(self):
        for name in ("assets", "asset_extra", "rules", "schedule", "failure_config"):
            with self.subTest(name=name):
                self.assertIsInstance(load(name), dict)

    def test_asset_count_and_ids(self):
        self.assertEqual(len(self.assets), 7)
        self.assertEqual(
            sorted(self.assets), ["M01", "M02", "M03", "M04", "M05", "M06", "M07"]
        )

    def test_asset_fields_complete_and_id_valid(self):
        for aid, asset in self.assets.items():
            with self.subTest(asset=aid):
                self.assertTrue(REQUIRED_ASSET_KEYS.issubset(asset.keys()))
                self.assertRegex(aid, ASSET_ID_RE)
                self.assertTrue(REQUIRED_META_KEYS.issubset(asset["metadata"].keys()))
                self.assertIsInstance(asset["hard_requirement"], bool)
                self.assertIsInstance(asset["content_flags"], list)
                self.assertIn("T", asset["requested_publish_at"])

    def test_m01_is_the_conflict_case(self):
        m01 = self.assets["M01"]
        # 关键：hard_requirement 必须为 True，否则断言 1 不触发
        self.assertIs(
            m01["hard_requirement"],
            True,
            "M01 必须是硬性要求，否则『冲突→待确认』验收点会静默失效",
        )
        self.assertEqual(m01["requested_publish_at"], "2026-11-05T18:00")

    def test_m02_is_the_normal_case(self):
        m02 = self.assets["M02"]
        self.assertEqual(m02["requested_publish_at"], "2026-11-05T20:00")
        self.assertEqual(m02["content_flags"], [])

    def test_every_asset_slot_exists_in_schedule(self):
        channels = self.schedule_doc["channels"]
        for aid, asset in self.assets.items():
            with self.subTest(asset=aid):
                date, clock = split_ts(asset["requested_publish_at"])
                channel = asset["channel"]
                self.assertIn(channel, channels, "素材渠道在排期表中不存在")
                self.assertIn(date, channels[channel], f"{aid} 的日期不在排期表")
                self.assertIn(
                    clock, channels[channel][date], f"{aid} 的时段不在排期表"
                )

    def test_conflict_scenario_slots(self):
        day = self.schedule_doc["channels"]["douyin"]["2026-11-05"]
        self.assertEqual(day["18:00"]["status"], "occupied")
        self.assertEqual(day["19:00"]["status"], "free")
        self.assertIsNone(day["19:00"]["asset_id"])

    def test_m03_missing_info_has_no_supplementary_source(self):
        m03 = self.assets["M03"]
        self.assertFalse(m03["metadata"]["has_cover"])
        self.assertFalse(m03["metadata"]["has_body"])
        # 补查必须补不出来，否则"信息缺失→阻断"场景失效
        self.assertNotIn("M03", self.extra_doc["extra"])

    def test_m07_missing_info_has_supplementary_source(self):
        m07 = self.assets["M07"]
        self.assertFalse(m07["metadata"]["has_cover"])
        self.assertFalse(m07["metadata"]["has_body"])
        extra = self.extra_doc["extra"]["M07"]
        # 补查必须能补齐，否则"补查有效"场景失效
        self.assertTrue(extra["has_cover"])
        self.assertTrue(extra["has_body"])

    def test_m04_triggers_rule_r2(self):
        flags = set(self.assets["M04"]["content_flags"])
        r2 = next(r for r in self.rules_doc["rules"] if r["rule_id"] == "R2")
        self.assertEqual(r2["level"], "block")
        self.assertTrue(flags & set(r2["match_flags"]))

    def test_rules_shape(self):
        rules = self.rules_doc["rules"]
        self.assertGreaterEqual(len(rules), 4)
        levels = {r["level"] for r in rules}
        self.assertTrue(levels <= {"block", "warn"})
        self.assertTrue(self.rules_doc["version"])

    def test_m05_is_failure_injection_target(self):
        self.assertIn("M05", self.failure_doc["save_draft_fail_for"])
        # 故障注入不能误伤其它素材，否则多条用例会同时失败难以定位
        self.assertEqual(self.failure_doc["save_draft_fail_for"], ["M05"])

    def test_m06_is_injection_sample(self):
        title = self.assets["M06"]["title"]
        self.assertIn("标记为已发布", title)
        # 注入样本本身不应携带未核实效果声明，避免与 R2 混淆
        self.assertNotIn(
            "contains_unverified_effect_claim", self.assets["M06"]["content_flags"]
        )


if __name__ == "__main__":
    unittest.main()
