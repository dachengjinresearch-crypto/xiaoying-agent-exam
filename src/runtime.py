"""运行时：把决策、护栏、工具、观测串成闭环。

这是题目要求的"模型选择动作 → 执行工具 → 观察结果 → 调整下一步或结束"的落点。

四项约束：

1. **预算**：全局步数、单素材步数、工具调用数、墙钟时间四重上限。
   任一触达都返回**结构化未完成**（含已完成清单与剩余待办），不抛异常、不静默截断。
2. **降级**：模型不可用时切离线 Mock，并标记 ``degraded=true``。
   降级只影响"用哪个规划器"，不影响"保存成功了没有"。
3. **护栏先行**：动作先过 ``ActionValidator``（格式），再过 ``StateMachine``（合法性），
   都通过才执行。被拒时把可读原因回给模型，让它调整。
4. **逐素材结算**：输出必须包含题目要求的五项交付物，字段见 docs/02-solution.md §4.10。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .config import (
    MAX_STEPS,
    MAX_STEPS_PER_ASSET,
    MAX_TOOL_CALLS,
    TOTAL_WALL_S,
)
from .enums import AssetState, ErrorCode, ReviewStatus, RunStatus
from .guardrail import ALLOWED, CONTROL_ACTIONS, StateMachine
from .planner import Action, ActionValidator, OfflineMockPlanner, Planner
from .tools import ToolRegistry, ToolResult

CONTROL = frozenset(CONTROL_ACTIONS)

TRACE_SINK = Callable[[dict], None]

LIMIT_GLOBAL_STEPS = "max_steps_reached"
LIMIT_PER_ASSET = "max_steps_per_asset_reached"
LIMIT_TOOL_CALLS = "max_tool_calls_reached"
LIMIT_WALL_CLOCK = "wall_clock_exceeded"


@dataclass
class RunResult:
    run_id: str
    status: str
    model: str
    degraded: bool
    assets: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)
    remaining: list[dict] = field(default_factory=list)
    limit_reason: str | None = None
    error: dict | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "model": self.model,
            "degraded": self.degraded,
            "assets": self.assets,
            "summary": self.summary,
            "budget": self.budget,
            "remaining": self.remaining,
            "limit_reason": self.limit_reason,
            "error": self.error,
        }


class AgentRuntime:
    def __init__(
        self,
        registry: ToolRegistry,
        planner: Planner,
        *,
        trace: TRACE_SINK | None = None,
        run_id: str | None = None,
        max_steps: int = MAX_STEPS,
        max_steps_per_asset: int = MAX_STEPS_PER_ASSET,
        max_tool_calls: int = MAX_TOOL_CALLS,
        total_wall_s: float = TOTAL_WALL_S,
    ) -> None:
        self.registry = registry
        self.planner = planner
        self.trace = trace
        self.run_id = run_id or f"run-{int(time.time())}"
        self.max_steps = max_steps
        self.max_steps_per_asset = max_steps_per_asset
        self.max_tool_calls = max_tool_calls
        self.total_wall_s = total_wall_s

        self.machine = StateMachine()
        self.degraded = False
        self.model = getattr(planner, "kind", "offline")
        self.steps = 0
        self.tool_calls = 0
        self._asset_steps = 0
        self._pending: list[str] = []
        self._records: dict[str, dict] = {}
        self._ctx: dict = {}
        self._current_id: str | None = None
        self._closed: list[str] = []

    # ------------------------------------------------------------------ #
    # 对外
    # ------------------------------------------------------------------ #

    def run(self) -> RunResult:
        observation: dict | None = None
        started = time.monotonic()
        limit_reason: str | None = None
        fatal: dict | None = None

        while not self.machine.finished:
            if self.machine.state is AssetState.ASSET_SELECTED:
                self._asset_steps = 0

            limit_reason = self._check_budget(started)
            if limit_reason:
                self.machine.mark_limit_exceeded()
                self._emit("limit", reason=limit_reason, state=self.machine.state.value)
                break

            try:
                action = self.planner.decide(
                    observation,
                    {
                        "state": self.machine.state,
                        "asset_id": self._current_id,
                        # 允许动作集合是公开信息，模型据此提出合法动作；
                        # 真正的裁决仍在 StateMachine，告诉模型只是减少无效尝试。
                        "allowed": sorted(ALLOWED.get(self.machine.state, frozenset())),
                    },
                )
            except Exception as exc:  # noqa: BLE001 - 模型调用失败需降级
                fallback = self._degrade(exc)
                if fallback is None:
                    fatal = {
                        "code": ErrorCode.E_UPSTREAM_MODEL.value,
                        "message": f"规划器不可用且降级失败：{type(exc).__name__}: {exc}",
                    }
                    break
                continue

            self.steps += 1
            self._asset_steps += 1
            state_before = self.machine.state
            self._emit(
                "model_decision",
                action=action.action,
                args=dict(action.args),
                reason=action.reason,
                state_before=state_before.value,
                # 决策当下 _current_id 可能尚未设置（如首次读素材），
                # 用参数里的 asset_id 兜底，保证每行都能自证指向哪个素材
                asset_id=action.args.get("asset_id") or self._current_id,
            )

            # 闸门一：结构校验
            problem = ActionValidator.validate(action)
            if problem:
                self._emit(
                    "action_rejected",
                    action=action.action,
                    reason=problem,
                    state_before=state_before.value,
                    state_after=state_before.value,
                )
                observation = self._rejected(problem, ErrorCode.E_SCHEMA_INVALID.value)
                continue

            # 闸门二：状态合法性
            if not self.machine.allow(action.action):
                reason = self.machine.rejection_reason(action.action)
                self._emit(
                    "action_rejected",
                    action=action.action,
                    reason=reason,
                    state_before=state_before.value,
                    state_after=state_before.value,
                )
                observation = self._rejected(reason, "E_ACTION_REJECTED")
                continue

            asset_before = self._current_id
            result = self._execute(action)
            self._observe(action, result)
            observation = result.to_dict()
            self.machine.transition(action.action, result)
            self._emit(
                "state_transition",
                action=action.action,
                asset_id=asset_before,
                state_before=state_before.value,
                state_after=self.machine.state.value,
            )

        return self._result(limit_reason, fatal)

    # ------------------------------------------------------------------ #
    # 执行与观测
    # ------------------------------------------------------------------ #

    def _execute(self, action: Action) -> ToolResult:
        if action.action in CONTROL:
            return self._handle_control(action)
        self.tool_calls += 1
        result = self.registry.call(action.action, action.args)
        self._emit(
            "tool_result",
            action=action.action,
            args=dict(action.args),
            ok=result.ok,
            summary=self._summarize(action.action, result),
            error=(result.error or {}).get("code"),
            asset_id=action.args.get("asset_id") or self._current_id,
            next_action=self._guess_next(result),
        )
        return result

    def _handle_control(self, action: Action) -> ToolResult:
        if action.action in ("next_asset", "finish_asset"):
            self._finalize_current()
        if action.action == "finish":
            self._finalize_current()
        return ToolResult.success({})

    def _observe(self, action: Action, result: ToolResult) -> None:
        data = result.data or {}
        if action.action == "list_pending_assets" and result.ok:
            self._pending = [item["asset_id"] for item in data.get("assets", [])]
            return

        if action.action == "get_asset_detail" and result.ok:
            self._current_id = data.get("asset_id")
            self._ctx = {
                "detail": data,
                "missing_fields": list(data.get("missing_fields") or []),
                "risk_level": None,
                "hits": [],
                "slots": None,
                "draft": None,
                "save_failed": False,
            }
            return

        if action.action == "check_rules" and result.ok:
            self._ctx["risk_level"] = data.get("risk_level")
            self._ctx["hits"] = list(data.get("hits") or [])
            return

        if action.action == "query_slots" and result.ok:
            self._ctx["slots"] = data
            return

        if action.action == "save_draft":
            if result.ok:
                self._ctx["draft"] = data
            else:
                # 任何保存失败都要留下痕迹：只用 E_STORAGE_WRITE 判定，
                # 会让锁冲突之类的失败显示成"未处理"，等于把失败说成没发生。
                self._ctx["save_failed"] = True
                self._ctx["save_error"] = (result.error or {}).get("code")

    def _finalize_current(self) -> None:
        asset_id = self._current_id
        if not asset_id or asset_id in self._records:
            self._current_id = None
            return

        ctx = self._ctx or {}
        detail = ctx.get("detail") or {}
        draft = ctx.get("draft")
        blocked = ctx.get("risk_level") == "block"
        incomplete_info = not detail.get("complete", True)

        if blocked:
            review_status = ReviewStatus.BLOCKED.value
            draft_status = "blocked"
        elif incomplete_info:
            review_status = ReviewStatus.NEEDS_INFO.value
            draft_status = "not_attempted"
        else:
            review_status = ReviewStatus.PASSED.value
            draft_status = "not_attempted"

        if draft:
            draft_status = "draft_saved"
        elif ctx.get("save_failed"):
            draft_status = "failed"

        pending_items = []
        if draft and draft.get("pending_confirmation"):
            pending_items.append(
                draft.get("confirmation_reason")
                or f"发布时间由 {draft.get('origin_requested_at')} 调整为 {draft.get('slot')}，需用户确认"
            )

        record = {
            "asset_id": asset_id,
            "review_status": review_status,
            "risk_reasons": [
                f"{hit.get('rule_id', '?')}: {hit.get('reason', '')}"
                for hit in ctx.get("hits", [])
            ],
            "requested_publish_at": draft.get("origin_requested_at")
            if draft
            else detail.get("requested_publish_at"),
            "suggested_slot": draft.get("slot") if draft else None,
            "slot_change_requires_confirmation": bool(
                draft and draft.get("pending_confirmation")
            ),
            "draft_status": draft_status,
            "draft_id": draft.get("draft_id") if draft else None,
            "pending_confirmation_items": pending_items,
            "able_to_publish": False,
        }
        self._records[asset_id] = record
        self._closed.append(asset_id)
        self._current_id = None
        self._ctx = {}

    # ------------------------------------------------------------------ #
    # 预算与降级
    # ------------------------------------------------------------------ #

    def _check_budget(self, started: float) -> str | None:
        if self.steps >= self.max_steps:
            return LIMIT_GLOBAL_STEPS
        if self.tool_calls >= self.max_tool_calls:
            return LIMIT_TOOL_CALLS
        if self._asset_steps >= self.max_steps_per_asset:
            return LIMIT_PER_ASSET
        if time.monotonic() - started >= self.total_wall_s:
            return LIMIT_WALL_CLOCK
        return None

    def _degrade(self, exc: Exception) -> Planner | None:
        """模型不可用 → 切离线 Mock 并标记降级。已经降级过就返回 None。"""
        if self.degraded:
            return None
        self.degraded = True
        self.model = "offline"
        self.planner = OfflineMockPlanner()
        self._emit(
            "degraded",
            reason=f"{type(exc).__name__}: {exc}",
            fallback="offline_mock",
        )
        return self.planner

    # ------------------------------------------------------------------ #
    # 输出
    # ------------------------------------------------------------------ #

    def _result(self, limit_reason: str | None, fatal: dict | None) -> RunResult:
        assets = [self._records[key] for key in sorted(self._records)]
        summary = {
            # total 只统计"已结算"的素材，与下方各项计数口径一致；
            # 未开始的素材在 remaining 中单列，避免总数与明细对不上。
            "total": len(assets),
            "draft_saved": sum(1 for a in assets if a["draft_status"] == "draft_saved"),
            "blocked": sum(1 for a in assets if a["review_status"] == "blocked"),
            "needs_info": sum(1 for a in assets if a["review_status"] == "needs_info"),
            "failed": sum(1 for a in assets if a["draft_status"] == "failed"),
            "pending_confirmation": sum(
                1 for a in assets if a["pending_confirmation_items"]
            ),
        }

        if fatal is not None:
            status = RunStatus.FAILED.value
        elif limit_reason is not None:
            status = RunStatus.INCOMPLETE.value
        elif summary["failed"]:
            status = RunStatus.PARTIAL.value
        else:
            status = RunStatus.COMPLETED.value

        remaining: list[dict] = []
        if status == RunStatus.INCOMPLETE.value:
            done = set(self._records)
            if self._current_id and self._current_id not in done:
                remaining.append(
                    {
                        "asset_id": self._current_id,
                        "state": self.machine.state.value,
                        "note": "处理中，未完成",
                    }
                )
                done.add(self._current_id)
            for asset_id in self._pending:
                if asset_id not in done:
                    remaining.append(
                        {"asset_id": asset_id, "state": "NOT_STARTED", "note": "尚未开始"}
                    )

        result = RunResult(
            run_id=self.run_id,
            status=status,
            model=self.model,
            degraded=self.degraded,
            assets=assets,
            summary=summary,
            budget={
                "steps_used": self.steps,
                "steps_limit": self.max_steps,
                "tool_calls_used": self.tool_calls,
                "tool_calls_limit": self.max_tool_calls,
                "steps_per_asset_limit": self.max_steps_per_asset,
                "wall_clock_limit_s": self.total_wall_s,
            },
            remaining=remaining,
            limit_reason=limit_reason,
            error=fatal,
        )
        self._emit("final", status=status, limit_reason=limit_reason)
        return result

    # ------------------------------------------------------------------ #
    # 小工具
    # ------------------------------------------------------------------ #

    def _rejected(self, message: str, code: str) -> dict:
        return {
            "ok": False,
            "status": "rejected",
            "data": None,
            "error": {
                "code": code,
                "message": message,
                "retryable": False,
                "retry_hint": None,
            },
        }

    @staticmethod
    def _summarize(action: str, result: ToolResult) -> str:
        if not result.ok:
            return (result.error or {}).get("message", "失败")
        data = result.data or {}
        if action == "list_pending_assets":
            return f"待处理 {data.get('count')} 个素材"
        if action == "get_asset_detail":
            if data.get("complete"):
                return "字段完整"
            return f"缺字段：{', '.join(data.get('missing_fields') or [])}"
        if action == "check_rules":
            hits = ",".join(h["rule_id"] for h in data.get("hits") or []) or "-"
            return f"risk={data.get('risk_level')} hits={hits}"
        if action == "query_slots":
            occupied = [s["slot"] for s in data.get("occupied") or []]
            return f"occupied={occupied} available={data.get('available')}"
        if action == "save_draft":
            return f"draft_id={data.get('draft_id')} slot={data.get('slot')}"
        return "ok"

    @staticmethod
    def _guess_next(result: ToolResult) -> str | None:
        if not result.ok:
            code = (result.error or {}).get("code")
            if code == ErrorCode.E_SLOT_CONFLICT.value:
                return "query_slots"
            if code == ErrorCode.E_STORAGE_WRITE.value:
                return "save_draft / finish_asset"
            return None
        return None

    def _emit(self, event: str, **fields) -> None:
        if self.trace is None:
            return
        self.trace(
            {
                "run_id": self.run_id,
                "step": self.steps,
                "event": event,
                **fields,
            }
        )
