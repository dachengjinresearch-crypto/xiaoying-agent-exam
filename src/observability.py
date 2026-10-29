"""观测层：决策轨迹（Trace）与保存审计（Audit）。

两者职责不重叠，刻意分开写：

- **Trace** 面向评审，记录决策链。字段固定、可为体积裁剪，用于回答
  "模型看到了什么、做了什么、为什么调整"。
- **Audit** 面向追责，记录**每一次保存尝试**（成功与失败都记），
  append-only、永不覆盖，用于回答"到底有没有写进去"。

Trace 只记事实型字段，**不记思维链**——题目明确无需提交模型内部推理过程，
记录推理反而可能被判定为无效决策日志。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 固定字段顺序：保证同一份 trace 在任何一次运行里列对齐
TRACE_FIELDS = (
    "run_id",
    "ts",
    "step",
    "event",
    "asset_id",
    "action",
    "tool_args",
    "result_summary",
    "next_action",
    "reason",
    "state_before",
    "state_after",
    "error_code",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _write_line(path: Path, payload: dict) -> None:
    _ensure_parent(path)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()


class TraceWriter:
    """运行时的事件接收器（可调用对象），负责标准化并落盘。

    只保留 ``TRACE_FIELDS`` 中的字段：模型若在事件里塞了 `thought`、
    `chain_of_thought` 之类的键，会被**丢弃**而不是写进 trace。
    """

    def __init__(self, path: str | Path, *, echo: bool = False) -> None:
        self.path = Path(path)
        self._echo = echo
        # 每次运行开始前清空，避免多次运行混在同一个文件里
        _ensure_parent(self.path)
        self.path.write_text("", encoding="utf-8")

    def __call__(self, event: dict) -> None:
        record = self._normalize(event)
        self._append(record)

    def _append(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
        if self._echo:
            print(json.dumps(record, ensure_ascii=False))

    @staticmethod
    def _normalize(event: dict) -> dict:
        return {
            "run_id": event.get("run_id"),
            "ts": event.get("ts") or _now(),
            "step": event.get("step"),
            "event": event.get("event"),
            "asset_id": event.get("asset_id"),
            "action": event.get("action"),
            "tool_args": event.get("args"),
            "result_summary": event.get("summary"),
            "next_action": event.get("next_action"),
            "reason": event.get("reason"),
            "state_before": event.get("state_before"),
            "state_after": event.get("state_after"),
            "error_code": event.get("error"),
        }

    def close(self) -> None:
        """TraceWriter 逐行写入并 flush，无需额外关闭动作。"""


class AuditLog:
    """保存尝试的 append-only 记录。从不截断，跨运行累积。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def record(self, tool: str, args: dict, result: Any) -> None:
        """匹配 ``ToolRegistry`` 的审计钩子签名。"""
        error = getattr(result, "error", None) or {}
        payload = {
            "ts": _now(),
            "tool": tool,
            "args": args,
            "ok": bool(getattr(result, "ok", False)),
            "status": getattr(result, "status", None),
            "error_code": error.get("code"),
        }
        _write_line(self.path, payload)

    def entries(self) -> list[dict]:
        if not self.path.is_file():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]
