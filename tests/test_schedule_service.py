"""T5 验收：ScheduleService。

通过标准（docs/06-task-plan.md T5）：
- M01 场景 18:00 占用、19:00 可用且升序
- 未知渠道 / 未知日期 → E_NOT_FOUND 且不可重试
- 表中不存在的时段一律视为不可用（保守判定）
"""

import unittest

from src.enums import ErrorCode
from src.errors import ServiceError
from src.repository import Repository
from src.services import AssetService, ScheduleService


class TestScheduleServiceRealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Repository()
        cls.schedule = ScheduleService(cls.repo)
        cls.assets = AssetService(cls.repo)

    def test_slots_split_occupied_and_available(self):
        result = self.schedule.slots("douyin", "2026-11-05")
        self.assertEqual(result["occupied"], [{"slot": "18:00", "asset_id": "M00"}])
        self.assertEqual(result["available"], ["19:00", "20:00"])

    def test_slots_are_sorted_ascending(self):
        result = self.schedule.slots("douyin", "2026-11-06")
        self.assertEqual(result["available"], sorted(result["available"]))

    def test_is_free(self):
        self.assertFalse(self.schedule.is_free("douyin", "2026-11-05", "18:00"))
        self.assertTrue(self.schedule.is_free("douyin", "2026-11-05", "19:00"))

    def test_unknown_slot_is_treated_as_unavailable(self):
        # 表中没有 21:00，保守判定为不可用
        self.assertFalse(self.schedule.is_free("douyin", "2026-11-05", "21:00"))

    def test_is_conflict_for_m01(self):
        m01 = self.assets.get("M01")
        self.assertTrue(
            self.schedule.is_conflict(m01["channel"], m01["requested_publish_at"])
        )

    def test_is_not_conflict_for_m02(self):
        m02 = self.assets.get("M02")
        self.assertFalse(
            self.schedule.is_conflict(m02["channel"], m02["requested_publish_at"])
        )

    def test_find_alternatives_after_given_time(self):
        self.assertEqual(
            self.schedule.find_alternatives("douyin", "2026-11-05", after="18:00"),
            ["19:00", "20:00"],
        )

    def test_find_alternatives_with_limit(self):
        self.assertEqual(
            self.schedule.find_alternatives("douyin", "2026-11-05", limit=1), ["19:00"]
        )

    def test_slot_of_asset(self):
        m01 = self.assets.get("M01")
        self.assertEqual(self.schedule.slot_of(m01), ("2026-11-05", "18:00"))


class TestScheduleServiceMissing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schedule = ScheduleService(Repository())

    def test_unknown_channel_raises(self):
        with self.assertRaises(ServiceError) as ctx:
            self.schedule.slots("xiaohongshu", "2026-11-05")
        self.assertEqual(ctx.exception.code, ErrorCode.E_NOT_FOUND)
        self.assertFalse(ctx.exception.retryable)

    def test_unknown_date_raises(self):
        with self.assertRaises(ServiceError) as ctx:
            self.schedule.slots("douyin", "2026-12-31")
        self.assertEqual(ctx.exception.code, ErrorCode.E_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
