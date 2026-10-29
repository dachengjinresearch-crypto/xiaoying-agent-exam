"""护栏层：动作合法性矩阵 + 硬约束断言。

这是"模型只有提议权"的落点（docs/02-solution.md §4.1）：

- 模型产出的动作，先过 ``StateMachine.allow``；不在当前状态的允许集合里，
  **根本不执行**，而是把 ``action_rejected`` 作为观察结果回给模型。
- ``BLOCKED`` 状态的允许集合里没有 ``save_draft``。因此即使模型被素材内容
  诱导去保存，也执行不了——**越权在结构上不可能，而不是"规则不允许"**。
- ``PolicyEngine`` 在真正写盘前再断言一次硬约束：硬性发布时间被调整却没标记
  待确认、规则阻断、请求发布，一律拒绝。

注意：``action_rejected`` 只是护栏生效的证据，**不能**充当 ReAct 决策证据。
两条决策轨迹必须来自业务结果差异（时段冲突改期、规则阻断终止）。
"""

from __future__ import annotations

from .enums import AssetState, ErrorCode
from .errors import ServiceError
from .tools import ToolResult

# 控制类动作（非工具调用）
CONTROL_ACTIONS = ("next_asset", "finish_asset", "finish")

# 当前状态 → 允许的动作集合
ALLOWED: dict[AssetState, frozenset[str]] = {
    AssetState.INIT: frozenset({"list_pending_assets"}),
    AssetState.ASSET_SELECTED: frozenset({"get_asset_detail", "finish"}),
    AssetState.DETAIL_LOADED: frozenset({"check_rules"}),
    AssetState.NEEDS_INFO: frozenset({"get_asset_detail", "finish_asset"}),
    AssetState.RULES_READ: frozenset({"query_slots", "finish_asset"}),
    AssetState.BLOCKED: frozenset({"next_asset", "finish"}),
    AssetState.SLOTS_READ: frozenset({"save_draft", "query_slots", "finish_asset"}),
    AssetState.DRAFT_SAVED: frozenset({"next_asset", "finish"}),
    AssetState.SAVE_FAILED: frozenset({"save_draft", "finish_asset", "finish"}),
    AssetState.LIMIT_EXCEEDED: frozenset({"finish"}),
}

# 每个素材最多调用 2 次 get_asset_detail：首次 + 1 次补查
MAX_DETAIL_CALLS = 2
# 保存失败后的重试次数上限（题目要求"自动重试不算决策"，这里只给 1 次）
MAX_SAVE_RETRIES = 1


class StateMachine:
    """单个素材的状态机。素材之间通过 ``begin_asset`` 复位。"""

    def __init__(self) -> None:
        self.state: AssetState = AssetState.INIT
        self.finished = False
        self.asset_closed = False
        self._detail_calls = 0
        self._save_retries = 0

    # -- 判定 ---------------------------------------------------------- #

    def allow(self, action: str) -> bool:
        if self.finished:
            return False
        if action not in ALLOWED.get(self.state, frozenset()):
            return False
        if action == "get_asset_detail" and self.state is AssetState.NEEDS_INFO:
            return self._detail_calls < MAX_DETAIL_CALLS
        if action == "save_draft" and self.state is AssetState.SAVE_FAILED:
            return self._save_retries <= MAX_SAVE_RETRIES
        return True

    def rejection_reason(self, action: str) -> str:
        """给模型的可读拒绝原因，便于它调整下一步。"""
        if self.finished:
            return "本次运行已结束，不再接受动作"
        allowed = sorted(ALLOWED.get(self.state, frozenset()))
        if not allowed:
            return f"状态 {self.state.value} 不允许任何动作"
        return (
            f"状态 {self.state.value} 下不允许动作 {action}，"
            f"可选动作：{', '.join(allowed)}"
        )

    # -- 迁移 ---------------------------------------------------------- #

    def begin_asset(self) -> AssetState:
        """开始处理一个新素材（含阻断后跳到下一个）。"""
        self.state = AssetState.ASSET_SELECTED
        self.asset_closed = False
        self._detail_calls = 0
        self._save_retries = 0
        return self.state

    def mark_limit_exceeded(self) -> AssetState:
        self.state = AssetState.LIMIT_EXCEEDED
        return self.state

    def transition(self, action: str, result: ToolResult) -> AssetState:
        """只有成功执行（或产生可识别失败）才迁移状态。

        被护栏拒绝的动作不会走到这里；执行失败但需要模型换参数的情形
        （如硬性改期没标记待确认）保持原状态，允许模型修正参数后重试。
        """
        if action == "finish":
            self.finished = True
            return self.state

        if action == "next_asset":
            return self.begin_asset()

        if action == "finish_asset":
            self.asset_closed = True
            return self.begin_asset()

        if action == "list_pending_assets":
            if result.ok:
                self.state = AssetState.ASSET_SELECTED
            return self.state

        if action == "get_asset_detail":
            self._detail_calls += 1
            if result.ok:
                complete = bool((result.data or {}).get("complete"))
                self.state = (
                    AssetState.DETAIL_LOADED if complete else AssetState.NEEDS_INFO
                )
            return self.state

        if action == "check_rules":
            if result.ok:
                blocked = (result.data or {}).get("risk_level") == "block"
                self.state = AssetState.BLOCKED if blocked else AssetState.RULES_READ
            return self.state

        if action == "query_slots":
            if result.ok:
                self.state = AssetState.SLOTS_READ
            return self.state

        if action == "save_draft":
            if result.ok:
                self.state = AssetState.DRAFT_SAVED
                return self.state
            code = (result.error or {}).get("code")
            if code == ErrorCode.E_STORAGE_WRITE.value:
                self._save_retries += 1
                self.state = AssetState.SAVE_FAILED
            elif code == ErrorCode.E_RULE_BLOCKED.value:
                self.state = AssetState.BLOCKED
            # 其它失败（如缺待确认）保持 SLOTS_READ，让模型修正后重试
            return self.state

        return self.state


class PolicyEngine:
    """硬约束断言。放在写盘前最后一道，不采信模型的任何参数覆盖。"""

    @staticmethod
    def confirmation_required(asset: dict, slot: str) -> bool:
        """改期是否必须标记待确认：硬性要求被改动时必须。"""
        return slot != asset.get("requested_publish_at") and bool(
            asset.get("hard_requirement")
        )

    @classmethod
    def assert_can_save(
        cls,
        asset: dict,
        rule_result: dict | None,
        slot: str,
        *,
        pending_confirmation: bool = False,
        reason: str | None = None,
        able_to_publish: bool = False,
    ) -> None:
        if able_to_publish:
            raise ServiceError(
                ErrorCode.E_INTEGRITY,
                "系统只能保存草稿，不允许标记为已发布",
                retryable=False,
            )

        if rule_result is not None and rule_result.get("risk_level") == "block":
            raise ServiceError(
                ErrorCode.E_RULE_BLOCKED,
                f"素材 {asset.get('asset_id')} 命中阻断规则，不能排期",
                retryable=False,
            )

        if cls.confirmation_required(asset, slot) and not pending_confirmation:
            raise ServiceError(
                ErrorCode.E_INTEGRITY,
                (
                    f"硬性发布时间 {asset.get('requested_publish_at')} 被调整为 {slot}，"
                    "必须标记 pending_confirmation 并说明原因"
                ),
                retryable=False,
            )

        if pending_confirmation and not (reason and reason.strip()):
            raise ServiceError(
                ErrorCode.E_INTEGRITY,
                "标记待确认时必须给出确认原因",
                retryable=False,
            )
