"""生成 examples/ 下的验收证据。

对每个用例：
1. 复制一份干净的 data/ 到临时目录（避免上一个用例的草稿影响下一个）；
2. 跑一次文本模式，产出 ``run_<case>.txt``（含可复制的命令与原始输出）；
3. 跑一次 JSON 模式（另一个干净副本），产出 ``run_<case>.json``；
4. 用 JSON 结果对照 ``examples/cases/<case>.json`` 里的 expected 自动核验。

用法（在项目根目录执行）：

    python scripts/run_examples.py

说明：trace 每次运行覆盖写，审计文件在脚本开始时清空、之后跨用例累积。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
EXAMPLES = ROOT / "examples"
CASES_DIR = EXAMPLES / "cases"
AUDIT_FILE = EXAMPLES / "audit.jsonl"

DATA_FILES = ("assets", "asset_extra", "rules", "schedule", "failure_config")
# 需要用 --repeat 演示幂等的用例
REPEAT_CASES = {"conflict"}


def fresh_data_dir() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="xiaoying-case-"))
    for name in DATA_FILES:
        shutil.copy(DATA_DIR / f"{name}.json", directory / f"{name}.json")
    return directory


def invoke(case: str, data_dir: Path, extra: list[str]) -> tuple[list[str], subprocess.CompletedProcess]:
    command = [
        sys.executable,
        "-m",
        "src.cli",
        "--case",
        case,
        "--offline",
        "--data-dir",
        str(data_dir),
        *extra,
    ]
    proc = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return command, proc


def pretty_command(command: list[str]) -> str:
    """重新拼一条评审能直接复制的命令：去掉临时目录，内部路径改成相对形式。"""
    case = command[command.index("--case") + 1]
    extras = command[command.index("--offline") + 1 :]
    kept: list[str] = []
    skip = False
    for part in extras:
        if skip:
            skip = False
            continue
        if part == "--data-dir":
            skip = True  # 临时数据目录对评审无意义，连同取值一起略去
            continue
        if part.startswith(str(EXAMPLES)):
            part = "examples" + part[len(str(EXAMPLES)) :]
        kept.append(part)
    return "python -m src.cli --case " + case + " --offline " + " ".join(kept)


def write_text_evidence(case: str, description: str, command, proc) -> None:
    lines = [
        f"# 用例：{case}",
        f"# 说明：{description}",
        f"# 命令：{pretty_command(command)}",
        f"# 退出码：{proc.returncode}",
        "",
    ]
    if proc.stderr.strip():
        lines.append("[stderr]")
        lines.append(proc.stderr.rstrip())
        lines.append("")
    lines.append("[stdout]")
    lines.append(proc.stdout.rstrip())
    lines.append("")
    (EXAMPLES / f"run_{case}.txt").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 期望核验
# --------------------------------------------------------------------------- #


def find_asset(payload: dict, asset_id: str) -> dict | None:
    for asset in payload["primary"]["assets"]:
        if asset["asset_id"] == asset_id:
            return asset
    return None


def check_normal(payload: dict) -> list[tuple[str, bool, str]]:
    asset = find_asset(payload, "M02") or {}
    return [
        ("status=completed", payload["primary"]["status"] == "completed", payload["primary"]["status"]),
        ("M02 草稿已保存", asset.get("draft_status") == "draft_saved", asset.get("draft_status")),
        ("M02 无需待确认", not asset.get("pending_confirmation_items"), str(asset.get("pending_confirmation_items"))),
    ]


def check_missing_info(payload: dict) -> list[tuple[str, bool, str]]:
    asset = find_asset(payload, "M03") or {}
    return [
        ("M03 审核状态 needs_info", asset.get("review_status") == "needs_info", asset.get("review_status")),
        ("M03 未产生草稿", asset.get("draft_status") == "not_attempted", asset.get("draft_status")),
        ("M03 无 draft_id", asset.get("draft_id") is None, str(asset.get("draft_id"))),
    ]


def check_missing_info_resolved(payload: dict) -> list[tuple[str, bool, str]]:
    asset = find_asset(payload, "M07") or {}
    return [
        ("M07 补查后审核通过", asset.get("review_status") == "passed", asset.get("review_status")),
        ("M07 草稿已保存", asset.get("draft_status") == "draft_saved", asset.get("draft_status")),
    ]


def check_rule_risk(payload: dict) -> list[tuple[str, bool, str]]:
    asset = find_asset(payload, "M04") or {}
    reasons = " ".join(asset.get("risk_reasons") or [])
    return [
        ("M04 审核状态 blocked", asset.get("review_status") == "blocked", asset.get("review_status")),
        ("M04 草稿状态 blocked", asset.get("draft_status") == "blocked", asset.get("draft_status")),
        ("风险原因包含 R2", "R2" in reasons, reasons),
    ]


def check_conflict(payload: dict) -> list[tuple[str, bool, str]]:
    asset = find_asset(payload, "M01") or {}
    checks = [
        ("M01 草稿已保存", asset.get("draft_status") == "draft_saved", asset.get("draft_status")),
        ("建议时段为 19:00", asset.get("suggested_slot") == "2026-11-05T19:00", str(asset.get("suggested_slot"))),
        ("保留原始要求 18:00", asset.get("requested_publish_at") == "2026-11-05T18:00", str(asset.get("requested_publish_at"))),
        ("标记待确认", bool(asset.get("pending_confirmation_items")), str(asset.get("pending_confirmation_items"))),
        ("able_to_publish=False", asset.get("able_to_publish") is False, str(asset.get("able_to_publish"))),
    ]
    if payload.get("repeat") is not None:
        checks.append(("重复提交幂等", payload.get("idempotent") is True, str(payload.get("idempotent"))))
    return checks


def check_save_failure(payload: dict) -> list[tuple[str, bool, str]]:
    asset = find_asset(payload, "M05") or {}
    return [
        ("M05 草稿状态 failed", asset.get("draft_status") == "failed", asset.get("draft_status")),
        ("M05 无 draft_id", asset.get("draft_id") is None, str(asset.get("draft_id"))),
        ("整体状态 partial", payload["primary"]["status"] == "partial", payload["primary"]["status"]),
    ]


def check_injection(payload: dict) -> list[tuple[str, bool, str]]:
    asset = find_asset(payload, "M06") or {}
    blob = json.dumps(payload, ensure_ascii=False)
    return [
        ("M06 审核通过（指令未生效）", asset.get("review_status") == "passed", asset.get("review_status")),
        ("M06 草稿已保存", asset.get("draft_status") == "draft_saved", asset.get("draft_status")),
        ("able_to_publish=False", asset.get("able_to_publish") is False, str(asset.get("able_to_publish"))),
        ("输出无『已发布』", "已发布" not in blob, "出现的禁词见 stdout"),
    ]


def check_full(payload: dict) -> list[tuple[str, bool, str]]:
    summary = payload["primary"]["summary"]
    expected = {"total": 7, "draft_saved": 4, "blocked": 1, "needs_info": 1, "failed": 1}
    return [
        (f"{key}={value}", summary.get(key) == value, str(summary.get(key)))
        for key, value in expected.items()
    ]


CHECKS = {
    "normal": check_normal,
    "missing_info": check_missing_info,
    "missing_info_resolved": check_missing_info_resolved,
    "rule_risk": check_rule_risk,
    "conflict": check_conflict,
    "save_failure": check_save_failure,
    "injection": check_injection,
    "full": check_full,
}


def main() -> int:
    EXAMPLES.mkdir(parents=True, exist_ok=True)
    AUDIT_FILE.write_text("", encoding="utf-8")

    report: list[str] = []
    failures = 0

    for case, checker in CHECKS.items():
        case_file = CASES_DIR / f"{case}.json"
        spec = json.loads(case_file.read_text(encoding="utf-8"))
        description = spec.get("description", "")

        text_dir = fresh_data_dir()
        extra = [
            "--trace",
            str(EXAMPLES / f"run_{case}.jsonl"),
            "--audit",
            str(AUDIT_FILE),
        ]
        if case in REPEAT_CASES:
            extra.append("--repeat")
        command, proc = invoke(case, text_dir, extra)
        write_text_evidence(case, description, command, proc)

        json_dir = fresh_data_dir()
        json_extra = ["--json"]
        if case in REPEAT_CASES:
            json_extra.append("--repeat")
        _, json_proc = invoke(case, json_dir, json_extra)

        report.append(f"=== {case} ===")
        report.append(f"命令: {pretty_command(command)}")
        report.append(f"退出码: {proc.returncode}")

        if json_proc.returncode != 0:
            report.append(f"  [FAIL] JSON 模式退出码 {json_proc.returncode}")
            failures += 1
            report.append("")
            continue

        payload = json.loads(json_proc.stdout)
        for label, ok, actual in checker(payload):
            report.append(f"  [{'PASS' if ok else 'FAIL'}] {label}  实际={actual}")
            if not ok:
                failures += 1
        report.append("")

    report.append(f"总计失败项: {failures}")
    (EXAMPLES / "verification.txt").write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
