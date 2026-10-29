"""T3 验收：AssetService。

通过标准（docs/06-task-plan.md T3）：
- M03 补查后仍缺字段且不可重试
- M07 补查后补齐
- 不存在的 ID 返回 E_NOT_FOUND
- get 返回深拷贝，外部修改不污染内存数据
- merge_extra 不覆盖已有真值、不新增字段
"""

import unittest

from src.enums import ErrorCode
from src.errors import ServiceError
from src.repository import Repository
from src.services import AssetService


class TestAssetServiceRealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = AssetService(Repository())

    def test_list_pending_returns_all_seven(self):
        rows = self.service.list_pending()
        self.assertEqual(len(rows), 7)
        self.assertEqual(
            sorted(r["asset_id"] for r in rows),
            ["M01", "M02", "M03", "M04", "M05", "M06", "M07"],
        )

    def test_list_pending_fields_are_minimal(self):
        row = self.service.list_pending()[0]
        self.assertEqual(
            set(row),
            {"asset_id", "title", "channel", "requested_publish_at", "hard_requirement"},
        )

    def test_list_pending_filter_by_channel(self):
        self.assertEqual(len(self.service.list_pending("douyin")), 7)
        self.assertEqual(self.service.list_pending("xiaohongshu"), [])

    def test_get_returns_deep_copy(self):
        asset = self.service.get("M01")
        asset["metadata"]["has_cover"] = False
        self.assertTrue(self.service.get("M01")["metadata"]["has_cover"])

    def test_get_unknown_asset_raises_not_found(self):
        with self.assertRaises(ServiceError) as ctx:
            self.service.get("M99")
        self.assertEqual(ctx.exception.code, ErrorCode.E_NOT_FOUND)
        self.assertFalse(ctx.exception.retryable, "不存在的素材不应允许重试")

    def test_m03_补查后仍缺失(self):
        detail = self.service.detail("M03")
        self.assertEqual(detail["missing_fields"], ["has_cover", "has_body"])
        self.assertFalse(detail["complete"])

    def test_m07_补查后补齐(self):
        detail = self.service.detail("M07")
        self.assertEqual(detail["missing_fields"], [])
        self.assertTrue(detail["complete"])

    def test_m01_字段齐全(self):
        self.assertEqual(self.service.detail("M01")["missing_fields"], [])

    def test_merge_extra_does_not_overwrite_truthy_values(self):
        asset = self.service.get("M01")
        merged = self.service.merge_extra(asset)
        self.assertTrue(merged["metadata"]["has_cover"])

    def test_merge_extra_does_not_add_unknown_keys(self):
        asset = self.service.get("M07")
        merged = self.service.merge_extra(asset)
        self.assertNotIn("has_bgm", merged["metadata"])


class TestAssetServiceEdgeCases(unittest.TestCase):
    def test_zero_duration_counts_as_missing(self):
        service = AssetService(Repository())
        asset = service.get("M01")
        asset["metadata"]["duration_sec"] = 0
        self.assertIn("duration_sec", service.missing_fields(asset))

    def test_missing_fields_accepts_plain_dict(self):
        service = AssetService(Repository())
        bare = {
            "asset_id": "M99",
            "metadata": {
                "has_cover": False,
                "has_body": True,
                "has_authorization": True,
                "duration_sec": 10,
            },
        }
        self.assertEqual(service.missing_fields(bare), ["has_cover"])


if __name__ == "__main__":
    unittest.main()
