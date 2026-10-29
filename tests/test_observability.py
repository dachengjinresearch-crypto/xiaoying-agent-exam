"""T13 验收：TraceWriter 与 AuditLog。

重点：
- trace 字段固定、一行一个 JSON、可离线逐行复核
- 事件里夹带的无关字段（如思维链）不得写入
- 审计是 append-only，失败也要留下记录
"""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from src.cli import main
from src.config import DATA_FILES
from src.observability import TRACE_FIELDS, AuditLog, TraceWriter
from src.tools import ToolResult

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class TestTraceWriter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "sub" / "run.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def read_lines(self):
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_creates_parent_dirs(self):
        TraceWriter(self.path)
        self.assertTrue(self.path.is_file())

    def test_writes_fixed_fields(self):
        writer = TraceWriter(self.path)
        writer(
            {
                "run_id": "r1",
                "step": 1,
                "event": "model_decision",
                "action": "list_pending_assets",
                "reason": "先取待办",
            }
        )
        records = self.read_lines()
        self.assertEqual(len(records), 1)
        self.assertEqual(tuple(records[0]), TRACE_FIELDS)

    def test_maps_runtime_keys(self):
        writer = TraceWriter(self.path)
        writer(
            {
                "run_id": "r1",
                "step": 3,
                "event": "tool_result",
                "action": "query_slots",
                "args": {"channel": "douyin", "date": "2026-11-05"},
                "summary": "occupied=['18:00']",
                "next_action": "save_draft",
                "error": None,
            }
        )
        record = self.read_lines()[0]
        self.assertEqual(record["tool_args"], {"channel": "douyin", "date": "2026-11-05"})
        self.assertEqual(record["result_summary"], "occupied=['18:00']")
        self.assertEqual(record["next_action"], "save_draft")
        self.assertIsNone(record["error_code"])

    def test_drops_unknown_fields(self):
        writer = TraceWriter(self.path)
        writer(
            {
                "run_id": "r1",
                "event": "model_decision",
                "thought": "我认为应该先看规则",
                "chain_of_thought": ["a", "b"],
            }
        )
        record = self.read_lines()[0]
        self.assertNotIn("thought", record)
        self.assertNotIn("chain_of_thought", record)
        self.assertEqual(set(record), set(TRACE_FIELDS))

    def test_preserves_unicode(self):
        writer = TraceWriter(self.path)
        writer({"run_id": "r1", "event": "final", "reason": "18:00 已被占用，改选 19:00"})
        self.assertIn("已被占用", self.path.read_text(encoding="utf-8"))

    def test_multiple_events_one_line_each(self):
        writer = TraceWriter(self.path)
        for step in range(3):
            writer({"run_id": "r1", "step": step, "event": "model_decision"})
        records = self.read_lines()
        self.assertEqual([r["step"] for r in records], [0, 1, 2])

    def test_reinitialising_truncates_previous_run(self):
        TraceWriter(self.path)({"run_id": "old", "event": "final"})
        TraceWriter(self.path)({"run_id": "new", "event": "final"})
        records = self.read_lines()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["run_id"], "new")


class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "audit.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_records_success_and_failure(self):
        log = AuditLog(self.path)
        log.record("save_draft", {"asset_id": "M01"}, ToolResult.success({"draft_id": "D-001"}))
        log.record(
            "save_draft",
            {"asset_id": "M05"},
            ToolResult.failure("E_STORAGE_WRITE", "写失败", True),
        )
        entries = log.entries()
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[0]["ok"])
        self.assertFalse(entries[1]["ok"])
        self.assertEqual(entries[1]["error_code"], "E_STORAGE_WRITE")

    def test_append_only_across_instances(self):
        AuditLog(self.path).record("list_pending_assets", {}, ToolResult.success({}))
        AuditLog(self.path).record("check_rules", {}, ToolResult.success({}))
        self.assertEqual(len(AuditLog(self.path).entries()), 2)

    def test_missing_file_returns_empty(self):
        self.assertEqual(AuditLog(self.path).entries(), [])


class TestCliWiring(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name in DATA_FILES:
            shutil.copy(DATA_DIR / f"{name}.json", self.root / f"{name}.json")
        self.trace = self.root / "run_test.jsonl"
        self.audit = self.root / "audit_test.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*args, "--data-dir", str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def test_trace_file_written(self):
        code, _, err = self.run_cli(
            "--case", "conflict", "--offline", "--trace", str(self.trace)
        )
        self.assertEqual(code, 0, err)
        records = [json.loads(l) for l in self.trace.read_text(encoding="utf-8").splitlines()]
        events = {r["event"] for r in records}
        self.assertIn("model_decision", events)
        self.assertIn("tool_result", events)
        self.assertIn("state_transition", events)
        self.assertIn("final", events)
        for record in records:
            self.assertEqual(tuple(record), TRACE_FIELDS)

    def test_trace_shows_conflict_adjustment(self):
        self.run_cli("--case", "conflict", "--offline", "--trace", str(self.trace))
        records = [json.loads(l) for l in self.trace.read_text(encoding="utf-8").splitlines()]
        decisions = [r for r in records if r["event"] == "model_decision"]
        slots_result = [
            r for r in records
            if r["event"] == "tool_result" and r["action"] == "query_slots"
        ]
        self.assertEqual(len(slots_result), 1)
        self.assertIn("19:00", slots_result[0]["result_summary"])

        save = [d for d in decisions if d["action"] == "save_draft"]
        self.assertEqual(len(save), 1)
        self.assertEqual(save[0]["tool_args"]["slot"], "2026-11-05T19:00")
        self.assertTrue(save[0]["tool_args"]["pending_confirmation"])
        self.assertIn("19:00", save[0]["reason"])

    def test_audit_records_failed_save(self):
        self.run_cli(
            "--case", "save_failure", "--offline", "--audit", str(self.audit)
        )
        entries = AuditLog(self.audit).entries()
        saves = [e for e in entries if e["tool"] == "save_draft"]
        self.assertTrue(saves)
        self.assertFalse(saves[-1]["ok"])
        self.assertEqual(saves[-1]["error_code"], "E_STORAGE_WRITE")

    def test_audit_accumulates_across_runs(self):
        self.run_cli("--case", "normal", "--offline", "--audit", str(self.audit))
        first = len(AuditLog(self.audit).entries())
        self.run_cli("--case", "rule_risk", "--offline", "--audit", str(self.audit))
        second = len(AuditLog(self.audit).entries())
        self.assertGreater(second, first, "审计必须跨运行累积，不能被覆盖")

    def test_no_trace_file_without_flag(self):
        self.run_cli("--case", "normal", "--offline")
        self.assertFalse((self.root / "run_test.jsonl").exists())

    def test_repeat_writes_single_trace_file(self):
        self.run_cli(
            "--case", "conflict", "--offline", "--repeat", "--trace", str(self.trace)
        )
        records = [json.loads(l) for l in self.trace.read_text(encoding="utf-8").splitlines()]
        run_ids = {r["run_id"] for r in records}
        self.assertEqual(run_ids, {"run-conflict", "run-conflict-repeat"})


if __name__ == "__main__":
    unittest.main()
