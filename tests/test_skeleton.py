"""T0 验收：工程骨架与约定。

通过标准（docs/06-task-plan.md T0）：
- import src 无错
- .env.example 存在且注明"不配置也能离线运行"
- 状态枚举与 docs/02-solution.md §6 一致，且不存在"已发布"类取值

运行：python -m unittest discover -s tests -t . -v
"""

import unittest
from pathlib import Path

import src
from src import config, enums

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestSkeleton(unittest.TestCase):
    def test_package_importable(self):
        self.assertTrue(src.__version__)

    def test_required_dirs_exist(self):
        for name in ("src", "data", "tests", "examples", "docs"):
            with self.subTest(dir=name):
                self.assertTrue((PROJECT_ROOT / name).is_dir(), f"缺少目录 {name}")

    def test_required_files_exist(self):
        self.assertTrue((PROJECT_ROOT / "requirements.txt").is_file())
        self.assertTrue((PROJECT_ROOT / ".env.example").is_file())

    def test_env_example_states_offline_works_without_config(self):
        text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("不配置任何变量也能离线运行", text)
        self.assertIn("--offline", text)

    def test_asset_state_matches_spec(self):
        expected = {
            "INIT",
            "ASSET_SELECTED",
            "DETAIL_LOADED",
            "NEEDS_INFO",
            "RULES_READ",
            "BLOCKED",
            "SLOTS_READ",
            "DRAFT_SAVED",
            "SAVE_FAILED",
            "LIMIT_EXCEEDED",
        }
        self.assertEqual({s.value for s in enums.AssetState}, expected)

    def test_draft_status_has_no_publish_state(self):
        values = {s.value for s in enums.DraftStatus}
        self.assertEqual(values, {"not_attempted", "draft_saved", "failed", "blocked"})
        for forbidden in ("published", "live", "released"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, values)

    def test_run_status_values(self):
        self.assertEqual(
            {s.value for s in enums.RunStatus},
            {"completed", "partial", "incomplete", "failed"},
        )

    def test_budget_constants_are_bounded(self):
        # 题目硬性要求：必须有步数 / 工具调用上限
        self.assertGreaterEqual(config.MAX_STEPS, 20, "上限过低会导致跑不完 7 个素材")
        self.assertLessEqual(config.MAX_STEPS, 500)
        self.assertGreaterEqual(config.MAX_STEPS_PER_ASSET, 4)
        self.assertLessEqual(config.MAX_STEPS_PER_ASSET, 15)
        self.assertGreater(config.MAX_TOOL_CALLS, 0)
        self.assertLessEqual(config.MAX_TOOL_CALLS, config.MAX_STEPS)
        self.assertGreater(config.PER_TOOL_TIMEOUT_S, 0)
        self.assertGreater(config.TOTAL_WALL_S, 0)

    def test_retryable_error_set(self):
        self.assertTrue(enums.is_retryable(enums.ErrorCode.E_SLOT_CONFLICT))
        self.assertTrue(enums.is_retryable(enums.ErrorCode.E_STORAGE_WRITE))
        # 不可重试的错误不能被当成可重试，否则模型会无脑重试
        self.assertFalse(enums.is_retryable(enums.ErrorCode.E_RULE_BLOCKED))
        self.assertFalse(enums.is_retryable(enums.ErrorCode.E_NOT_FOUND))
        self.assertFalse(enums.is_retryable("E_UNKNOWN"))

    def test_error_codes_complete(self):
        codes = [
            "E_NOT_FOUND",
            "E_MISSING_FIELDS",
            "E_RULE_BLOCKED",
            "E_SLOT_CONFLICT",
            "E_STORAGE_WRITE",
            "E_STORAGE_LOCK",
            "E_TIMEOUT",
            "E_UPSTREAM_MODEL",
            "E_SCHEMA_INVALID",
            "E_LIMIT_EXCEEDED",
            "E_INTEGRITY",
        ]
        for code in codes:
            with self.subTest(code=code):
                self.assertEqual(enums.ErrorCode(code).value, code)


if __name__ == "__main__":
    unittest.main()
