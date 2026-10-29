"""T12 验收（下）：CLI。

覆盖：正常用例、重复提交、JSON 输出、保存失败、未知用例、诚实层拦截。
"""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from src.cli import main
from src.config import DATA_FILES

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class CliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name in DATA_FILES:
            shutil.copy(DATA_DIR / f"{name}.json", self.root / f"{name}.json")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*args, "--data-dir", str(self.root)])
        return code, out.getvalue(), err.getvalue()


class TestCliHappyPath(CliCase):
    def test_conflict_case_succeeds(self):
        code, out, err = self.run_cli("--case", "conflict", "--offline")
        self.assertEqual(code, 0, err)
        self.assertIn("草稿已保存", out)
        self.assertIn("待确认", out)
        self.assertIn("M01", out)
        self.assertIn("2026-11-05T19:00", out)

    def test_output_never_claims_publish(self):
        code, out, _ = self.run_cli("--case", "conflict", "--offline")
        self.assertEqual(code, 0)
        self.assertNotIn("已发布", out)
        self.assertNotIn("published", out.lower())

    def test_normal_case_completed(self):
        code, out, _ = self.run_cli("--case", "normal", "--offline")
        self.assertEqual(code, 0)
        self.assertIn("status       : completed", out)
        self.assertIn("已保存草稿=1", out)

    def test_json_output_is_valid(self):
        code, out, _ = self.run_cli("--case", "normal", "--offline", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["primary"]["status"], "completed")
        self.assertEqual(payload["primary"]["assets"][0]["asset_id"], "M02")

    def test_data_dir_not_polluted_with_lock(self):
        self.run_cli("--case", "normal", "--offline")
        self.assertFalse((self.root / ".drafts.lock").exists())


class TestCliRepeat(CliCase):
    def test_repeat_is_idempotent(self):
        code, out, err = self.run_cli("--case", "conflict", "--offline", "--repeat")
        self.assertEqual(code, 0, err)
        self.assertIn("重复提交校验", out)
        self.assertIn("一致（幂等）", out)
        self.assertNotIn("不一致", out)

    def test_repeat_produces_single_draft(self):
        self.run_cli("--case", "conflict", "--offline", "--repeat")
        store = json.loads((self.root / "drafts.json").read_text(encoding="utf-8"))
        self.assertEqual(list(store["drafts"]), ["M01"])
        self.assertEqual(store["next_seq"], 2)


class TestCliFailureCases(CliCase):
    def test_save_failure_reports_failed(self):
        code, out, err = self.run_cli("--case", "save_failure", "--offline")
        self.assertEqual(code, 0, err)
        self.assertIn("保存失败", out)
        self.assertNotIn("草稿已保存", out)

    def test_save_failure_creates_no_draft(self):
        self.run_cli("--case", "save_failure", "--offline")
        self.assertFalse((self.root / "drafts.json").exists())

    def test_rule_risk_is_blocked(self):
        code, out, _ = self.run_cli("--case", "rule_risk", "--offline")
        self.assertEqual(code, 0)
        self.assertIn("已阻断", out)
        self.assertIn("R2", out)

    def test_unknown_case_exits_nonzero(self):
        code, _, err = self.run_cli("--case", "nope", "--offline")
        self.assertEqual(code, 1)
        self.assertIn("未知用例", err)


class TestCliHonestyGate(CliCase):
    def test_honesty_violation_blocks_output(self):
        with mock.patch("src.cli.render", return_value="M01 已发布\n"):
            code, out, err = self.run_cli("--case", "normal", "--offline")
        self.assertEqual(code, 2)
        self.assertIn("HONESTY VIOLATION", err)
        self.assertEqual(out, "", "违反诚实约束时不得输出任何结果")

    def test_injected_publish_wording_is_caught(self):
        # 演示：一旦渲染层出错，诚实层会拦住，而不是把假话打给用户
        with mock.patch("src.cli.render", return_value="M02 发布成功\n"):
            code, _, err = self.run_cli("--case", "normal", "--offline")
        self.assertEqual(code, 2)
        self.assertIn("禁用表述", err)


class TestCliListCases(CliCase):
    def test_list_cases(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--list-cases"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        for name in ("normal", "conflict", "save_failure", "injection", "full"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
