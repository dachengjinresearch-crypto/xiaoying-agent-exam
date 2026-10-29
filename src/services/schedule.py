"""时段查询与冲突判定。

约定：``is_free`` 对**排期表中不存在的时段**一律返回 ``False``。
宁可拒绝排期，也不要把素材塞进一个不知道是否被占用的时段——
这符合 P1「诚实优先」：不确定时倾向保守。
"""

from __future__ import annotations

from ..enums import ErrorCode
from ..errors import ServiceError
from ..repository import Repository
from ..utils import split_ts


class ScheduleService:
    def __init__(self, repo: Repository) -> None:
        self._doc = repo.load("schedule")
        self._channels = self._doc["channels"]

    # -- 基础 ---------------------------------------------------------- #

    def channels(self) -> list[str]:
        return list(self._channels)

    def _days(self, channel: str) -> dict:
        days = self._channels.get(channel)
        if days is None:
            raise ServiceError(
                ErrorCode.E_NOT_FOUND, f"渠道 {channel} 不在排期表中", retryable=False
            )
        return days

    def _slots(self, channel: str, date: str) -> dict:
        days = self._days(channel)
        slots = days.get(date)
        if slots is None:
            raise ServiceError(
                ErrorCode.E_NOT_FOUND,
                f"渠道 {channel} 在 {date} 没有排期表",
                retryable=False,
            )
        return slots

    # -- 查询 ---------------------------------------------------------- #

    def slots(self, channel: str, date: str) -> dict:
        """返回某渠道某日的占用与可用时段，均按时间升序。"""
        raw = self._slots(channel, date)
        occupied = [
            {"slot": clock, "asset_id": entry.get("asset_id")}
            for clock, entry in sorted(raw.items())
            if entry["status"] == "occupied"
        ]
        available = sorted(
            clock for clock, entry in raw.items() if entry["status"] == "free"
        )
        return {
            "channel": channel,
            "date": date,
            "occupied": occupied,
            "available": available,
        }

    def is_free(self, channel: str, date: str, clock: str) -> bool:
        raw = self._slots(channel, date)
        entry = raw.get(clock)
        return bool(entry and entry["status"] == "free")

    def is_conflict(self, channel: str, requested_publish_at: str) -> bool:
        """用户要求的时段是否已被占用（含"表中不存在"的保守判定）。"""
        date, clock = split_ts(requested_publish_at)
        return not self.is_free(channel, date, clock)

    def find_alternatives(
        self,
        channel: str,
        date: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> list[str]:
        """可用时段，可按 "晚于某时刻" 过滤（``after`` 用 HH:MM 字符串比较）。"""
        available = self.slots(channel, date)["available"]
        if after:
            available = [slot for slot in available if slot > after]
        return available[:limit] if limit is not None else available

    def slot_of(self, asset: dict) -> tuple[str, str]:
        """素材请求时段 → (date, clock)。"""
        return split_ts(asset["requested_publish_at"])
