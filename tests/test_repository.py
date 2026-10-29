"""T2 验收：Repository 原子写与 schema 校验。

通过标准（docs/06-task-plan.md T2）：
- 写入后立即读回一致
- 数据文件缺失 / 损坏时 fail-fast，抛 DataCorruptError
- 非法对象不会落盘（先校验后写）
- 写入失败时不残留临时文件，且原文件保持不变
- 文件名非法（路径穿越）被拒绝

运行：python -m unittest discover -s tests -t . -v
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.repository import DataCorruptError, Repository


def sample_assets_doc(asset_id: str = "M01") -> dict:
    return {
        "version": "2026-10-29",
        "assets": [
            {
                "asset_id": asset_id,
                "title": "测试素材",
                "channel": "douyin",
                "requested_publish_at": "2026-11-05T18:00",
                "hard_requirement": True,
                "metadata": {
                    "has_cover": True,
                    "has_body": True,
                    "has_authorization": True,
                    "duration_sec": 30,
                },
                "content_flags": [],
            }
        ],
    }


class TestRepositoryRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = Repository(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_dump_then_load_is_identical(self):
        doc = sample_assets_doc()
        self.repo.dump("assets", doc)
        self.assertEqual(self.repo.load("assets"), doc)

    def test_second_dump_overwrites(self):
        self.repo.dump("assets", sample_assets_doc("M01"))
        self.repo.dump("assets", sample_assets_doc("M02"))
        self.assertEqual(self.repo.load("assets")["assets"][0]["asset_id"], "M02")

    def test_no_temp_file_left_after_success(self):
        self.repo.dump("assets", sample_assets_doc())
        leftovers = list(self.root.glob("*.tmp"))
        self.assertEqual(leftovers, [], f"残留临时文件: {leftovers}")

    def test_creates_root_dir_if_absent(self):
        nested = self.root / "nested" / "data"
        repo = Repository(nested)
        repo.dump("assets", sample_assets_doc())
        self.assertTrue((nested / "assets.json").is_file())


class TestRepositoryFailFast(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = Repository(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_raises(self):
        with self.assertRaises(DataCorruptError):
            self.repo.load("assets")

    def test_corrupted_json_raises(self):
        (self.root / "assets.json").write_text("{ not json", encoding="utf-8")
        with self.assertRaises(DataCorruptError):
            self.repo.load("assets")

    def test_non_object_top_level_raises(self):
        (self.root / "assets.json").write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(DataCorruptError):
            self.repo.load("assets")

    def test_does_not_silently_recover(self):
        """静默恢复会制造"看起来成功"的假象，必须直接报错。"""
        (self.root / "assets.json").write_text("", encoding="utf-8")
        with self.assertRaises(DataCorruptError):
            self.repo.load("assets")


class TestRepositorySchema(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = Repository(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_asset_field_rejected_before_write(self):
        doc = sample_assets_doc()
        del doc["assets"][0]["hard_requirement"]
        with self.assertRaises(DataCorruptError):
            self.repo.dump("assets", doc)
        # 校验失败时不得落盘
        self.assertFalse((self.root / "assets.json").exists())

    def test_invalid_asset_id_rejected(self):
        doc = sample_assets_doc("../../etc/passwd")
        with self.assertRaises(DataCorruptError):
            self.repo.dump("assets", doc)

    def test_duplicate_asset_id_rejected(self):
        doc = sample_assets_doc("M01")
        doc["assets"].append(dict(doc["assets"][0]))
        with self.assertRaises(DataCorruptError):
            self.repo.dump("assets", doc)

    def test_non_boolean_hard_requirement_rejected(self):
        doc = sample_assets_doc()
        doc["assets"][0]["hard_requirement"] = "true"  # 字符串不是布尔
        with self.assertRaises(DataCorruptError):
            self.repo.dump("assets", doc)

    def test_occupied_slot_requires_asset_id(self):
        doc = {
            "version": "2026-10-29",
            "channels": {"douyin": {"2026-11-05": {"18:00": {"status": "occupied"}}}},
        }
        with self.assertRaises(DataCorruptError):
            self.repo.dump("schedule", doc)

    def test_bad_rule_level_rejected(self):
        doc = {
            "version": "2026-10-01",
            "rules": [{"rule_id": "R9", "desc": "x", "level": "critical"}],
        }
        with self.assertRaises(DataCorruptError):
            self.repo.dump("rules", doc)

    def test_unknown_name_skips_schema_validation(self):
        # drafts 等运行时文件的 schema 由 T6 负责，此处只保证能读写
        self.repo.dump("drafts", {"anything": [1, 2, 3]})
        self.assertEqual(self.repo.load("drafts"), {"anything": [1, 2, 3]})


class TestRepositorySafety(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = Repository(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_path_traversal_name_rejected(self):
        for bad in ("../secret", "a/b", "assets.json", "", "Assets", "a b"):
            with self.subTest(name=bad):
                with self.assertRaises(DataCorruptError):
                    self.repo.path(bad)

    def test_failed_write_preserves_original(self):
        self.repo.dump("assets", sample_assets_doc("M01"))
        before = (self.root / "assets.json").read_text(encoding="utf-8")

        with self.assertRaises(TypeError):
            self.repo.dump("assets", {"assets": object()}, validate=False)

        after = (self.root / "assets.json").read_text(encoding="utf-8")
        self.assertEqual(before, after, "写入失败后原文件被破坏")
        self.assertEqual(list(self.root.glob("*.tmp")), [], "写入失败后残留临时文件")


class TestRepositoryRealData(unittest.TestCase):
    def test_load_all_real_data(self):
        repo = Repository()
        data = repo.load_all()
        self.assertEqual(
            sorted(data),
            ["asset_extra", "assets", "failure_config", "rules", "schedule"],
        )
        self.assertEqual(len(data["assets"]["assets"]), 7)
        self.assertTrue(data["rules"]["version"])

    def test_real_assets_json_is_well_formed(self):
        repo = Repository()
        doc = repo.load("assets")
        ids = [a["asset_id"] for a in doc["assets"]]
        self.assertIn("M01", ids)
        self.assertEqual(json.dumps(doc)[:1], "{")


if __name__ == "__main__":
    unittest.main()
