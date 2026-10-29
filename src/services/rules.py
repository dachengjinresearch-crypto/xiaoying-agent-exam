"""规则匹配引擎。

设计要点：
- **数据驱动**：规则内容与等级来自 ``data/rules.json``，代码只实现"检查器"，
  新增规则优先改数据而不是改分支。
- **返回全部命中项**，不只第一条——评审需要看到风险的全貌，
  模型也需要完整依据才能决定阻断还是继续。
- 同槽冲突不计入 R3（连发间隔），那是 ``ScheduleService`` 的职责，
  避免同一问题被两条规则重复报告。
"""

from __future__ import annotations

from ..repository import Repository
from ..utils import split_ts, to_minutes
from .asset import AssetService

# check 名 → 实例方法名
_CHECKERS: dict[str, str] = {
    "metadata_completeness": "_check_metadata",
    "content_flags": "_check_flags",
    "channel_interval": "_check_channel_interval",
    "publish_window": "_check_publish_window",
}

_LEVEL_RANK = {"none": 0, "warn": 1, "block": 2}


class RuleEngine:
    def __init__(self, repo: Repository, assets: AssetService | None = None) -> None:
        self._doc = repo.load("rules")
        self._schedule_doc = repo.load("schedule")
        self.version: str = self._doc["version"]
        self.assets = assets or AssetService(repo)

    # -- 对外 ---------------------------------------------------------- #

    def check(self, asset_id: str) -> dict:
        asset = self.assets.detail(asset_id)
        hits: list[dict] = []
        for rule in self._doc["rules"]:
            method_name = _CHECKERS.get(rule.get("check", ""))
            if method_name is None:
                continue
            reason = getattr(self, method_name)(asset, rule)
            if reason:
                hits.append(
                    {
                        "rule_id": rule["rule_id"],
                        "level": rule["level"],
                        "reason": reason,
                    }
                )
        return {
            "asset_id": asset_id,
            "risk_level": self._aggregate(hits),
            "hits": hits,
            "rules_version": self.version,
        }

    @staticmethod
    def is_blocked(result: dict) -> bool:
        return result["risk_level"] == "block"

    # -- 检查器 -------------------------------------------------------- #

    def _check_metadata(self, asset: dict, rule: dict) -> str | None:
        missing = asset.get("missing_fields") or []
        if missing:
            return f"缺少要素：{', '.join(missing)}"
        return None

    def _check_flags(self, asset: dict, rule: dict) -> str | None:
        matched = sorted(set(asset.get("content_flags", [])) & set(rule.get("match_flags", [])))
        if matched:
            return f"命中未核实内容特征：{', '.join(matched)}"
        return None

    def _check_channel_interval(self, asset: dict, rule: dict) -> str | None:
        date, clock = split_ts(asset["requested_publish_at"])
        interval = int(rule.get("interval_minutes", 30))
        slots = self._schedule_doc.get("channels", {}).get(asset["channel"], {}).get(date, {})
        target = to_minutes(clock)
        for other_clock, entry in sorted(slots.items()):
            if entry.get("status") != "occupied":
                continue
            if other_clock == clock:
                # 同一时段的冲突由 ScheduleService 判定
                continue
            if abs(to_minutes(other_clock) - target) < interval:
                return (
                    f"{other_clock} 已有 {entry.get('asset_id')} 排期，"
                    f"间隔小于 {interval} 分钟"
                )
        return None

    def _check_publish_window(self, asset: dict, rule: dict) -> str | None:
        _, clock = split_ts(asset["requested_publish_at"])
        start, end = rule.get("window", ("09:00", "22:00"))
        if not (to_minutes(start) <= to_minutes(clock) < to_minutes(end)):
            return f"{clock} 不在允许发布时段 {start}-{end} 内"
        return None

    # -- 聚合 ---------------------------------------------------------- #

    @staticmethod
    def _aggregate(hits: list[dict]) -> str:
        level = "none"
        for hit in hits:
            if _LEVEL_RANK[hit["level"]] > _LEVEL_RANK[level]:
                level = hit["level"]
        return level
