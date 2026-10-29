"""工具层：把领域服务包装成模型可调用的工具。

三条硬规则（docs/02-solution.md §4.3）：

1. **异常绝不穿透**。任何错误都转成 ``ToolResult`` 信封返回给模型，
   让模型能"观察到失败并调整"，而不是让运行时崩溃。
2. **错误码自带 retryable**。模型据 ``retry_hint`` 判断该不该重试，
   避免用无脑重试冒充决策。
3. **写盘前的两道闸**：规则阻断的素材不允许保存；故障注入在触碰磁盘之前拦截。
   即使上层护栏失效，这两个检查也能挡住"越权保存"。

另外，``save_draft`` **刻意不暴露** ``able_to_publish`` 参数——
系统不具备发布能力，模型无从请求"直接发布"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from .enums import ErrorCode
from .errors import ServiceError
from .repository import DataCorruptError, Repository
from .services import AssetService, DraftService, RuleEngine, ScheduleService

ASSET_ID_RE = re.compile(r"^[A-Z]\d{2}$")

AUDIT_SINK = Callable[[str, dict, "ToolResult"], None]


@dataclass(frozen=True)
class ToolResult:
    """统一返回信封。``ok`` 与 ``status`` 语义一致，保留两者是为了可读性。"""

    ok: bool
    status: str
    data: dict | None = None
    error: dict | None = None

    # -- 构造 ---------------------------------------------------------- #

    @classmethod
    def success(cls, data: dict) -> "ToolResult":
        return cls(ok=True, status="ok", data=data, error=None)

    @classmethod
    def failure(
        cls,
        code: ErrorCode | str,
        message: str,
        retryable: bool,
        retry_hint: str | None = None,
    ) -> "ToolResult":
        return cls(
            ok=False,
            status="failed",
            data=None,
            error={
                "code": code.value if isinstance(code, ErrorCode) else code,
                "message": message,
                "retryable": retryable,
                "retry_hint": retry_hint,
            },
        )

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "data": self.data,
            "error": self.error,
        }


def _require_str(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(
            ErrorCode.E_SCHEMA_INVALID, f"参数 {key} 必须是非空字符串", retryable=False
        )
    return value


def _require_asset_id(args: dict) -> str:
    asset_id = _require_str(args, "asset_id")
    if not ASSET_ID_RE.match(asset_id):
        raise ServiceError(
            ErrorCode.E_SCHEMA_INVALID,
            f"asset_id 非法：{asset_id!r}（应形如 M01）",
            retryable=False,
        )
    return asset_id


class ToolRegistry:
    TOOL_NAMES = (
        "list_pending_assets",
        "get_asset_detail",
        "check_rules",
        "query_slots",
        "save_draft",
    )

    def __init__(
        self,
        repo: Repository,
        *,
        audit: AUDIT_SINK | None = None,
        asset_filter: set[str] | None = None,
    ) -> None:
        self.repo = repo
        self.assets = AssetService(repo)
        self.rules = RuleEngine(repo, self.assets)
        self.schedule = ScheduleService(repo)
        self.drafts = DraftService(repo)
        self.rules_version: str = self.rules.version
        # 本批次要处理的素材范围；None 表示全部
        self.asset_filter = asset_filter

        failure_config = repo.load("failure_config")
        self._fail_targets = set(failure_config.get("save_draft_fail_for", []))

        self._audit: AUDIT_SINK | None = audit

    # ------------------------------------------------------------------ #
    # 调度
    # ------------------------------------------------------------------ #

    def call(self, name: str, args: dict | None = None) -> ToolResult:
        args = args or {}
        if not isinstance(args, dict):
            result = ToolResult.failure(
                ErrorCode.E_SCHEMA_INVALID, "工具参数必须是对象", retryable=False
            )
            self._emit(name, {}, result)
            return result

        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            result = ToolResult.failure(
                ErrorCode.E_NOT_FOUND,
                f"未知工具 {name!r}，可用工具：{', '.join(self.TOOL_NAMES)}",
                retryable=False,
            )
            self._emit(name, args, result)
            return result

        try:
            result = ToolResult.success(handler(args))
        except ServiceError as exc:
            result = ToolResult.failure(exc.code, exc.message, exc.retryable)
        except DataCorruptError as exc:
            result = ToolResult.failure(ErrorCode.E_INTEGRITY, str(exc), retryable=False)
        except Exception as exc:  # noqa: BLE001 - 工具层是最后一道兜底
            result = ToolResult.failure(
                ErrorCode.E_INTEGRITY,
                f"未预期的工具异常：{type(exc).__name__}: {exc}",
                retryable=False,
            )

        self._emit(name, args, result)
        return result

    def _emit(self, name: str, args: dict, result: ToolResult) -> None:
        if self._audit is not None:
            self._audit(name, args, result)

    # ------------------------------------------------------------------ #
    # 工具实现
    # ------------------------------------------------------------------ #

    def _tool_list_pending_assets(self, args: dict) -> dict:
        channel = args.get("channel")
        if channel is not None and not isinstance(channel, str):
            raise ServiceError(
                ErrorCode.E_SCHEMA_INVALID, "channel 必须是字符串", retryable=False
            )
        assets = self.assets.list_pending(channel, only=self.asset_filter)
        return {"count": len(assets), "assets": assets}

    def _tool_get_asset_detail(self, args: dict) -> dict:
        return self.assets.detail(_require_asset_id(args))

    def _tool_check_rules(self, args: dict) -> dict:
        return self.rules.check(_require_asset_id(args))

    def _tool_query_slots(self, args: dict) -> dict:
        channel = _require_str(args, "channel")
        date = _require_str(args, "date")
        return self.schedule.slots(channel, date)

    def _tool_save_draft(self, args: dict) -> dict:
        asset_id = _require_asset_id(args)
        slot = _require_str(args, "slot")
        pending_confirmation = args.get("pending_confirmation", False)
        if not isinstance(pending_confirmation, bool):
            # 不能用 bool() 强转：字符串 "false" 会被判为真，导致绕过确认边界
            raise ServiceError(
                ErrorCode.E_SCHEMA_INVALID,
                "pending_confirmation 必须是布尔值",
                retryable=False,
            )
        reason = args.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ServiceError(
                ErrorCode.E_SCHEMA_INVALID, "reason 必须是字符串", retryable=False
            )

        asset = self.assets.detail(asset_id)

        # 闸门一：规则阻断的素材不允许保存（纵深防御，T8 也会拦）
        rule_result = self.rules.check(asset_id)
        if self.rules.is_blocked(rule_result):
            hits = "；".join(h["reason"] for h in rule_result["hits"])
            raise ServiceError(
                ErrorCode.E_RULE_BLOCKED,
                f"素材 {asset_id} 命中阻断规则，不能排期：{hits}",
                retryable=False,
            )

        # 闸门二：故障注入在触碰磁盘之前拦截
        if asset_id in self._fail_targets:
            raise ServiceError(
                ErrorCode.E_STORAGE_WRITE,
                f"素材 {asset_id} 的草稿写入失败（故障注入）",
                retryable=True,
            )

        return self.drafts.save(
            asset,
            slot,
            rules_version=self.rules_version,
            pending_confirmation=pending_confirmation,
            reason=reason,
        )
