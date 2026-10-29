"""T12 验收（上）：HonestyGate。

诚实层的行为是"发现即报错"，不是"发现即改写"。
下面每条都在验证：错的内容会被拒绝，对的内容会通过。
"""

import unittest
from types import SimpleNamespace

from src.honesty import (
    HonestyViolation,
    find_forbidden,
    mask_sensitive,
    status_label,
    verify_text,
)


def make_result(assets):
    return SimpleNamespace(assets=assets)


PENDING_ASSET = {
    "asset_id": "M01",
    "review_status": "passed",
    "draft_status": "draft_saved",
    "draft_id": "D-001",
    "pending_confirmation_items": ["18:00 改为 19:00，需用户确认"],
    "able_to_publish": False,
}
CLEAN_ASSET = {
    "asset_id": "M02",
    "review_status": "passed",
    "draft_status": "draft_saved",
    "draft_id": "D-002",
    "pending_confirmation_items": [],
    "able_to_publish": False,
}
BLOCKED_ASSET = {
    "asset_id": "M04",
    "review_status": "blocked",
    "draft_status": "blocked",
    "draft_id": None,
    "pending_confirmation_items": [],
    "able_to_publish": False,
}


class TestForbiddenWords(unittest.TestCase):
    def test_detects_each_forbidden_phrase(self):
        for text in ("素材已发布", "发布成功", "已上线", "published ok", "the site went live"):
            with self.subTest(text=text):
                self.assertTrue(find_forbidden(text), f"未识别禁词：{text}")

    def test_allows_legitimate_wording(self):
        text = "草稿已保存，待确认发布时间。发布时间由 18:00 调整为 19:00。"
        self.assertEqual(find_forbidden(text), [])

    def test_verify_rejects_forbidden_words(self):
        with self.assertRaises(HonestyViolation):
            verify_text("M01 已发布", make_result([CLEAN_ASSET]))


class TestConsistency(unittest.TestCase):
    def test_pending_requires_待确认_label(self):
        result = make_result([PENDING_ASSET])
        with self.assertRaises(HonestyViolation):
            verify_text("M01 草稿已保存", result)
        verify_text("M01 草稿已保存，待确认", result)

    def test_saved_requires_saved_wording(self):
        result = make_result([CLEAN_ASSET])
        with self.assertRaises(HonestyViolation):
            verify_text("M02 处理完毕", result)
        verify_text("M02 草稿已保存", result)

    def test_claims_saved_when_nothing_saved(self):
        result = make_result([BLOCKED_ASSET])
        with self.assertRaises(HonestyViolation):
            verify_text("M04 草稿已保存", result)
        verify_text("M04 已阻断", result)

    def test_publishable_asset_is_rejected(self):
        broken = dict(CLEAN_ASSET, able_to_publish=True)
        with self.assertRaises(HonestyViolation):
            verify_text("M02 草稿已保存", make_result([broken]))


class TestMasking(unittest.TestCase):
    def test_masks_phone(self):
        self.assertEqual(mask_sensitive("联系 13800138000"), "联系 138****8000")

    def test_masks_email(self):
        masked = mask_sensitive("邮箱 zhangsan@example.com")
        self.assertIn("***@example.com", masked)
        self.assertNotIn("zhangsan", masked)

    def test_leaves_normal_text_alone(self):
        text = "素材 M01 建议时段 2026-11-05T19:00"
        self.assertEqual(mask_sensitive(text), text)


class TestStatusLabel(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(status_label("passed", "draft_saved"), "草稿已保存")
        self.assertEqual(status_label("passed", "failed"), "保存失败")
        self.assertEqual(status_label("blocked", "blocked"), "已阻断")
        self.assertEqual(status_label("needs_info", "not_attempted"), "待补资料")
        self.assertEqual(status_label("passed", "not_attempted"), "未处理")

    def test_labels_never_contain_publish_words(self):
        combos = [
            ("passed", "draft_saved"),
            ("passed", "failed"),
            ("passed", "blocked"),
            ("passed", "not_attempted"),
            ("blocked", "blocked"),
            ("needs_info", "not_attempted"),
        ]
        for review, draft in combos:
            with self.subTest(review=review, draft=draft):
                self.assertEqual(find_forbidden(status_label(review, draft)), [])


if __name__ == "__main__":
    unittest.main()
