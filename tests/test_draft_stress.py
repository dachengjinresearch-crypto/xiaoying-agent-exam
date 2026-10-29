"""T6 压力测试：幂等在重复、重启与批量场景下是否保持。

这些用例不验证"功能"，而是验证**不变量在极端次数下不塌**：
- 纯重复 N 次，草稿数恒为 1，且不写盘
- 反复改期 M 次，草稿数恒为 1，最终时段正确
- 批量素材互不串号，序号不重复不跳号
- 反复重启后仍只有一份草稿
- 文件大小不随重复次数增长
"""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from src.enums import ErrorCode
from src.errors import ServiceError
from src.repository import Repository
from src.services import DraftService
from src.services import draft as draft_module

LV = "2026-10-01"


def asset(asset_id="M01", ts="2026-11-05T18:00", hard=False):
    return {
        "asset_id": asset_id,
        "title": "压测素材",
        "channel": "douyin",
        "requested_publish_at": ts,
        "hard_requirement": hard,
        "metadata": {
            "has_cover": True,
            "has_body": True,
            "has_authorization": True,
            "duration_sec": 30,
        },
        "content_flags": [],
    }


class TestStress(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = Repository(self.root)
        self.svc = DraftService(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    # -- 重复次数 ------------------------------------------------------ #

    def test_500_repeats_produce_one_draft(self):
        started = time.perf_counter()
        ids = set()
        for _ in range(500):
            ids.add(
                self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)["draft_id"]
            )
        elapsed = time.perf_counter() - started

        self.assertEqual(ids, {"D-001"})
        self.assertEqual(self.svc.count(), 1)
        self.assertLess(elapsed, 5.0, "纯重复不应触发磁盘写入，耗时异常")

    def test_repeat_count_does_not_grow_file(self):
        self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        size_after_first = (self.root / "drafts.json").stat().st_size
        for _ in range(300):
            self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        size_after_many = (self.root / "drafts.json").stat().st_size
        self.assertEqual(size_after_first, size_after_many, "重复提交不得膨胀草稿文件")

    def test_200_slot_flips_keep_one_draft(self):
        slots = ["2026-11-05T19:00", "2026-11-05T20:00"]
        started = time.perf_counter()
        for index in range(200):
            self.svc.save(
                asset(),
                slots[index % 2],
                rules_version=LV,
                pending_confirmation=True,
                reason="压测改期",
            )
        elapsed = time.perf_counter() - started

        self.assertEqual(self.svc.count(), 1)
        self.assertEqual(self.svc.get("M01")["slot"], slots[199 % 2])
        self.assertLess(elapsed, 30.0, "200 次原子写耗时异常")

    # -- 批量 ---------------------------------------------------------- #

    def test_100_assets_produce_100_unique_drafts(self):
        for index in range(100):
            asset_id = f"M{index:02d}"
            self.svc.save(
                asset(asset_id, "2026-11-05T18:00"),
                "2026-11-05T18:00",
                rules_version=LV,
            )
        drafts = self.svc.all_drafts()
        self.assertEqual(len(drafts), 100)
        self.assertEqual(len({d["draft_id"] for d in drafts}), 100, "draft_id 必须唯一")
        self.assertEqual(drafts[0]["draft_id"], "D-001")
        self.assertEqual(drafts[-1]["draft_id"], "D-100")

    def test_batch_with_repeats_stays_consistent(self):
        for index in range(50):
            asset_id = f"M{index:02d}"
            for _ in range(4):
                self.svc.save(
                    asset(asset_id, "2026-11-05T18:00"),
                    "2026-11-05T18:00",
                    rules_version=LV,
                )
        self.assertEqual(self.svc.count(), 50)
        self.assertEqual(len({d["draft_id"] for d in self.svc.all_drafts()}), 50)

    def test_same_slot_for_many_assets_is_allowed_here(self):
        """跨素材时段唯一性不属于草稿层的职责，此处显式记录该边界。"""
        for index in range(5):
            self.svc.save(
                asset(f"M{index:02d}", "2026-11-05T18:00"),
                "2026-11-05T18:00",
                rules_version=LV,
            )
        self.assertEqual(self.svc.count(), 5)
        self.assertIsNotNone(self.svc.slot_taken("2026-11-05T18:00"))

    # -- 重启 ---------------------------------------------------------- #

    def test_30_restarts_keep_one_draft(self):
        first = self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        for _ in range(30):
            svc = DraftService(self.repo)
            result = svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
            self.assertEqual(result["draft_id"], first["draft_id"])
        self.assertEqual(DraftService(self.repo).count(), 1)

    def test_restart_after_slot_change_keeps_single_draft(self):
        self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        self.svc.save(
            asset(), "2026-11-05T19:00", rules_version=LV,
            pending_confirmation=True, reason="改期",
        )
        restarted = DraftService(self.repo)
        self.assertEqual(restarted.count(), 1)
        self.assertEqual(restarted.get("M01")["slot"], "2026-11-05T19:00")

    # -- 一致性 -------------------------------------------------------- #

    def test_memory_matches_disk_after_stress(self):
        for index in range(20):
            item = asset(f"M{index:02d}", "2026-11-05T18:00")
            self.svc.save(item, "2026-11-05T18:00", rules_version=LV)
            self.svc.save(item, "2026-11-05T19:00", rules_version=LV)

        on_disk = json.loads((self.root / "drafts.json").read_text(encoding="utf-8"))
        self.assertEqual(len(on_disk["drafts"]), self.svc.count())
        self.assertEqual(on_disk["next_seq"], self.svc.count() + 1)
        for asset_id, draft in on_disk["drafts"].items():
            self.assertEqual(self.svc.get(asset_id)["slot"], draft["slot"])

    def test_no_temp_files_after_stress(self):
        for index in range(30):
            self.svc.save(
                asset(f"M{index:02d}", "2026-11-05T18:00"),
                "2026-11-05T18:00",
                rules_version=LV,
            )
        self.assertEqual(list(self.root.glob("*.tmp")), [])


class TestConcurrencyAndLock(unittest.TestCase):
    TS = "2026-11-05T18:00"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = Repository(self.root)
        self.lock_path = DraftService(self.repo).lock_path()

    def tearDown(self):
        self._tmp.cleanup()

    def test_lock_lives_outside_data_dir(self):
        DraftService(self.repo).save(asset("M01"), self.TS, rules_version=LV)
        self.assertNotEqual(self.lock_path.parent, self.root)
        self.assertFalse((self.root / ".drafts.lock").exists(), "数据目录不应被锁文件污染")

    def test_parallel_threads_do_not_lose_updates(self):
        threads_count, per_thread = 4, 5
        errors: list[str] = []

        def worker(tid: int) -> None:
            svc = DraftService(self.repo)
            for index in range(per_thread):
                try:
                    svc.save(
                        asset(f"M{tid * per_thread + index:02d}"),
                        self.TS,
                        rules_version=LV,
                    )
                except Exception as exc:  # pragma: no cover - 失败时才走到
                    errors.append(f"{tid}-{index}: {exc}")

        threads = [
            threading.Thread(target=worker, args=(tid,))
            for tid in range(threads_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        final = DraftService(self.repo)
        self.assertEqual(final.count(), threads_count * per_thread)
        ids = {d["draft_id"] for d in final.all_drafts()}
        self.assertEqual(len(ids), threads_count * per_thread, "draft_id 不得冲突")

    def test_lock_is_released_after_save(self):
        DraftService(self.repo).save(asset("M01"), self.TS, rules_version=LV)
        self.assertFalse(self.lock_path.exists(), "锁文件必须在写入结束后删除")

    def test_lock_is_released_when_write_fails(self):
        svc = DraftService(self.repo)
        with mock.patch.object(Repository, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                svc.save(asset("M01"), self.TS, rules_version=LV)
        self.assertFalse(self.lock_path.exists(), "异常路径也必须释放锁")

    def test_lock_timeout_returns_retryable_error(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text("held", encoding="utf-8")
        # 锁是新鲜的，不会被当成僵尸锁清理
        os.utime(self.lock_path, None)

        svc = DraftService(self.repo)
        with mock.patch.object(draft_module, "LOCK_TIMEOUT_S", 0.05), \
             mock.patch.object(draft_module, "LOCK_POLL_S", 0.005):
            with self.assertRaises(ServiceError) as ctx:
                svc.save(asset("M01"), self.TS, rules_version=LV)

        self.assertEqual(ctx.exception.code, ErrorCode.E_STORAGE_LOCK)
        self.assertTrue(ctx.exception.retryable, "锁冲突应当允许重试")

    def test_stale_lock_is_cleaned_up(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text("zombie", encoding="utf-8")
        old = time.time() - 120
        os.utime(self.lock_path, (old, old))

        result = DraftService(self.repo).save(asset("M01"), self.TS, rules_version=LV)
        self.assertEqual(result["draft_id"], "D-001")
        self.assertFalse(self.lock_path.exists())


if __name__ == "__main__":
    unittest.main()
