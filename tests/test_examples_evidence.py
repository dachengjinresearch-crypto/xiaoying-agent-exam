"""T15：证据完整性核验。

这些用例不测代码逻辑，而是测**提交材料本身**：
文件齐不齐、轨迹是否符合固定 schema、命令里有没有泄漏内部路径、
冲突场景的轨迹是否真的体现了"因工具结果而调整动作"。

如果 `examples/` 被重新生成而结构变了，这里会第一时间失败。
"""

import json
import unittest
from pathlib import Path

from src.observability import TRACE_FIELDS

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

CASES = [
    "normal",
    "missing_info",
    "missing_info_resolved",
    "rule_risk",
    "conflict",
    "save_failure",
    "injection",
    "full",
]

AUDIT_KEYS = {"ts", "tool", "args", "ok", "status", "error_code"}


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestEvidenceFiles(unittest.TestCase):
    def test_all_case_inputs_present(self):
        for case in CASES:
            with self.subTest(case=case):
                self.assertTrue((EXAMPLES / "cases" / f"{case}.json").is_file())

    def test_all_run_records_present(self):
        for case in CASES:
            for suffix in ("txt", "jsonl"):
                with self.subTest(case=case, suffix=suffix):
                    path = EXAMPLES / f"run_{case}.{suffix}"
                    self.assertTrue(path.is_file(), f"缺少 {path.name}")
                    self.assertGreater(path.stat().st_size, 0)

    def test_aggregate_files_present(self):
        for name in ("audit.jsonl", "verification.txt", "unittest_output.txt", "README.md"):
            with self.subTest(file=name):
                self.assertTrue((EXAMPLES / name).is_file())

    def test_run_record_has_command_and_exit_code(self):
        for case in CASES:
            text = (EXAMPLES / f"run_{case}.txt").read_text(encoding="utf-8")
            with self.subTest(case=case):
                self.assertIn("# 命令：python -m src.cli", text)
                self.assertIn("# 退出码：0", text)


class TestNoInternalPathLeak(unittest.TestCase):
    def test_no_sandbox_or_absolute_paths(self):
        markers = ("/sessions/", "C:\\", "/home/", "/tmp/")
        for path in sorted(EXAMPLES.glob("*.txt")) + sorted(EXAMPLES.glob("*.jsonl")):
            text = path.read_text(encoding="utf-8")
            for marker in markers:
                with self.subTest(file=path.name, marker=marker):
                    self.assertNotIn(marker, text, f"{path.name} 泄漏了内部路径")


class TestVerification(unittest.TestCase):
    def test_verification_reports_zero_failures(self):
        text = (EXAMPLES / "verification.txt").read_text(encoding="utf-8")
        self.assertIn("总计失败项: 0", text)
        self.assertNotIn("[FAIL]", text)

    def test_verification_covers_all_cases(self):
        text = (EXAMPLES / "verification.txt").read_text(encoding="utf-8")
        for case in CASES:
            with self.subTest(case=case):
                self.assertIn(f"=== {case} ===", text)


class TestTraceSchema(unittest.TestCase):
    def test_every_line_matches_fixed_schema(self):
        for case in CASES:
            records = read_jsonl(EXAMPLES / f"run_{case}.jsonl")
            with self.subTest(case=case):
                self.assertTrue(records, "轨迹不应为空")
                for record in records:
                    self.assertEqual(tuple(record), TRACE_FIELDS)

    def test_run_id_is_consistent_within_file(self):
        for case in CASES:
            records = read_jsonl(EXAMPLES / f"run_{case}.jsonl")
            with self.subTest(case=case):
                # conflict 用例带 --repeat，会有两个 run_id
                expected = {f"run-{case}"}
                if case == "conflict":
                    expected.add("run-conflict-repeat")
                self.assertTrue({r["run_id"] for r in records} <= expected)

    def test_each_trace_has_decision_and_final(self):
        for case in CASES:
            events = {r["event"] for r in read_jsonl(EXAMPLES / f"run_{case}.jsonl")}
            with self.subTest(case=case):
                self.assertIn("model_decision", events)
                self.assertIn("final", events)


class TestTwoExecutionPathsShowAdaptation(unittest.TestCase):
    """题目要求：至少两条路径能看出模型因不同工具结果而调整动作。"""

    def test_conflict_path_reschedules_after_slot_conflict(self):
        records = read_jsonl(EXAMPLES / "run_conflict.jsonl")

        # 1) 工具返回：18:00 占用，19:00 可用
        slot_results = [
            r for r in records
            if r["event"] == "tool_result" and r["action"] == "query_slots"
        ]
        self.assertTrue(slot_results)
        self.assertIn("18:00", slot_results[0]["result_summary"])
        self.assertIn("19:00", slot_results[0]["result_summary"])

        # 2) 后续保存动作改用 19:00，并标记待确认
        saves = [
            r for r in records
            if r["event"] == "model_decision" and r["action"] == "save_draft"
        ]
        self.assertTrue(saves)
        for save in saves:
            with self.subTest(step=save["step"]):
                self.assertEqual(save["tool_args"]["slot"], "2026-11-05T19:00")
                self.assertTrue(save["tool_args"]["pending_confirmation"])
                self.assertIn("19:00", save["reason"])

    def test_rule_risk_path_stops_after_block(self):
        records = read_jsonl(EXAMPLES / "run_rule_risk.jsonl")

        rule_results = [
            r for r in records
            if r["event"] == "tool_result" and r["action"] == "check_rules"
        ]
        self.assertTrue(rule_results)
        self.assertIn("block", rule_results[0]["result_summary"])

        # 命中 block 之后不应再出现保存动作
        actions_after = [
            r["action"] for r in records
            if r["event"] == "model_decision" and r["step"] > rule_results[0]["step"]
        ]
        self.assertNotIn("save_draft", actions_after)
        self.assertNotIn("query_slots", actions_after)


class TestAuditEvidence(unittest.TestCase):
    def test_audit_schema(self):
        entries = read_jsonl(EXAMPLES / "audit.jsonl")
        self.assertGreater(len(entries), 20)
        for entry in entries:
            self.assertEqual(set(entry), AUDIT_KEYS)

    def test_failed_save_is_recorded(self):
        entries = read_jsonl(EXAMPLES / "audit.jsonl")
        failed = [
            e for e in entries
            if e["tool"] == "save_draft" and not e["ok"]
        ]
        self.assertTrue(failed, "保存失败必须留下审计记录")
        self.assertIn("E_STORAGE_WRITE", {e["error_code"] for e in failed})

    def test_successful_saves_are_recorded(self):
        entries = read_jsonl(EXAMPLES / "audit.jsonl")
        ok_saves = [e for e in entries if e["tool"] == "save_draft" and e["ok"]]
        self.assertGreaterEqual(len(ok_saves), 4)


if __name__ == "__main__":
    unittest.main()
