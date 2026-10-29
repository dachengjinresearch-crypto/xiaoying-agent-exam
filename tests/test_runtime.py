"""T10 验收：AgentRuntime 循环、预算、降级与逐素材结算。

重点覆盖：
- 离线全量运行产出正确的逐素材结果（题目五项交付物）
- 四重预算任一触达 → 结构化 incomplete（含 remaining）
- 模型异常 → 切离线并标记 degraded
- 护栏拒绝不是致命错误，模型能收到原因并继续
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.config import DATA_FILES
from src.enums import ErrorCode
from src.planner import Action, OfflineMockPlanner
from src.repository import Repository
from src.runtime import (
    AgentRuntime,
    LIMIT_GLOBAL_STEPS,
    LIMIT_PER_ASSET,
    LIMIT_TOOL_CALLS,
)
from src.tools import ToolRegistry, ToolResult

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class ScriptedPlanner:
    """按脚本返回动作，用于构造确定的边界场景。"""

    kind = "scripted"

    def __init__(self, actions):
        self._actions = list(actions)
        self._index = 0

    def decide(self, observation, state_view):
        if self._index >= len(self._actions):
            return Action("finish", {}, "脚本结束")
        action = self._actions[self._index]
        self._index += 1
        return action


class FailingPlanner:
    kind = "online"

    def decide(self, observation, state_view):
        raise TimeoutError("upstream model timeout")


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name in DATA_FILES:
            shutil.copy(DATA_DIR / f"{name}.json", self.root / f"{name}.json")
        self.registry = ToolRegistry(Repository(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, planner=None, **kwargs):
        return AgentRuntime(self.registry, planner or OfflineMockPlanner(), **kwargs)


class TestFullOfflineRun(RuntimeCase):
    def setUp(self):
        super().setUp()
        self.events = []
        self.result = self.make(trace=self.events.append).run()
        self.by_id = {a["asset_id"]: a for a in self.result.assets}

    def test_all_assets_are_finalized(self):
        self.assertEqual(len(self.result.assets), 7)
        self.assertEqual(sorted(self.by_id), [f"M0{i}" for i in range(1, 8)])

    def test_status_is_partial_because_one_save_failed(self):
        self.assertEqual(self.result.status, "partial")
        self.assertIsNone(self.result.limit_reason)

    def test_summary_counts(self):
        summary = self.result.summary
        self.assertEqual(summary["draft_saved"], 4)
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["needs_info"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["pending_confirmation"], 1)

    def test_conflict_asset_record(self):
        m01 = self.by_id["M01"]
        self.assertEqual(m01["review_status"], "passed")
        self.assertEqual(m01["draft_status"], "draft_saved")
        self.assertEqual(m01["requested_publish_at"], "2026-11-05T18:00")
        self.assertEqual(m01["suggested_slot"], "2026-11-05T19:00")
        self.assertTrue(m01["slot_change_requires_confirmation"])
        self.assertEqual(len(m01["pending_confirmation_items"]), 1)
        self.assertIn("19:00", m01["pending_confirmation_items"][0])

    def test_blocked_asset_record(self):
        m04 = self.by_id["M04"]
        self.assertEqual(m04["review_status"], "blocked")
        self.assertEqual(m04["draft_status"], "blocked")
        self.assertIsNone(m04["draft_id"])
        self.assertTrue(m04["risk_reasons"], "阻断必须给出风险原因")
        self.assertIn("R2", "".join(m04["risk_reasons"]) or "")

    def test_needs_info_asset_record(self):
        m03 = self.by_id["M03"]
        self.assertEqual(m03["review_status"], "needs_info")
        self.assertEqual(m03["draft_status"], "not_attempted")

    def test_failed_save_record(self):
        m05 = self.by_id["M05"]
        self.assertEqual(m05["draft_status"], "failed")
        self.assertIsNone(m05["draft_id"], "保存失败不得有 draft_id")

    def test_every_record_has_five_deliverables(self):
        required = {
            "review_status",
            "risk_reasons",
            "suggested_slot",
            "pending_confirmation_items",
            "draft_status",
        }
        for record in self.result.assets:
            with self.subTest(asset=record["asset_id"]):
                self.assertTrue(required.issubset(record))
                self.assertIs(record["able_to_publish"], False)

    def test_result_never_claims_publish(self):
        payload = json.dumps(self.result.to_dict(), ensure_ascii=False)
        self.assertNotIn("已发布", payload)
        self.assertNotIn("published", payload)

    def test_budget_is_reported(self):
        budget = self.result.budget
        self.assertGreater(budget["steps_used"], 0)
        self.assertLessEqual(budget["steps_used"], budget["steps_limit"])
        self.assertLessEqual(budget["tool_calls_used"], budget["tool_calls_limit"])

    def test_trace_contains_decision_and_tool_events(self):
        kinds = {e["event"] for e in self.events}
        self.assertIn("model_decision", kinds)
        self.assertIn("tool_result", kinds)
        self.assertIn("final", kinds)
        for event in self.events:
            self.assertEqual(event["run_id"], self.result.run_id)
            self.assertIn("step", event)

    def test_trace_records_reason_without_chain_of_thought(self):
        decisions = [e for e in self.events if e["event"] == "model_decision"]
        self.assertTrue(all("reason" in e for e in decisions))
        self.assertTrue(all(isinstance(e["reason"], str) for e in decisions))


class TestSingleAssetCompleted(RuntimeCase):
    def test_normal_case_status_completed(self):
        planner = ScriptedPlanner(
            [
                Action("list_pending_assets"),
                Action("get_asset_detail", {"asset_id": "M02"}),
                Action("check_rules", {"asset_id": "M02"}),
                Action("query_slots", {"channel": "douyin", "date": "2026-11-05"}),
                Action("save_draft", {"asset_id": "M02", "slot": "2026-11-05T20:00"}),
                Action("finish"),
            ]
        )
        result = self.make(planner).run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.assets), 1)
        self.assertEqual(result.assets[0]["draft_status"], "draft_saved")
        self.assertEqual(result.summary["failed"], 0)


class TestBudgetLimits(RuntimeCase):
    def test_global_step_limit_returns_incomplete(self):
        result = self.make(OfflineMockPlanner(), max_steps=5).run()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.limit_reason, LIMIT_GLOBAL_STEPS)
        self.assertTrue(result.remaining, "未完成必须列出剩余待办")

    def test_per_asset_limit_returns_incomplete(self):
        result = self.make(OfflineMockPlanner(), max_steps_per_asset=2).run()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.limit_reason, LIMIT_PER_ASSET)

    def test_tool_call_limit_returns_incomplete(self):
        result = self.make(OfflineMockPlanner(), max_tool_calls=2).run()
        self.assertEqual(result.status, "incomplete")
        self.assertIn(result.limit_reason, (LIMIT_TOOL_CALLS, LIMIT_GLOBAL_STEPS))
        self.assertLessEqual(result.budget["tool_calls_used"], 2)

    def test_incomplete_lists_unfinished_assets(self):
        result = self.make(OfflineMockPlanner(), max_steps=6).run()
        self.assertEqual(result.status, "incomplete")
        ids = {item["asset_id"] for item in result.remaining}
        self.assertTrue(ids, "remaining 不得为空")
        # 已完成的素材不应出现在 remaining
        done = {a["asset_id"] for a in result.assets}
        self.assertFalse(ids & done, f"remaining 与已完成重复：{ids & done}")

    def test_incomplete_keeps_completed_results(self):
        result = self.make(OfflineMockPlanner(), max_steps=6).run()
        self.assertTrue(result.assets, "未完成时也必须保留已成功的步骤结果")
        for record in result.assets:
            self.assertEqual(record["draft_status"], "draft_saved")


class TestSaveFailureIsNeverHidden(RuntimeCase):
    def test_non_storage_save_failure_is_recorded_as_failed(self):
        """锁冲突等失败也必须记为 failed，不能显示成"未处理"。"""
        original = ToolRegistry.call

        def fake_call(self, name, args=None):
            result = original(self, name, args)
            if name == "save_draft":
                return ToolResult.failure(
                    ErrorCode.E_STORAGE_LOCK, "草稿表被占用", True
                )
            return result

        planner = ScriptedPlanner(
            [
                Action("list_pending_assets"),
                Action("get_asset_detail", {"asset_id": "M02"}),
                Action("check_rules", {"asset_id": "M02"}),
                Action("query_slots", {"channel": "douyin", "date": "2026-11-05"}),
                Action("save_draft", {"asset_id": "M02", "slot": "2026-11-05T20:00"}),
                Action("finish_asset"),
            ]
        )
        with mock.patch.object(ToolRegistry, "call", fake_call):
            result = self.make(planner).run()

        self.assertEqual(len(result.assets), 1)
        self.assertEqual(result.assets[0]["draft_status"], "failed")
        self.assertIsNone(result.assets[0]["draft_id"])
        self.assertEqual(result.status, "partial")


class TestDegradation(RuntimeCase):
    def test_model_failure_falls_back_to_offline(self):
        result = self.make(FailingPlanner()).run()
        self.assertTrue(result.degraded)
        self.assertEqual(result.model, "offline")
        self.assertIn(result.status, ("partial", "completed"))
        self.assertEqual(len(result.assets), 7)

    def test_failure_after_degrade_is_fatal(self):
        with mock.patch.object(
            OfflineMockPlanner, "decide", side_effect=RuntimeError("mock broken")
        ):
            result = self.make(FailingPlanner()).run()
        self.assertEqual(result.status, "failed")
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error["code"], "E_UPSTREAM_MODEL")

    def test_degradation_is_traced(self):
        events = []
        self.make(FailingPlanner(), trace=events.append).run()
        self.assertTrue(any(e["event"] == "degraded" for e in events))


class TestGuardrailInRuntime(RuntimeCase):
    def test_rejected_action_is_not_fatal(self):
        planner = ScriptedPlanner(
            [
                Action("save_draft", {"asset_id": "M02", "slot": "2026-11-05T20:00"}),
                Action("list_pending_assets"),
                Action("finish"),
            ]
        )
        events = []
        result = self.make(planner, trace=events.append).run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.assets, [])
        rejected = [e for e in events if e["event"] == "action_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("INIT", rejected[0]["reason"])

    def test_blocked_asset_cannot_reach_save(self):
        # 即便出错也不会在 BLOCKED 后调用 save_draft
        events = []
        self.make(trace=events.append).run()
        rejected = [e for e in events if e["event"] == "action_rejected"]
        self.assertEqual(rejected, [], "正常链路不应出现被护栏拒绝的动作")


if __name__ == "__main__":
    unittest.main()
