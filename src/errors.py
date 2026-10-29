"""领域层统一错误类型。

服务层抛 ``ServiceError``，工具层（T7）负责把它转成 ``ToolResult`` 信封，
**绝不让异常穿透到运行时循环**。
"""

from __future__ import annotations

from .enums import ErrorCode, is_retryable


class ServiceError(Exception):
    """领域层的可预期失败。携带错误码，供模型判断是否值得重试。"""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code if isinstance(code, ErrorCode) else ErrorCode(code)
        self.message = message
        self.retryable = is_retryable(self.code) if retryable is None else retryable

    def to_dict(self) -> dict:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ServiceError({self.code.value}: {self.message})"
