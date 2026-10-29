"""素材读取与字段完整性判定。

关键约束（docs/02-solution.md §6）：
- ``get`` 返回深拷贝，调用方修改不会污染内存中的原始数据。
- ``merge_extra`` 只补齐空字段，**不覆盖已有值、不新增字段**——
  补查是"补缺"，不是"改写"。
- ``missing_fields`` 是 R1 规则与"信息缺失"验收点的共同数据源。
"""

from __future__ import annotations

from copy import deepcopy

from ..enums import ErrorCode
from ..errors import ServiceError
from ..repository import Repository

# 布尔型必备要素
REQUIRED_BOOL_FLAGS = ("has_cover", "has_body", "has_authorization")
# 数值型必备要素：必须为正数
REQUIRED_DURATION = "duration_sec"


class AssetService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self._doc = repo.load("assets")
        self._extra = repo.load("asset_extra")
        self._by_id = {a["asset_id"]: a for a in self._doc["assets"]}

    # -- 查询 ---------------------------------------------------------- #

    def asset_ids(self) -> list[str]:
        return list(self._by_id)

    def list_pending(
        self, channel: str | None = None, only: set[str] | None = None
    ) -> list[dict]:
        """待处理素材概览。不含正文，避免把大字段塞进模型上下文。

        ``only`` 用于限定"本批次"要处理的素材范围（命令行 --case 使用）。
        """
        fields = (
            "asset_id",
            "title",
            "channel",
            "requested_publish_at",
            "hard_requirement",
        )
        return [
            {key: asset[key] for key in fields}
            for asset in self._doc["assets"]
            if (channel is None or asset["channel"] == channel)
            and (only is None or asset["asset_id"] in only)
        ]

    def get(self, asset_id: str) -> dict:
        asset = self._by_id.get(asset_id)
        if asset is None:
            raise ServiceError(
                ErrorCode.E_NOT_FOUND,
                f"素材 {asset_id} 不存在",
                retryable=False,
            )
        return deepcopy(asset)

    # -- 补查 ---------------------------------------------------------- #

    def merge_extra(self, asset: dict) -> dict:
        """把 ``data/asset_extra.json`` 的补充材料合并进素材。

        规则：只填 metadata 中原本为空/假的字段；已有真值不覆盖；不新增字段。
        """
        patch = self._extra.get("extra", {}).get(asset["asset_id"], {})
        merged = deepcopy(asset)
        metadata = merged.setdefault("metadata", {})
        for key, value in patch.items():
            if key in metadata and not metadata[key]:
                metadata[key] = value
        return merged

    # -- 完整性 -------------------------------------------------------- #

    def missing_fields(self, asset: dict) -> list[str]:
        metadata = asset.get("metadata", {})
        missing: list[str] = []
        for flag in REQUIRED_BOOL_FLAGS:
            if not metadata.get(flag):
                missing.append(flag)
        duration = metadata.get(REQUIRED_DURATION)
        if not isinstance(duration, (int, float)) or duration <= 0:
            missing.append(REQUIRED_DURATION)
        return missing

    def detail(self, asset_id: str) -> dict:
        """完整素材视图：已合并补充材料，并附带缺失字段与完整性结论。"""
        merged = self.merge_extra(self.get(asset_id))
        missing = self.missing_fields(merged)
        return {
            **merged,
            "missing_fields": missing,
            "complete": not missing,
        }
