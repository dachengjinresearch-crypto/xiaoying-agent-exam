"""决策层：规划器协议、离线 Mock 与动作校验。

**关于 ``OfflineMockPlanner`` 的定位（重要）**：

它是**规则驱动的占位决策器**，只用于在模型不可用时把整条链路跑通，
证明流程、护栏、幂等与失败上报是通的。

它 **不是 ReAct 决策证据**——题目明确"固定条件分支、自动重试、预设决策日志
不视为完成 ReAct 决策要求"。真正的两条决策轨迹必须由 ``LLMAdapter``（T11）产出。

不过它并不是"死脚本"：每一步都读取上一步的工具返回（``observation``）再做决定。
例如时段被占用时会读取 ``available`` 选替代时段，补查仍失败时才阻断。
这让离线运行产出的 trace 在结构上与在线一致，便于对比。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from .enums import AssetState
from .guardrail import CONTROL_ACTIONS
from .tools import ASSET_ID_RE, ToolRegistry

SLOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$")

CONTROL_ACTION_SET = frozenset(CONTROL_ACTIONS)
KNOWN_ACTIONS = frozenset(ToolRegistry.TOOL_NAMES) | CONTROL_ACTION_SET

# 每个工具动作的必填参数
REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "get_asset_detail": ("asset_id",),
    "check_rules": ("asset_id",),
    "query_slots": ("channel", "date"),
    "save_draft": ("asset_id", "slot"),
}


@dataclass(frozen=True)
class Action:
    """模型（或 Mock）产出的一步动作。"""

    action: str
    args: dict = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict:
        return {"action": self.action, "args": dict(self.args), "reason": self.reason}


class Planner(Protocol):
    def decide(self, observation: dict | None, state_view: dict) -> Action: ...


class ActionValidator:
    """结构校验。只判断"格式对不对"，不判断"业务上该不该"。"""

    @staticmethod
    def validate(action: Action) -> str | None:
        """返回 ``None`` 表示通过，否则返回可读的拒绝原因。"""
        if not isinstance(action, Action):
            return "决策结果必须是 Action"
        if not isinstance(action.action, str) or not action.action:
            return "action 必须是非空字符串"
        if action.action not in KNOWN_ACTIONS:
            return (
                f"未知动作 {action.action!r}，"
                f"可用动作：{', '.join(sorted(KNOWN_ACTIONS))}"
            )
        if not isinstance(action.args, dict):
            return "args 必须是对象"

        for key in REQUIRED_ARGS.get(action.action, ()):
            if key not in action.args:
                return f"动作 {action.action} 缺少参数 {key}"

        asset_id = action.args.get("asset_id")
        if asset_id is not None:
            if not isinstance(asset_id, str) or not ASSET_ID_RE.match(asset_id):
                return f"asset_id 非法：{asset_id!r}"

        slot = action.args.get("slot")
        if slot is not None:
            if not isinstance(slot, str) or not SLOT_RE.match(slot):
                return f"slot 格式非法，应形如 2026-11-05T19:00：{slot!r}"

        pending = action.args.get("pending_confirmation")
        if pending is not None and not isinstance(pending, bool):
            return "pending_confirmation 必须是布尔值"

        return None


class OfflineMockPlanner:
    """按 observation 决策的占位规划器（离线模式专用）。"""

    disclaimer = "offline mock：仅验证流程与护栏，不代表真实模型的决策质量"

    def __init__(self) -> None:
        self._remaining: list[str] = []
        self._loaded = False
        self._current: dict = {}
        self._detail_calls = 0
        self._retried = False
        self._last_save_args: dict | None = None

    # ------------------------------------------------------------------ #

    def decide(self, observation: dict | None, state_view: dict) -> Action:
        state = state_view.get("state")
        state_name = state.value if isinstance(state, AssetState) else str(state)
        handler = getattr(self, f"_at_{state_name.lower()}", None)
        if handler is None:
            return Action("finish", {}, f"状态 {state_name} 无对应策略，结束运行")
        return handler(observation or {})

    # -- 各状态 -------------------------------------------------------- #

    def _at_init(self, observation: dict) -> Action:
        return Action("list_pending_assets", {}, "先取出待处理素材")

    def _at_asset_selected(self, observation: dict) -> Action:
        if not self._loaded:
            items = (observation.get("data") or {}).get("assets") or []
            self._remaining = [item["asset_id"] for item in items]
            self._loaded = True

        if not self._remaining:
            return Action("finish", {}, "待处理素材已全部处理完毕")

        asset_id = self._remaining.pop(0)
        self._current = {}
        self._detail_calls = 1
        self._retried = False
        self._last_save_args = None
        return Action(
            "get_asset_detail",
            {"asset_id": asset_id},
            f"开始处理素材 {asset_id}，先读取元数据",
        )

    def _at_detail_loaded(self, observation: dict) -> Action:
        self._current = observation.get("data") or self._current
        asset_id = self._current.get("asset_id", "")
        return Action(
            "check_rules",
            {"asset_id": asset_id},
            f"素材 {asset_id} 信息完整，检查发布规则",
        )

    def _at_needs_info(self, observation: dict) -> Action:
        data = observation.get("data") or {}
        if data:
            self._current = data
        asset_id = self._current.get("asset_id", "")

        if self._detail_calls < 2:
            self._detail_calls += 1
            return Action(
                "get_asset_detail",
                {"asset_id": asset_id},
                f"素材 {asset_id} 字段缺失，补查一次补充材料",
            )
        return Action(
            "finish_asset",
            {},
            f"素材 {asset_id} 补查后仍缺字段，阻断并跳过",
        )

    def _at_rules_read(self, observation: dict) -> Action:
        if observation.get("ok") is False:
            error = observation.get("error") or {}
            if error.get("retryable") and not self._retried:
                self._retried = True
                return self._query_slots("查询时段失败（可重试），重试一次")
            return Action(
                "finish_asset", {}, f"查询时段失败：{error.get('code')}"
            )
        return self._query_slots("规则无阻断，查询时段占用情况")

    def _query_slots(self, reason: str) -> Action:
        date, _ = self._current["requested_publish_at"].split("T")
        return Action(
            "query_slots",
            {"channel": self._current["channel"], "date": date},
            reason,
        )

    def _at_slots_read(self, observation: dict) -> Action:
        # 保存失败：不能把它误读成"没有可用时段"，否则素材会被静默丢掉
        if observation.get("ok") is False:
            error = observation.get("error") or {}
            if (
                error.get("retryable")
                and not self._retried
                and self._last_save_args is not None
            ):
                self._retried = True
                return Action(
                    "save_draft",
                    dict(self._last_save_args),
                    f"保存失败（{error.get('code')}，可重试），重试一次",
                )
            return Action(
                "finish_asset", {}, f"保存失败：{error.get('code')}"
            )

        data = observation.get("data") or {}
        available = data.get("available") or []
        asset_id = self._current.get("asset_id", "")
        requested = self._current.get("requested_publish_at", "")

        date, clock = requested.split("T") if "T" in requested else ("", "")

        if clock and clock in available:
            args = {"asset_id": asset_id, "slot": requested}
            self._last_save_args = args
            return Action(
                "save_draft",
                args,
                f"{requested} 可用，按原定时间保存草稿",
            )

        if available:
            slot = f"{data.get('date') or date}T{available[0]}"
            reason = f"{requested} 已被占用，建议调整为 {slot}"
            args = {
                "asset_id": asset_id,
                "slot": slot,
                "pending_confirmation": True,
                "reason": reason,
            }
            self._last_save_args = args
            return Action("save_draft", args, reason)

        return Action(
            "finish_asset", {}, f"{date} 已无可用时段，放弃素材 {asset_id}"
        )

    def _at_save_failed(self, observation: dict) -> Action:
        asset_id = self._current.get("asset_id", "")
        error = observation.get("error") or {}
        # 可重试错误只再试一次：题目明确"自动重试不算决策"，
        # 这里的重试是由工具返回的 retryable 字段驱动的，且有明确上限。
        if error.get("retryable") and not self._retried and self._last_save_args:
            self._retried = True
            return Action(
                "save_draft",
                dict(self._last_save_args),
                f"保存失败（{error.get('code')}，可重试），重试一次",
            )
        return Action(
            "finish_asset",
            {},
            f"素材 {asset_id} 保存失败：{error.get('code')}，标记失败并继续",
        )

    def _at_draft_saved(self, observation: dict) -> Action:
        if self._remaining:
            return Action("next_asset", {}, "当前素材草稿已保存，继续下一个")
        return Action("finish", {}, "全部素材处理完毕")

    def _at_blocked(self, observation: dict) -> Action:
        if self._remaining:
            return Action("next_asset", {}, "素材被规则阻断，跳过并继续")
        return Action("finish", {}, "剩余素材均已阻断，结束运行")

    def _at_limit_exceeded(self, observation: dict) -> Action:
        return Action("finish", {}, "已达执行上限，返回未完成状态")
