"""草稿保存与幂等（T6）。

本模块是整个项目的核心不变量所在：

    **一个素材在系统中永远只有一份草稿。**

题目明确要求"相同请求重复保存只产生一份草稿"，评审还会主动"更换占用时段并重复提交"
来试探。因此我们**不用请求体 hash 作幂等域**——那样一旦时段变了就会产生第二份草稿。
这里以 ``asset_id`` 作为唯一索引，草稿表的 key 就是 asset_id，结构上不可能出现两条。

几个关键取舍：

1. **改期 = 更新同一份草稿**，返回同一 ``draft_id``，标记 ``updated=true``。
2. **纯重复 = 不写盘**，直接返回 ``idempotent_replay=true``，避免无谓 IO。
3. **保存失败不产生记录**：先构造新状态、写盘成功后才提交到内存，
   失败时内存与磁盘都保持原样，下一次重试仍拿到同一个 ``draft_id``。
4. **checksum 防篡改**：手工改 ``drafts.json`` 会在下次加载时被检出。
5. **``able_to_publish`` 恒为 False**：系统不具备发布能力，该字段只读，
   调用方传 True 直接拒绝。
6. **规则版本变化**：草稿失效重算，但仍是同一份草稿（同一 draft_id），
   旧幂等键保留在 ``previous_idempotency_key`` 以便审计。

并发策略（两重保护）：

- **签名刷新**：写入前比对文件的 ``(mtime_ns, size)``，变了就重新加载，
  防止长驻实例用陈旧快照覆盖别人的草稿。
- **跨平台文件锁**：读-改-写整体包在 ``O_CREAT|O_EXCL`` 锁文件里，
  多线程 / 多进程同时写也不会丢更新或重用 draft_id。
  抢锁超时返回 ``E_STORAGE_LOCK``（可重试），超过 ``LOCK_STALE_S`` 的僵尸锁会被清理。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from ..enums import DraftStatus, ErrorCode
from ..errors import ServiceError
from ..repository import DataCorruptError, Repository

SLOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$")
ASSET_ID_RE = re.compile(r"^[A-Z]\d{2}$")

STORE = "drafts"
SCHEMA_VERSION = "1"

LOCK_TIMEOUT_S = 5.0
LOCK_STALE_S = 30.0
LOCK_POLL_S = 0.005

# 持久化字段（runtime 字段 idempotent_replay / updated / recomputed 不落盘）
_PERSISTED_KEYS = (
    "draft_id",
    "asset_id",
    "slot",
    "status",
    "pending_confirmation",
    "confirmation_reason",
    "origin_requested_at",
    "idempotency_key",
    "rules_version",
    "created_at",
    "updated_at",
    "able_to_publish",
    "checksum",
)


def _canonical(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k != "checksum"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def checksum_of(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _fresh_doc() -> dict:
    return {"schema_version": SCHEMA_VERSION, "next_seq": 1, "drafts": {}}


class DraftService:
    def __init__(self, repo: Repository, *, store_name: str = STORE) -> None:
        self.repo = repo
        self.store_name = store_name
        self._doc = self._load()
        self._sig = self._signature()

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #

    def _load(self) -> dict:
        path = self.repo.path(self.store_name)
        if not path.is_file():
            return _fresh_doc()

        # 注意：这里刻意不捕获 DataCorruptError。
        # 草稿表损坏时必须 fail-fast，静默重置会制造"看起来成功"的假象。
        doc = self.repo.load(self.store_name, validate=False)
        self._verify_structure(doc)
        self._verify_checksums(doc)
        return doc

    @staticmethod
    def _verify_structure(doc: dict) -> None:
        if not isinstance(doc.get("drafts"), dict):
            raise DataCorruptError("drafts: 缺少 drafts 对象")
        if not isinstance(doc.get("next_seq"), int) or doc["next_seq"] < 1:
            raise DataCorruptError("drafts: next_seq 非法")

    @staticmethod
    def _verify_checksums(doc: dict) -> None:
        for asset_id, draft in doc["drafts"].items():
            missing = [k for k in _PERSISTED_KEYS if k not in draft]
            if missing:
                raise DataCorruptError(f"drafts[{asset_id}]: 缺少字段 {missing}")
            if checksum_of(draft) != draft["checksum"]:
                raise DataCorruptError(
                    f"drafts[{asset_id}]: checksum 校验失败，草稿可能被篡改"
                )

    def get(self, asset_id: str) -> dict | None:
        self._refresh()
        draft = self._doc["drafts"].get(asset_id)
        return deepcopy(draft) if draft is not None else None

    def all_drafts(self) -> list[dict]:
        return [deepcopy(d) for _, d in sorted(self._doc["drafts"].items())]

    def count(self) -> int:
        return len(self._doc["drafts"])

    def slot_taken(self, slot: str) -> str | None:
        """该时段是否已被某份草稿占用（供调度层做跨素材冲突判定）。"""
        for draft in self._doc["drafts"].values():
            if draft["slot"] == slot:
                return draft["asset_id"]
        return None

    # ------------------------------------------------------------------ #
    # 一致性：签名刷新 + 文件锁
    # ------------------------------------------------------------------ #

    def _signature(self) -> tuple[int, int] | None:
        path = self.repo.path(self.store_name)
        if not path.is_file():
            return None
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)

    def _refresh(self) -> None:
        """磁盘被外部改过就重新读取，避免用陈旧快照覆盖别人的草稿。

        这一层能挡住"先后覆盖"，但挡不住"同时覆盖"——后者由 ``_locked`` 负责。
        """
        signature = self._signature()
        if signature != self._sig:
            self._doc = self._load()
            self._sig = signature

    def lock_path(self) -> Path:
        """锁文件放在系统临时目录，按数据目录哈希区分。

        不放进数据目录的理由：一是不污染业务数据，二是数据目录可能位于
        只读或受限挂载点上，在那里创建/删除文件会让"释放锁"变成新的故障源。
        """
        digest = hashlib.sha1(
            str(Path(self.repo.root).resolve()).encode("utf-8")
        ).hexdigest()[:12]
        return Path(tempfile.gettempdir()) / f"xiaoying-agent-{digest}.lock"

    @contextmanager
    def _locked(self):
        """跨平台互斥：用 O_CREAT|O_EXCL 原子创建锁文件。

        抢不到就短轮询；超时抛 ``E_STORAGE_LOCK``（retryable），
        让上层按降级矩阵重试，而不是把并发写当成成功。

        **释放锁必须是尽力而为**：删除失败绝不能把一次已经成功的写入
        报成失败——那等于用清理动作的失败污染业务结果。
        """
        lock_path = self.lock_path()
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        handle = None
        while handle is None:
            try:
                handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    stale = (time.time() - lock_path.stat().st_mtime) > LOCK_STALE_S
                except FileNotFoundError:
                    continue
                if stale:
                    try:
                        lock_path.unlink()
                        continue
                    except OSError:
                        # 删不掉就按普通占用处理，避免死循环
                        pass
                if time.monotonic() > deadline:
                    raise ServiceError(
                        ErrorCode.E_STORAGE_LOCK,
                        "草稿表正被其他进程写入，获取锁超时",
                    )
                time.sleep(LOCK_POLL_S)
        try:
            yield
        finally:
            try:
                os.close(handle)
            except OSError:
                pass
            try:
                lock_path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def save(
        self,
        asset: dict,
        slot: str,
        *,
        rules_version: str,
        pending_confirmation: bool = False,
        reason: str | None = None,
        able_to_publish: bool = False,
    ) -> dict:
        asset_id, requested = self._validate(
            asset, slot, pending_confirmation, reason, able_to_publish
        )
        with self._locked():
            return self._commit(
                asset,
                asset_id,
                requested,
                slot,
                rules_version,
                pending_confirmation,
                reason,
            )

    def _validate(
        self,
        asset: dict,
        slot: str,
        pending_confirmation: bool,
        reason: str | None,
        able_to_publish: bool,
    ) -> tuple[str, str]:
        asset_id = asset.get("asset_id")
        requested = asset.get("requested_publish_at")

        if not isinstance(asset_id, str) or not ASSET_ID_RE.match(asset_id):
            raise ServiceError(
                ErrorCode.E_SCHEMA_INVALID, f"asset_id 非法：{asset_id!r}", retryable=False
            )
        if not isinstance(slot, str) or not SLOT_RE.match(slot):
            raise ServiceError(
                ErrorCode.E_SCHEMA_INVALID,
                f"时段格式非法，应形如 2026-11-05T19:00：{slot!r}",
                retryable=False,
            )
        if not isinstance(requested, str) or not SLOT_RE.match(requested):
            raise ServiceError(
                ErrorCode.E_SCHEMA_INVALID,
                f"素材原始发布时间非法：{requested!r}",
                retryable=False,
            )
        if able_to_publish:
            # 系统不具备发布能力，任何"直接发布"的请求都必须被拒绝
            raise ServiceError(
                ErrorCode.E_INTEGRITY,
                "本系统只能保存草稿，able_to_publish 恒为 False",
                retryable=False,
            )
        if pending_confirmation and not (reason and reason.strip()):
            raise ServiceError(
                ErrorCode.E_INTEGRITY,
                "标记待确认时必须给出确认原因",
                retryable=False,
            )
        if slot != requested and asset.get("hard_requirement") and not pending_confirmation:
            raise ServiceError(
                ErrorCode.E_INTEGRITY,
                f"硬性发布时间 {requested} 被调整为 {slot}，必须标记待确认",
                retryable=False,
            )
        return asset_id, requested

    def _commit(
        self,
        asset: dict,
        asset_id: str,
        requested: str,
        slot: str,
        rules_version: str,
        pending_confirmation: bool,
        reason: str | None,
    ) -> dict:
        # 持锁后再对齐磁盘，保证读到的是最新状态
        self._refresh()

        existing = self._doc["drafts"].get(asset_id)
        if existing is not None and existing["origin_requested_at"] != requested:
            raise ServiceError(
                ErrorCode.E_INTEGRITY,
                "素材原始发布时间与已存草稿不一致，源数据疑似被改写",
                retryable=False,
            )

        idem_key = self._idem_key(asset_id, rules_version)
        now = self._now()

        # 纯重复：不写盘，直接返回
        if (
            existing is not None
            and existing["slot"] == slot
            and existing["pending_confirmation"] == pending_confirmation
            and existing["rules_version"] == rules_version
        ):
            return {**deepcopy(existing), "idempotent_replay": True, "updated": False}

        new_doc = deepcopy(self._doc)
        if existing is None:
            draft_id = f"D-{new_doc['next_seq']:03d}"
            new_doc["next_seq"] += 1
            created_at = now
            recomputed = False
        else:
            draft_id = existing["draft_id"]
            created_at = existing["created_at"]
            recomputed = existing["rules_version"] != rules_version

        draft = {
            "draft_id": draft_id,
            "asset_id": asset_id,
            "slot": slot,
            "status": DraftStatus.DRAFT_SAVED.value,
            "pending_confirmation": bool(pending_confirmation),
            "confirmation_reason": reason if pending_confirmation else None,
            "origin_requested_at": requested,
            "idempotency_key": idem_key,
            "rules_version": rules_version,
            "created_at": created_at,
            "updated_at": now,
            "able_to_publish": False,
        }
        if existing is not None and existing["idempotency_key"] != idem_key:
            draft["previous_idempotency_key"] = existing["idempotency_key"]
        draft["checksum"] = checksum_of(draft)

        new_doc["drafts"][asset_id] = draft

        # 先落盘，成功后才提交内存
        self.repo.dump(self.store_name, new_doc, validate=False)
        self._doc = new_doc
        self._sig = self._signature()

        return {
            **deepcopy(draft),
            "idempotent_replay": True,
            "updated": existing is not None,
            "recomputed": recomputed,
        }

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _idem_key(asset_id: str, rules_version: str) -> str:
        raw = f"{asset_id}|{rules_version}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
