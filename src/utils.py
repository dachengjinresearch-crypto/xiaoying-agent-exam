"""时间与文本小工具。"""

from __future__ import annotations


def split_ts(ts: str) -> tuple[str, str]:
    """把 ``2026-11-05T18:00`` 拆成 ``("2026-11-05", "18:00")``。"""
    date, sep, clock = ts.partition("T")
    if not sep or not date or not clock:
        raise ValueError(f"时间格式非法，应形如 2026-11-05T18:00：{ts!r}")
    return date, clock


def to_minutes(clock: str) -> int:
    """``"18:30"`` → ``1110``。用于区间比较。"""
    hour, sep, minute = clock.partition(":")
    if not sep:
        raise ValueError(f"时段格式非法，应形如 18:30：{clock!r}")
    return int(hour) * 60 + int(minute)


def format_slot(date: str, clock: str) -> str:
    return f"{date}T{clock}"
