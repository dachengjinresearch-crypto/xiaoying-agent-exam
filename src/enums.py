"""全局唯一的状态与错误码枚举。

docs/02-solution.md §6 规定：全篇使用本文件的枚举，禁止裸字符串、禁止新增枚举。
若业务确需新状态，先改本节与文档，再改代码。
"""

from enum import Enum


class AssetState(str, Enum):
    """单个素材在处理流程中的状态，由 StateMachine 维护。"""

    INIT = "INIT"
    ASSET_SELECTED = "ASSET_SELECTED"
    DETAIL_LOADED = "DETAIL_LOADED"
    NEEDS_INFO = "NEEDS_INFO"
    RULES_READ = "RULES_READ"
    BLOCKED = "BLOCKED"
    SLOTS_READ = "SLOTS_READ"
    DRAFT_SAVED = "DRAFT_SAVED"
    SAVE_FAILED = "SAVE_FAILED"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"


class DraftStatus(str, Enum):
    """草稿层的保存结果。注意：不存在"已发布"这一取值。"""

    NOT_ATTEMPTED = "not_attempted"
    DRAFT_SAVED = "draft_saved"
    FAILED = "failed"
    BLOCKED = "blocked"


class ReviewStatus(str, Enum):
    """对外输出的审核状态。"""

    PASSED = "passed"
    BLOCKED = "blocked"
    NEEDS_INFO = "needs_info"


class RunStatus(str, Enum):
    """一次运行的总体状态。"""

    COMPLETED = "completed"
    PARTIAL = "partial"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class ErrorCode(str, Enum):
    """工具层统一错误码，决定模型是否应当重试。"""

    E_NOT_FOUND = "E_NOT_FOUND"
    E_MISSING_FIELDS = "E_MISSING_FIELDS"
    E_RULE_BLOCKED = "E_RULE_BLOCKED"
    E_SLOT_CONFLICT = "E_SLOT_CONFLICT"
    E_STORAGE_WRITE = "E_STORAGE_WRITE"
    E_STORAGE_LOCK = "E_STORAGE_LOCK"
    E_TIMEOUT = "E_TIMEOUT"
    E_UPSTREAM_MODEL = "E_UPSTREAM_MODEL"
    E_SCHEMA_INVALID = "E_SCHEMA_INVALID"
    E_LIMIT_EXCEEDED = "E_LIMIT_EXCEEDED"
    E_INTEGRITY = "E_INTEGRITY"


# 可重试错误码：只有这些允许模型再次尝试同一动作。
RETRYABLE_ERRORS: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.E_SLOT_CONFLICT,
        ErrorCode.E_STORAGE_WRITE,
        ErrorCode.E_STORAGE_LOCK,
        ErrorCode.E_TIMEOUT,
        ErrorCode.E_UPSTREAM_MODEL,
        ErrorCode.E_SCHEMA_INVALID,
    }
)


def is_retryable(code: ErrorCode | str) -> bool:
    """错误码是否可重试。字符串入参便于从工具返回值直接判断。"""
    try:
        return ErrorCode(code) in RETRYABLE_ERRORS
    except ValueError:
        return False
