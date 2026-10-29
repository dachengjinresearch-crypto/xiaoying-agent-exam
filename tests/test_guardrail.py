"""T8 验收：StateMachine 动作矩阵 + PolicyEngine 硬约束。

最关键的一条：``BLOCKED`` 状态下 ``save_draft`` 必须被拒绝。
这是"越权发布在结构上不可能"的直接证据，也是本文件的第一优先级。
"""

import unittest

from src.enums import AssetState, ErrorCode
from src.errors import ServiceError
from src.guardrail import PolicyEngine, StateMachine
from src.tools import ToolResult


def ok(data: dict | None = None) -> ToolResult:
    return ToolResult.success(data or {})


def fail(code: ErrorCode, retryable: bool = False) -> ToolResult:
    return ToolResult.failure(code, "failed", retryable)


class TestStateMatrix(unittest.TestCase):
    def setUp(self):
        self.sm = StateMachine()

    def test_initial_state_allows_only_listing(self):
        self.assertIs(self.sm.state, AssetState.INIT)
        self.assertTrue(self.sm.allow("list_pending_assets"))
        for action in (
            "get_asset_detail",
            "check_rules",
            "query_slots",
            "save_draft",
            "finish",
        ):
            with self.subTest(action=action):
                self.assertFalse(self.sm.allow(action))

    def test_listing_moves_to_asset_selected(self):
        self.sm.transition("list_pending_assets", ok())
        self.assertIs(self.sm.state, AssetState.ASSET_SELECTED)
        self.assertFalse(self.sm.allow("save_draft"))

    def test_happy_path_transitions(self):
        self.sm.transition("list_pending_assets", ok())
        self.sm.transition("get_asset_detail", ok({"complete": True}))
        self.assertIs(self.sm.state, AssetState.DETAIL_LOADED)
        self.sm.transition("check_rules", ok({"risk_level": "none"}))
        self.assertIs(self.sm.state, AssetState.RULES_READ)
        self.sm.transition("query_slots", ok({"available": ["19:00"]}))
        self.assertIs(self.sm.state, AssetState.SLOTS_READ)
        self.assertTrue(self.sm.allow("save_draft"))
        self.sm.transition("save_draft", ok({"draft_id": "D-001"}))
        self.assertIs(self.sm.state, AssetState.DRAFT_SAVED)
        self.assertTrue(self.sm.allow("next_asset"))

    def test_can_requery_slots_but_not_save_blind(self):
        self.sm.begin_asset()
        self.sm.transition("get_asset_detail", ok({"complete": True}))
        self.sm.transition("check_rules", ok({"risk_level": "none"}))
        self.sm.transition("query_slots", ok({}))
        self.assertTrue(self.sm.allow("query_slots"), "允许换日期重查")
        self.sm.transition("query_slots", ok({}))
        self.assertIs(self.sm.state, AssetState.SLOTS_READ)
        self.assertTrue(self.sm.allow("save_draft"))

    def test_complete_detail_skips_needs_info(self):
        self.sm.begin_asset()
        self.sm.transition("get_asset_detail", ok({"complete": True}))
        self.assertIs(self.sm.state, AssetState.DETAIL_LOADED)


class TestNeedsInfo(unittest.TestCase):
    def setUp(self):
        self.sm = StateMachine()
        self.sm.begin_asset()
        self.sm.transition("get_asset_detail", ok({"complete": False}))

    def test_incomplete_detail_enters_needs_info(self):
        self.assertIs(self.sm.state, AssetState.NEEDS_INFO)
        self.assertFalse(self.sm.allow("check_rules"))
        self.assertFalse(self.sm.allow("save_draft"))

    def test_one_supplementary_lookup_allowed(self):
        self.assertTrue(self.sm.allow("get_asset_detail"))
        self.sm.transition("get_asset_detail", ok({"complete": False}))
        self.assertIs(self.sm.state, AssetState.NEEDS_INFO)

    def test_second_supplementary_lookup_rejected(self):
        self.sm.transition("get_asset_detail", ok({"complete": False}))
        self.assertFalse(self.sm.allow("get_asset_detail"), "补查只允许一次")

    def test_can_finish_blocked(self):
        self.assertTrue(self.sm.allow("finish_asset"))


class TestBlockedState(unittest.TestCase):
    def setUp(self):
        self.sm = StateMachine()
        self.sm.begin_asset()
        self.sm.transition("get_asset_detail", ok({"complete": True}))
        self.sm.transition("check_rules", ok({"risk_level": "block"}))

    def test_blocked_by_rules(self):
        self.assertIs(self.sm.state, AssetState.BLOCKED)

    def test_save_draft_is_structurally_impossible(self):
        self.assertFalse(
            self.sm.allow("save_draft"), "BLOCKED 状态不得允许保存——这是防越权的核心"
        )
        self.assertFalse(self.sm.allow("query_slots"))
        self.assertFalse(self.sm.allow("get_asset_detail"))

    def test_can_move_to_next_asset(self):
        self.assertTrue(self.sm.allow("next_asset"))
        self.sm.transition("next_asset", ok())
        self.assertIs(self.sm.state, AssetState.ASSET_SELECTED)

    def test_rejection_reason_is_actionable(self):
        reason = self.sm.rejection_reason("save_draft")
        self.assertIn("BLOCKED", reason)
        self.assertIn("next_asset", reason)


class TestSaveFailure(unittest.TestCase):
    def setUp(self):
        self.sm = StateMachine()
        self.sm.begin_asset()
        self.sm.transition("get_asset_detail", ok({"complete": True}))
        self.sm.transition("check_rules", ok({"risk_level": "none"}))
        self.sm.transition("query_slots", ok({}))

    def test_storage_failure_enters_save_failed(self):
        self.sm.transition("save_draft", fail(ErrorCode.E_STORAGE_WRITE, True))
        self.assertIs(self.sm.state, AssetState.SAVE_FAILED)
        self.assertTrue(self.sm.allow("save_draft"), "应允许重试一次")

    def test_retry_is_capped_at_one(self):
        self.sm.transition("save_draft", fail(ErrorCode.E_STORAGE_WRITE, True))
        self.sm.transition("save_draft", fail(ErrorCode.E_STORAGE_WRITE, True))
        self.assertFalse(self.sm.allow("save_draft"), "重试次数必须封顶")
        self.assertTrue(self.sm.allow("finish_asset"))

    def test_integrity_failure_keeps_state_for_retry_with_new_args(self):
        # 硬性改期没标记待确认：模型应修正参数后重试，而不是被推进到失败态
        self.sm.transition("save_draft", fail(ErrorCode.E_INTEGRITY))
        self.assertIs(self.sm.state, AssetState.SLOTS_READ)
        self.assertTrue(self.sm.allow("save_draft"))

    def test_rule_blocked_on_save_moves_to_blocked(self):
        self.sm.transition("save_draft", fail(ErrorCode.E_RULE_BLOCKED))
        self.assertIs(self.sm.state, AssetState.BLOCKED)


class TestLimits(unittest.TestCase):
    def test_limit_exceeded_only_allows_finish(self):
        sm = StateMachine()
        sm.begin_asset()
        sm.mark_limit_exceeded()
        self.assertIs(sm.state, AssetState.LIMIT_EXCEEDED)
        for action in ("save_draft", "query_slots", "next_asset"):
            with self.subTest(action=action):
                self.assertFalse(sm.allow(action))
        self.assertTrue(sm.allow("finish"))

    def test_finish_is_terminal(self):
        sm = StateMachine()
        sm.begin_asset()
        sm.transition("finish", ok())
        self.assertTrue(sm.finished)
        for action in ("finish", "next_asset", "save_draft"):
            with self.subTest(action=action):
                self.assertFalse(sm.allow(action))

    def test_begin_asset_resets_counters(self):
        sm = StateMachine()
        sm.begin_asset()
        sm.transition("get_asset_detail", ok({"complete": False}))
        sm.transition("get_asset_detail", ok({"complete": False}))
        sm.transition("finish_asset", ok())
        self.assertIs(sm.state, AssetState.ASSET_SELECTED)
        # 新素材重新拥有完整的补查额度
        self.sm_reset_check(sm)

    def sm_reset_check(self, sm):
        sm.transition("get_asset_detail", ok({"complete": False}))
        self.assertTrue(sm.allow("get_asset_detail"))


class TestPolicyEngine(unittest.TestCase):
    HARD = {"asset_id": "M01", "requested_publish_at": "2026-11-05T18:00", "hard_requirement": True}
    SOFT = {"asset_id": "M07", "requested_publish_at": "2026-11-06T17:00", "hard_requirement": False}
    NO_RISK = {"risk_level": "none", "hits": []}
    BLOCKED = {"risk_level": "block", "hits": [{"rule_id": "R2"}]}

    def test_confirmation_required_truth_table(self):
        self.assertTrue(PolicyEngine.confirmation_required(self.HARD, "2026-11-05T19:00"))
        self.assertFalse(PolicyEngine.confirmation_required(self.HARD, "2026-11-05T18:00"))
        self.assertFalse(PolicyEngine.confirmation_required(self.SOFT, "2026-11-06T18:00"))

    def test_blocked_rules_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            PolicyEngine.assert_can_save(
                self.HARD, self.BLOCKED, "2026-11-05T18:00"
            )
        self.assertEqual(ctx.exception.code, ErrorCode.E_RULE_BLOCKED)

    def test_hard_change_without_confirmation_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            PolicyEngine.assert_can_save(self.HARD, self.NO_RISK, "2026-11-05T19:00")
        self.assertEqual(ctx.exception.code, ErrorCode.E_INTEGRITY)

    def test_confirmation_without_reason_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            PolicyEngine.assert_can_save(
                self.HARD,
                self.NO_RISK,
                "2026-11-05T19:00",
                pending_confirmation=True,
                reason="  ",
            )
        self.assertEqual(ctx.exception.code, ErrorCode.E_INTEGRITY)

    def test_confirmed_change_passes(self):
        PolicyEngine.assert_can_save(
            self.HARD,
            self.NO_RISK,
            "2026-11-05T19:00",
            pending_confirmation=True,
            reason="18:00 已被占用",
        )

    def test_soft_change_without_confirmation_passes(self):
        PolicyEngine.assert_can_save(self.SOFT, self.NO_RISK, "2026-11-06T18:00")

    def test_same_slot_passes(self):
        PolicyEngine.assert_can_save(self.HARD, self.NO_RISK, "2026-11-05T18:00")

    def test_publish_request_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            PolicyEngine.assert_can_save(
                self.HARD,
                self.NO_RISK,
                "2026-11-05T18:00",
                able_to_publish=True,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.E_INTEGRITY)


if __name__ == "__main__":
    unittest.main()
