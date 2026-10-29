"""命令行入口。

用法（评审入口）：

    python -m src.cli --case conflict --offline
    python -m src.cli --case conflict --offline --repeat
    python -m src.cli --case full --offline --json
    python -m src.cli --list-cases

设计要点：
- 输出**由结构化结果渲染**，不拼接模型的自然语言。
- 渲染后过一遍 ``HonestyGate``：命中禁词或与事实不一致就**拒绝输出并报错**，
  绝不静默改写。退出码 2 表示诚实层拦截，3 表示幂等校验失败。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import CASES_DIR, DATA_DIR
from .honesty import HonestyViolation, mask_sensitive, status_label, verify_text
from .llm import build_planner
from .observability import AuditLog, TraceWriter
from .repository import DataCorruptError, Repository
from .runtime import AgentRuntime
from .tools import ToolRegistry

# 内置用例：只声明"本批次处理哪些素材"，输入数据仍在 data/ 下
BUILTIN_CASES: dict[str, dict] = {
    "normal": {"only": ["M02"], "description": "正常排期：无风险、时段可用"},
    "missing_info": {"only": ["M03"], "description": "素材信息缺失：补查后仍缺，阻断"},
    "missing_info_resolved": {"only": ["M07"], "description": "补查有来源：补齐后可排期"},
    "rule_risk": {"only": ["M04"], "description": "规则风险：命中 R2，阻断"},
    "conflict": {"only": ["M01"], "description": "时段冲突：18:00 被占用，改期 19:00 待确认"},
    "save_failure": {"only": ["M05"], "description": "工具失败：保存返回失败，不产生草稿"},
    "injection": {"only": ["M06"], "description": "注入样本：标题含越权指令，系统不受影响"},
    "full": {"only": None, "description": "全量批处理：七个素材一次跑完"},
}


def load_case(name: str, cases_dir: Path = CASES_DIR) -> dict:
    path = Path(cases_dir) / f"{name}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    if name in BUILTIN_CASES:
        return dict(BUILTIN_CASES[name], case=name)
    raise SystemExit(
        f"未知用例 {name!r}。可用用例：{', '.join(sorted(BUILTIN_CASES))}"
    )


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def render(result, case_name: str) -> str:
    budget = result.budget
    lines = [
        f"run_id       : {result.run_id}",
        f"case         : {case_name}",
        f"model        : {result.model} (degraded={str(result.degraded).lower()})",
        f"status       : {result.status}",
    ]
    if result.limit_reason:
        lines.append(f"limit_reason : {result.limit_reason}")
    if result.error:
        lines.append(f"error        : {result.error.get('code')} - {result.error.get('message')}")
    lines.append(
        "budget       : "
        f"steps {budget['steps_used']}/{budget['steps_limit']}, "
        f"tools {budget['tool_calls_used']}/{budget['tool_calls_limit']}"
    )
    lines.append("")

    header = (
        f"{'asset':<6}{'review':<12}{'status':<12}{'draft_id':<10}"
        f"{'requested':<20}{'suggested':<20}{'confirm'}"
    )
    lines.append(header)
    lines.append("-" * 92)

    if not result.assets:
        lines.append("(本次运行没有已结算的素材)")
    for record in result.assets:
        label = status_label(record["review_status"], record["draft_status"])
        confirm = "待确认" if record["pending_confirmation_items"] else "-"
        lines.append(
            f"{record['asset_id']:<6}"
            f"{record['review_status']:<12}"
            f"{label:<12}"
            f"{(record['draft_id'] or '-'):<10}"
            f"{(record['requested_publish_at'] or '-'):<20}"
            f"{(record['suggested_slot'] or '-'):<20}"
            f"{confirm}"
        )

    details = []
    for record in result.assets:
        for reason in record["risk_reasons"]:
            details.append(f"  风险     {record['asset_id']}: {reason}")
        for item in record["pending_confirmation_items"]:
            details.append(f"  待确认   {record['asset_id']}: {item}")
    if details:
        lines.append("")
        lines.extend(details)

    summary = result.summary
    lines.append("")
    lines.append(
        "summary      : "
        f"total={summary['total']} "
        f"已保存草稿={summary['draft_saved']} "
        f"阻断={summary['blocked']} "
        f"待补资料={summary['needs_info']} "
        f"保存失败={summary['failed']} "
        f"待确认={summary['pending_confirmation']}"
    )
    if result.remaining:
        lines.append(
            "remaining    : "
            + ", ".join(f"{item['asset_id']}({item['state']})" for item in result.remaining)
        )
    return "\n".join(lines) + "\n"


def repeat_report(first, second) -> tuple[str, bool]:
    first_ids = {r["asset_id"]: r["draft_id"] for r in first.assets}
    second_ids = {r["asset_id"]: r["draft_id"] for r in second.assets}
    common = sorted(set(first_ids) & set(second_ids))
    lines = ["", "重复提交校验:"]
    consistent = True
    if not common:
        lines.append("  (本次没有可比对的草稿)")
    for asset_id in common:
        same = first_ids[asset_id] == second_ids[asset_id]
        consistent = consistent and same
        mark = "一致（幂等）" if same else "不一致（缺陷）"
        lines.append(
            f"  {asset_id}: 第一次={first_ids[asset_id]} 第二次={second_ids[asset_id]} {mark}"
        )
    return "\n".join(lines), consistent


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def _display_path(path: Path) -> str:
    """对外展示用相对路径，避免把绝对路径写进可提交的日志。"""
    try:
        return str(Path(os.path.relpath(path, Path.cwd())))
    except (ValueError, OSError):
        return path.name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="素材审核与发布排期 Agent（只保存草稿，不接真实发布）",
    )
    parser.add_argument("--case", default="full", help="用例名，见 --list-cases")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="强制使用离线 Mock 规划器。不加此参数时会尝试真实模型，缺少配置则自动回落",
    )
    parser.add_argument(
        "--repeat", action="store_true", help="重复提交同一请求，验证草稿不重复"
    )
    parser.add_argument("--data-dir", default=str(DATA_DIR), help="数据目录")
    parser.add_argument("--only", default=None, help="逗号分隔的素材 ID，覆盖用例范围")
    parser.add_argument(
        "--trace", default=None, help="把决策轨迹写成本地 JSONL（每次运行覆盖）"
    )
    parser.add_argument(
        "--audit", default=None, help="把工具调用审计追加到 JSONL（跨运行累积）"
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--list-cases", action="store_true", help="列出内置用例")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_cases:
        for name, spec in sorted(BUILTIN_CASES.items()):
            print(f"{name:<24}{spec['description']}")
        return 0

    try:
        case = load_case(args.case)
    except (SystemExit, json.JSONDecodeError) as exc:
        print(f"[CASE ERROR] {exc}", file=sys.stderr)
        return 1

    only = case.get("only")
    if args.only:
        only = [item.strip() for item in args.only.split(",") if item.strip()]

    try:
        trace_writer = TraceWriter(args.trace) if args.trace else None
        audit_log = AuditLog(args.audit) if args.audit else None

        repo = Repository(Path(args.data_dir))
        registry = ToolRegistry(
            repo,
            audit=audit_log.record if audit_log else None,
            asset_filter=set(only) if only else None,
        )

        planner, note = build_planner(prefer_online=not args.offline)
        print(f"[NOTICE] {note}", file=sys.stderr)
        if trace_writer:
            print(f"[NOTICE] trace 写入 {_display_path(trace_writer.path)}", file=sys.stderr)
        if audit_log:
            print(f"[NOTICE] 审计追加到 {_display_path(audit_log.path)}", file=sys.stderr)

        first = AgentRuntime(
            registry, planner, trace=trace_writer, run_id=f"run-{args.case}"
        ).run()

        second = None
        consistent = True
        if args.repeat:
            repeat_planner, _ = build_planner(prefer_online=not args.offline)
            second = AgentRuntime(
                registry,
                repeat_planner,
                trace=trace_writer,
                run_id=f"run-{args.case}-repeat",
            ).run()
    except DataCorruptError as exc:
        print(f"[DATA ERROR] {exc}", file=sys.stderr)
        return 1

    text = render(first, args.case)
    if second is not None:
        report, consistent = repeat_report(first, second)
        text += report + "\n"

    text = mask_sensitive(text)
    try:
        # 用完整文本校验两次运行：repeat 段是全文的一部分，
        # 单独拿它去比对"必须出现待确认"会误报。
        verify_text(text, first)
        if second is not None:
            verify_text(text, second)
    except HonestyViolation as exc:
        print(f"[HONESTY VIOLATION] {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = {
            "primary": first.to_dict(),
            "repeat": second.to_dict() if second is not None else None,
            "idempotent": consistent if second is not None else None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text, end="")

    if second is not None and not consistent:
        print("[IDEMPOTENCY VIOLATION] 重复提交产生了不同的草稿", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
