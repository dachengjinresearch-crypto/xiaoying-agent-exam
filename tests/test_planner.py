"""T9 验收：ActionValidator 与 OfflineMockPlanner。

注意：本文件**不**把 Mock 的决策当作 ReAct 证据。
这里只验证"离线链路能跑通、并且每一步都真的读了上一步的工具返回"。
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from src.config import DATA_FILES
from src.enums import AssetState
from src.guardrail import CONTROL_ACTIONS, StateMachine
from src.planner import Action, ActionValidator, OfflineMockPlanner
from src.repository import Repository
from src.tools import ToolRegistry, ToolResult

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONTROL = frozenset(CONTROL_ACTIONS)

DETAIL_M01 = {
    "asset_id": "M01",
    "channel": "douyin",
    "requested_publish_at": "2026-11-05T18:00",
    "hard_requirement": True,
    "complete": True,
}
SLOTS_CONFLICT = {
    "channel": "douyin",
    "date": "2026-11-05",
    "occupied": [{"slot": "18:00", "asset_id": "M00"}],
    "available": ["19:00", "20:00"],
}
LIST_TWO = {
    "data": {"count": 2, "assets": [{"asset_id": "M01"}, {"asset_id": "M02"}]}
}


class TestActionValidator(unittest.TestCase):
    def test_accepts_valid_tool_actions(self):
        cases = [
            Action("list_pending_assets"),
            Action("get_asset_detail", {"asset_id": "M01"}),
            Action("check_rules", {"asset_id": "M01"}),
            Action("query_slots", {"channel": "douyin", "date": "2026-11-05"}),
            Action("save_draft", {"asset_id": "M01", "slot": "2026-11-05T19:00"}),
            Action("finish"),
            Action("next_asset"),
            Action("finish_asset"),
        ]
        for action in cases:
            with self.subTest(action=action.action):
                self.assertIsNone(ActionValidator.validate(action))

    def test_rejects_unknown_action(self):
        self.assertIsNotNone(ActionValidator.validate(Action("publish_now")))

    def test_rejects_missing_required_args(self):
        self.assertIsNotNone(ActionValidator.validate(Action("check_rules")))
        self.assertIsNotNone(
            ActionValidator.validate(Action("query_slots", {"channel": "douyin"}))
        )
        self.assertIsNotNone(
            ActionValidator.validate(Action("save_draft", {"asset_id": "M01"}))
        )

    def test_rejects_bad_asset_id(self):
        for bad in ("../../x", "m01", "M1", "M001", 123):
            with self.subTest(asset_id=bad):
                self.assertIsNotNone(
                    ActionValidator.validate(
                        Action("check_rules", {"asset_id": bad})
                    )
                )

    def test_rejects_bad_slot(self):
        for bad in ("2026-11-05 19:00", "19:00", "2026-11-05T25:00"):
            with self.subTest(slot=bad):
                self.assertIsNotNone(
                    ActionValidator.validate(
                        Action("save_draft", {"asset_id": "M01", "slot": bad})
                    )
                )

    def test_rejects_non_boolean_confirmation(self):
        self.assertIsNotNone(
            ActionValidator.validate(
                Action(
                    "save_draft",
                    {"asset_id": "M01", "slot": "2026-11-05T19:00",
                     "pending_confirmation": "false"},
                )
            )
        )

    def test_rejects_non_dict_args(self):
        self.assertIsNotNone(
            ActionValidator.validate(Action("check_rules", ["M01"]))
        )


class TestMockDecisionBranches(unittest.TestCase):
    def _chain_to_slots(self, planner):
        """把规划器推进到 SLOTS_READ 前一步，返回产生的动作列表。"""
        actions = [
            planner.decide(None, {"state": AssetState.INIT}),
            planner.decide(LIST_TWO, {"state": AssetState.ASSET_SELECTED}),
            planner.decide({"data": DETAIL_M01}, {"state": AssetState.DETAIL_LOADED}),
            planner.decide({"data": {"risk_level": "none"}}, {"state": AssetState.RULES_READ}),
        ]
        return actions

    def test_init_lists_pending_assets(self):
        action = OfflineMockPlanner().decide(None, {"state": AssetState.INIT})
        self.assertEqual(action.action, "list_pending_assets")

    def test_selects_first_asset_from_observation(self):
        planner = OfflineMockPlanner()
        action = planner.decide(LIST_TWO, {"state": AssetState.ASSET_SELECTED})
        self.assertEqual(action.action, "get_asset_detail")
        self.assertEqual(action.args["asset_id"], "M01")

    def test_queries_slots_for_requested_date(self):
        planner = OfflineMockPlanner()
        actions = self._chain_to_slots(planner)
        query = actions[-1]
        self.assertEqual(query.action, "query_slots")
        self.assertEqual(query.args["date"], "2026-11-05")
        self.assertEqual(query.args["channel"], "douyin")

    def test_conflict_picks_alternative_and_flags_confirmation(self):
        planner = OfflineMockPlanner()
        self._chain_to_slots(planner)
        action = planner.decide(
            {"data": SLOTS_CONFLICT}, {"state": AssetState.SLOTS_READ}
        )
        self.assertEqual(action.action, "save_draft")
        self.assertEqual(action.args["slot"], "2026-11-05T19:00")
        self.assertTrue(action.args["pending_confirmation"])
        self.assertIn("18:00", action.args["reason"])
        # 原始要求不得被改写
        self.assertNotIn("requested_publish_at", action.args)

    def test_free_requested_slot_saves_without_confirmation(self):
        planner = OfflineMockPlanner()
        self._chain_to_slots(planner)
        free_slots = dict(SLOTS_CONFLICT, available=["18:00", "19:00"])
        action = planner.decide({"data": free_slots}, {"state": AssetState.SLOTS_READ})
        self.assertEqual(action.args["slot"], "2026-11-05T18:00")
        self.assertNotIn("pending_confirmation", action.args)

    def test_no_available_slot_gives_up_asset(self):
        planner = OfflineMockPlanner()
        self._chain_to_slots(planner)
        action = planner.decide(
            {"data": {"date": "2026-11-05", "available": [], "occupied": []}},
            {"state": AssetState.SLOTS_READ},
        )
        self.assertEqual(action.action, "finish_asset")

    def test_supplementary_lookup_then_block(self):
        planner = OfflineMockPlanner()
        planner.decide(LIST_TWO, {"state": AssetState.ASSET_SELECTED})
        incomplete = {"data": {"asset_id": "M03", "complete": False}}
        first = planner.decide(incomplete, {"state": AssetState.NEEDS_INFO})
        second = planner.decide(incomplete, {"state": AssetState.NEEDS_INFO})
        self.assertEqual(first.action, "get_asset_detail", "第一次缺失应补查")
        self.assertEqual(second.action, "finish_asset", "补查仍缺失则阻断")

    def test_save_failure_leads_to_finish_asset(self):
        planner = OfflineMockPlanner()
        self._chain_to_slots(planner)
        action = planner.decide(
            {"ok": False, "error": {"code": "E_STORAGE_WRITE", "retryable": False}},
            {"state": AssetState.SAVE_FAILED},
        )
        self.assertEqual(action.action, "finish_asset")

    def test_retryable_error_is_retried_exactly_once(self):
        planner = OfflineMockPlanner()
        self._chain_to_slots(planner)
        planner.decide({"data": SLOTS_CONFLICT}, {"state": AssetState.SLOTS_READ})

        retry = planner.decide(
            {"ok": False, "error": {"code": "E_STORAGE_WRITE", "retryable": True}},
            {"state": AssetState.SAVE_FAILED},
        )
        self.assertEqual(retry.action, "save_draft")
        self.assertEqual(retry.args["slot"], "2026-11-05T19:00", "重试必须沿用原参数")

        give_up = planner.decide(
            {"ok": False, "error": {"code": "E_STORAGE_WRITE", "retryable": True}},
            {"state": AssetState.SAVE_FAILED},
        )
        self.assertEqual(give_up.action, "finish_asset", "重试只能一次")

    def test_save_error_is_not_misread_as_no_slots(self):
        """回归：把错误返回当成"没有可用时段"会静默丢掉素材。"""
        planner = OfflineMockPlanner()
        self._chain_to_slots(planner)
        planner.decide({"data": SLOTS_CONFLICT}, {"state": AssetState.SLOTS_READ})
        action = planner.decide(
            {"ok": False, "error": {"code": "E_STORAGE_LOCK", "retryable": False}},
            {"state": AssetState.SLOTS_READ},
        )
        self.assertEqual(action.action, "finish_asset")
        self.assertIn("保存失败", action.reason)
        self.assertNotIn("无可用时段", action.reason)

    def test_advances_when_queue_not_empty(self):
        planner = OfflineMockPlanner()
        self._chain_to_slots(planner)
        action = planner.decide({}, {"state": AssetState.DRAFT_SAVED})
        self.assertEqual(action.action, "next_asset")

    def test_finishes_when_queue_empty(self):
        planner = OfflineMockPlanner()
        planner.decide(
            {"data": {"assets": [{"asset_id": "M01"}]}},
            {"state": AssetState.ASSET_SELECTED},
        )
        action = planner.decide({}, {"state": AssetState.DRAFT_SAVED})
        self.assertEqual(action.action, "finish")

    def test_blocked_skips_or_finishes(self):
        planner = OfflineMockPlanner()
        planner.decide(LIST_TWO, {"state": AssetState.ASSET_SELECTED})
        self.assertEqual(
            planner.decide({}, {"state": AssetState.BLOCKED}).action, "next_asset"
        )
        planner2 = OfflineMockPlanner()
        planner2.decide(
            {"data": {"assets": [{"asset_id": "M04"}]}},
            {"state": AssetState.ASSET_SELECTED},
        )
        self.assertEqual(
            planner2.decide({}, {"state": AssetState.BLOCKED}).action, "finish"
        )

    def test_limit_exceeded_finishes(self):
        planner = OfflineMockPlanner()
        action = planner.decide({}, {"state": AssetState.LIMIT_EXCEEDED})
        self.assertEqual(action.action, "finish")


class TestOfflineEndToEnd(unittest.TestCase):
    """用最小驱动循环验证离线链路可完整跑通（完整 Runtime 在 T10）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name in DATA_FILES:
            shutil.copy(DATA_DIR / f"{name}.json", self.root / f"{name}.json")
        self.registry = ToolRegistry(Repository(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def _drive(self, max_steps: int = 80):
        planner = OfflineMockPlanner()
        machine = StateMachine()
        observation = None
        rejections: list[str] = []

        for _ in range(max_steps):
            if machine.finished:
                break
            action = planner.decide(observation, {"state": machine.state})

            problem = ActionValidator.validate(action)
            if problem:
                rejections.append(problem)
                observation = {"ok": False, "error": {"message": problem}}
                continue

            if not machine.allow(action.action):
                rejections.append(f"rejected:{action.action}")
                observation = {
                    "ok": False,
                    "error": {"message": machine.rejection_reason(action.action)},
                }
                continue

            if action.action in CONTROL:
                result = ToolResult.success({})
            else:
                result = self.registry.call(action.action, action.args)

            observation = result.to_dict()
            machine.transition(action.action, result)

        return machine, rejections

    def test_full_offline_run_completes(self):
        machine, rejections = self._drive()
        self.assertTrue(machine.finished, "离线链路应能自然结束")
        self.assertEqual(rejections, [], "不应出现被护栏拒绝的动作")

    def test_offline_run_saves_expected_drafts(self):
        self._drive()
        drafts = {d["asset_id"]: d for d in self.registry.drafts.all_drafts()}
        # M03 缺信息、M04 规则阻断、M05 保存失败 → 不产生草稿
        self.assertEqual(sorted(drafts), ["M01", "M02", "M06", "M07"])

    def test_conflict_asset_is_rescheduled_with_confirmation(self):
        self._drive()
        m01 = self.registry.drafts.get("M01")
        self.assertEqual(m01["slot"], "2026-11-05T19:00")
        self.assertTrue(m01["pending_confirmation"])
        self.assertEqual(m01["origin_requested_at"], "2026-11-05T18:00")
        self.assertFalse(m01["able_to_publish"])

    def test_blocked_and_failed_assets_have_no_draft(self):
        self._drive()
        for asset_id in ("M03", "M04", "M05"):
            with self.subTest(asset_id=asset_id):
                self.assertIsNone(self.registry.drafts.get(asset_id))

    def test_offline_run_is_deterministic(self):
        first = self._drive()[0]
        # 同一份数据重复跑，结果一致（Mock 无随机性）
        drafts_first = [(d["asset_id"], d["slot"]) for d in self.registry.drafts.all_drafts()]
        second = self._drive()[0]
        drafts_second = [(d["asset_id"], d["slot"]) for d in self.registry.drafts.all_drafts()]
        self.assertTrue(first.finished and second.finished)
        self.assertEqual(drafts_first, drafts_second, "重复提交不得新增草稿")


if __name__ == "__main__":
    unittest.main()
