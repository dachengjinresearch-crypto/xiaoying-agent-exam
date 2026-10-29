"""JSON 数据访问层：原子写、schema 校验、fail-fast。

设计要点（docs/02-solution.md §4.5）：

1. **原子写**：先写同目录临时文件 → flush → fsync → ``os.replace`` 覆盖。
   任何一步失败都删除临时文件，原文件保持不变，绝不会出现"写了一半"的草稿表。
2. **fail-fast**：数据文件缺失、无法解析或不符合 schema 时抛 ``DataCorruptError``，
   不允许静默带病运行——静默恢复会制造"看起来成功"的假象。
3. **文件名白名单**：只接受 ``[a-z][a-z0-9_]*``，从数据层阻断路径穿越。
4. **写入前校验**：``dump`` 先校验再落盘，非法对象不会污染磁盘。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from .config import DATA_DIR, DATA_FILES

ASSET_ID_RE = re.compile(r"^[A-Z]\d{2}$")
SLOT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_ASSET_REQUIRED = (
    "asset_id",
    "title",
    "channel",
    "requested_publish_at",
    "hard_requirement",
    "metadata",
    "content_flags",
)
_META_REQUIRED = ("has_cover", "has_body", "has_authorization", "duration_sec")


class DataCorruptError(Exception):
    """数据文件缺失、无法解析、不符合 schema，或写入被拒绝。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DataCorruptError(message)


# --------------------------------------------------------------------------- #
# 各数据文件的 schema 校验器
# --------------------------------------------------------------------------- #


def validate_assets(obj: dict) -> None:
    _require(isinstance(obj, dict), "assets: 顶层必须是对象")
    assets = obj.get("assets")
    _require(isinstance(assets, list) and bool(assets), "assets: assets 必须是非空列表")

    seen: set[str] = set()
    for index, asset in enumerate(assets):
        where = f"assets[{index}]"
        _require(isinstance(asset, dict), f"{where}: 必须是对象")
        for key in _ASSET_REQUIRED:
            _require(key in asset, f"{where}: 缺少字段 {key}")

        asset_id = asset["asset_id"]
        _require(
            isinstance(asset_id, str) and bool(ASSET_ID_RE.match(asset_id)),
            f"{where}: asset_id 非法 {asset_id!r}（应形如 M01）",
        )
        _require(asset_id not in seen, f"{where}: asset_id 重复 {asset_id}")
        seen.add(asset_id)

        _require(
            isinstance(asset["hard_requirement"], bool),
            f"{where}: hard_requirement 必须是布尔值",
        )
        _require(
            isinstance(asset["content_flags"], list),
            f"{where}: content_flags 必须是列表",
        )
        _require(
            isinstance(asset["requested_publish_at"], str)
            and "T" in asset["requested_publish_at"],
            f"{where}: requested_publish_at 必须形如 2026-11-05T18:00",
        )

        metadata = asset["metadata"]
        _require(isinstance(metadata, dict), f"{where}: metadata 必须是对象")
        for key in _META_REQUIRED:
            _require(key in metadata, f"{where}: metadata 缺少字段 {key}")


def validate_asset_extra(obj: dict) -> None:
    _require(isinstance(obj, dict), "asset_extra: 顶层必须是对象")
    extra = obj.get("extra")
    _require(isinstance(extra, dict), "asset_extra: extra 必须是对象")
    for asset_id, patch in extra.items():
        _require(
            isinstance(asset_id, str) and bool(ASSET_ID_RE.match(asset_id)),
            f"asset_extra: asset_id 非法 {asset_id!r}",
        )
        _require(isinstance(patch, dict), f"asset_extra[{asset_id}]: 必须是对象")


def validate_rules(obj: dict) -> None:
    _require(isinstance(obj, dict), "rules: 顶层必须是对象")
    _require(isinstance(obj.get("version"), str) and obj["version"], "rules: 缺少 version")
    rules = obj.get("rules")
    _require(isinstance(rules, list) and bool(rules), "rules: rules 必须是非空列表")
    for index, rule in enumerate(rules):
        where = f"rules[{index}]"
        _require(isinstance(rule, dict), f"{where}: 必须是对象")
        for key in ("rule_id", "desc", "level"):
            _require(key in rule, f"{where}: 缺少字段 {key}")
        _require(
            rule["level"] in ("block", "warn"),
            f"{where}: level 必须是 block 或 warn，实际 {rule['level']!r}",
        )


def validate_schedule(obj: dict) -> None:
    _require(isinstance(obj, dict), "schedule: 顶层必须是对象")
    channels = obj.get("channels")
    _require(isinstance(channels, dict) and bool(channels), "schedule: channels 必须是非空对象")
    for channel, days in channels.items():
        _require(isinstance(days, dict), f"schedule[{channel}]: 必须是对象")
        for date, slots in days.items():
            _require(isinstance(slots, dict), f"schedule[{channel}][{date}]: 必须是对象")
            for clock, entry in slots.items():
                where = f"schedule[{channel}][{date}][{clock}]"
                _require(bool(SLOT_RE.match(clock)), f"{where}: 时段格式必须是 HH:MM")
                _require(isinstance(entry, dict), f"{where}: 必须是对象")
                status = entry.get("status")
                _require(
                    status in ("free", "occupied"),
                    f"{where}: status 必须是 free 或 occupied，实际 {status!r}",
                )
                if status == "occupied":
                    _require(entry.get("asset_id"), f"{where}: 占用时段必须带 asset_id")


def validate_failure_config(obj: dict) -> None:
    _require(isinstance(obj, dict), "failure_config: 顶层必须是对象")
    targets = obj.get("save_draft_fail_for")
    _require(isinstance(targets, list), "failure_config: save_draft_fail_for 必须是列表")
    for asset_id in targets:
        _require(
            isinstance(asset_id, str) and bool(ASSET_ID_RE.match(asset_id)),
            f"failure_config: asset_id 非法 {asset_id!r}",
        )


VALIDATORS: dict[str, Callable[[dict], None]] = {
    "assets": validate_assets,
    "asset_extra": validate_asset_extra,
    "rules": validate_rules,
    "schedule": validate_schedule,
    "failure_config": validate_failure_config,
}


# --------------------------------------------------------------------------- #
# 仓储
# --------------------------------------------------------------------------- #


class Repository:
    """按名称读写 JSON 数据文件。

    未知名称（如 ``drafts``）不做 schema 校验，仅保证 JSON 可解析；
    已知名称一律按 ``VALIDATORS`` 校验。
    """

    def __init__(self, root: str | Path = DATA_DIR) -> None:
        self.root = Path(root)

    # -- 路径 ---------------------------------------------------------- #

    def path(self, name: str) -> Path:
        _require(
            isinstance(name, str) and bool(SAFE_NAME_RE.match(name)),
            f"非法数据文件名 {name!r}（只允许 [a-z][a-z0-9_]*，用于阻断路径穿越）",
        )
        return self.root / f"{name}.json"

    # -- 读 ------------------------------------------------------------ #

    def load(self, name: str, *, validate: bool = True) -> dict:
        path = self.path(name)
        if not path.is_file():
            raise DataCorruptError(f"{name}: 数据文件不存在 {path}")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DataCorruptError(f"{name}: 读取失败 {exc}") from exc
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DataCorruptError(f"{name}: JSON 解析失败 {exc}") from exc
        _require(isinstance(obj, dict), f"{name}: 顶层必须是对象")
        if validate:
            self.validate(name, obj)
        return obj

    def load_all(self, names: tuple[str, ...] = DATA_FILES) -> dict[str, dict]:
        return {name: self.load(name) for name in names}

    # -- 写 ------------------------------------------------------------ #

    def dump(self, name: str, obj: dict, *, validate: bool = True) -> Path:
        path = self.path(name)
        if validate:
            self.validate(name, obj)
        self._atomic_write(path, obj)
        return path

    # -- 校验 ---------------------------------------------------------- #

    def validate(self, name: str, obj: dict) -> None:
        validator = VALIDATORS.get(name)
        if validator is not None:
            validator(obj)

    # -- 内部 ---------------------------------------------------------- #

    def _atomic_write(self, path: Path, obj: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.root), prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(obj, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
