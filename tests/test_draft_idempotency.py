"""T6 验收：DraftService 幂等与边界。

核心不变量：**一个素材永远只有一份草稿**。
本文件覆盖：纯重复、改期复用、重启、写失败回滚、篡改检测、硬性约束、规则版本变化、
输入非法、以及"重复提交不写盘"的行为保证。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.enums import DraftStatus, ErrorCode
from src.errors import ServiceError
from src.repository import DataCorruptError, Repository
from src.services import DraftService

LV = "2026-10-01"


def asset(asset_id="M01", ts="2026-11-05T18:00", hard=True):
    return {
        "asset_id": asset_id,
        "title": "测试素材",
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


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = Repository(self.root)
        self.svc = DraftService(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def read_store(self) -> dict:
        return json.loads((self.root / "drafts.json").read_text(encoding="utf-8"))

    def write_store(self, doc: dict) -> None:
        (self.root / "drafts.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8"
        )


class TestBasicIdempotency(_Base):
    def test_first_save_creates_draft(self):
        result = self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        self.assertEqual(result["draft_id"], "D-001")
        self.assertEqual(result["status"], DraftStatus.DRAFT_SAVED.value)
        self.assertFalse(result["able_to_publish"])
        self.assertFalse(result["updated"])

    def test_exact_repeat_returns_same_draft(self):
        first = self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        second = self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertFalse(second["updated"])
        self.assertEqual(self.svc.count(), 1)

    def test_exact_repeat_does_not_touch_disk(self):
        self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        with mock.patch.object(
            Repository, "dump", side_effect=AssertionError("纯重复不应写盘")
        ):
            result = self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        self.assertTrue(result["idempotent_replay"])

    def test_repeat_ten_times_still_one_draft(self):
        ids = {
            self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)["draft_id"]
            for _ in range(10)
        }
        self.assertEqual(ids, {"D-001"})
        self.assertEqual(self.svc.count(), 1)

    def test_alternative_slot_reuses_same_draft(self):
        first = self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        # 用户要求 18:00 但被占用，改期 19:00 并标记待确认
        second = self.svc.save(
            asset(),
            "2026-11-05T19:00",
            rules_version=LV,
            pending_confirmation=True,
            reason="18:00 已被占用，建议 19:00",
        )
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertTrue(second["updated"])
        self.assertEqual(second["slot"], "2026-11-05T19:00")
        self.assertTrue(second["pending_confirmation"])
        self.assertEqual(self.svc.count(), 1, "改期不得产生第二份草稿")

    def test_switching_slot_back_and_forth_keeps_one_draft(self):
        self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        for slot in ("2026-11-05T19:00", "2026-11-05T20:00", "2026-11-05T19:00"):
            self.svc.save(
                asset(),
                slot,
                rules_version=LV,
                pending_confirmation=True,
                reason="改期",
            )
        self.assertEqual(self.svc.count(), 1)
        self.assertEqual(self.svc.get("M01")["slot"], "2026-11-05T19:00")

    def test_different_assets_get_different_drafts(self):
        a = self.svc.save(asset("M01"), "2026-11-05T18:00", rules_version=LV)
        b = self.svc.save(asset("M02", "2026-11-05T20:00"), "2026-11-05T20:00", rules_version=LV)
        self.assertNotEqual(a["draft_id"], b["draft_id"])
        self.assertEqual({a["draft_id"], b["draft_id"]}, {"D-001", "D-002"})
        self.assertEqual(self.svc.count(), 2)

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.svc.get("M99"))

    def test_all_drafts_sorted_by_asset_id(self):
        self.svc.save(asset("M02", "2026-11-05T20:00"), "2026-11-05T20:00", rules_version=LV)
        self.svc.save(asset("M01"), "2026-11-05T18:00", rules_version=LV)
        self.assertEqual([d["asset_id"] for d in self.svc.all_drafts()], ["M01", "M02"])

    def test_slot_taken(self):
        self.svc.save(asset("M01"), "2026-11-05T18:00", rules_version=LV)
        self.assertEqual(self.svc.slot_taken("2026-11-05T18:00"), "M01")
        self.assertIsNone(self.svc.slot_taken("2026-11-05T19:00"))

    def test_all_drafts_returns_copies(self):
        self.svc.save(asset("M01"), "2026-11-05T18:00", rules_version=LV)
        drafts = self.svc.all_drafts()
        drafts[0]["slot"] = "tampered"
        self.assertEqual(self.svc.get("M01")["slot"], "2026-11-05T18:00")


class TestHardRequirementGuards(_Base):
    def test_hard_slot_change_without_confirmation_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            self.svc.save(asset(hard=True), "2026-11-05T19:00", rules_version=LV)
        self.assertEqual(ctx.exception.code, ErrorCode.E_INTEGRITY)
        self.assertEqual(self.svc.count(), 0, "被拒绝的保存不得留下草稿")

    def test_confirmation_without_reason_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            self.svc.save(
                asset(hard=True), "2026-11-05T19:00", rules_version=LV,
                pending_confirmation=True,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.E_INTEGRITY)

    def test_blank_reason_rejected(self):
        with self.assertRaises(ServiceError):
            self.svc.save(
                asset(hard=True), "2026-11-05T19:00", rules_version=LV,
                pending_confirmation=True, reason="   ",
            )

    def test_hard_slot_change_with_confirmation_accepted(self):
        result = self.svc.save(
            asset(hard=True), "2026-11-05T19:00", rules_version=LV,
            pending_confirmation=True, reason="18:00 被占用",
        )
        self.assertTrue(result["pending_confirmation"])
        self.assertEqual(result["confirmation_reason"], "18:00 被占用")

    def test_soft_asset_may_change_without_confirmation(self):
        result = self.svc.save(asset(hard=False), "2026-11-05T19:00", rules_version=LV)
        self.assertFalse(result["pending_confirmation"])

    def test_able_to_publish_true_is_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            self.svc.save(
                asset(), "2026-11-05T18:00", rules_version=LV, able_to_publish=True
            )
        self.assertEqual(ctx.exception.code, ErrorCode.E_INTEGRITY)
        self.assertEqual(self.svc.count(), 0)

    def test_output_never_claims_publishable(self):
        result = self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        self.assertIs(result["able_to_publish"], False)
        self.assertNotIn("published", json.dumps(result))


class TestPersistence(_Base):
    def test_restart_keeps_same_draft(self):
        first = self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        restarted = DraftService(self.repo)
        second = restarted.save(asset(), "2026-11-05T18:00", rules_version=LV)
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(restarted.count(), 1)

    def test_sequence_continues_after_restart(self):
        self.svc.save(asset("M01"), "2026-11-05T18:00", rules_version=LV)
        restarted = DraftService(self.repo)
        second = restarted.save(
            asset("M02", "2026-11-05T20:00"), "2026-11-05T20:00", rules_version=LV
        )
        self.assertEqual(second["draft_id"], "D-002", "重启后序号不得回退或复用")

    def test_restart_then_repeat_does_not_duplicate(self):
        self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        for _ in range(5):
            DraftService(self.repo).save(asset(), "2026-11-05T18:00", rules_version=LV)
        self.assertEqual(self.svc.count(), 1)

    def test_corrupted_store_fails_fast(self):
        (self.root / "drafts.json").write_text("{ broken", encoding="utf-8")
        with self.assertRaises(DataCorruptError):
            DraftService(self.repo)

    def test_missing_next_seq_fails_fast(self):
        (self.root / "drafts.json").write_text(
            json.dumps({"drafts": {}}), encoding="utf-8"
        )
        with self.assertRaises(DataCorruptError):
            DraftService(self.repo)


class TestTamperDetection(_Base):
    def setUp(self):
        super().setUp()
        self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)

    def _assert_tamper_detected(self):
        with self.assertRaises(DataCorruptError):
            DraftService(self.repo)

    def test_tampered_slot_detected(self):
        doc = self.read_store()
        doc["drafts"]["M01"]["slot"] = "2026-11-05T23:59"
        self.write_store(doc)
        self._assert_tamper_detected()

    def test_tampered_able_to_publish_detected(self):
        doc = self.read_store()
        doc["drafts"]["M01"]["able_to_publish"] = True
        self.write_store(doc)
        self._assert_tamper_detected()

    def test_tampered_draft_id_detected(self):
        doc = self.read_store()
        doc["drafts"]["M01"]["draft_id"] = "D-999"
        self.write_store(doc)
        self._assert_tamper_detected()

    def test_removed_field_detected(self):
        doc = self.read_store()
        del doc["drafts"]["M01"]["checksum"]
        self.write_store(doc)
        self._assert_tamper_detected()

    def test_extra_field_detected(self):
        doc = self.read_store()
        doc["drafts"]["M01"]["injected"] = "x"
        self.write_store(doc)
        self._assert_tamper_detected()

    def test_duplicated_draft_cannot_be_smuggled_in(self):
        """即使把两份草稿焊进同一个 key，checksum 也会露馅。"""
        doc = self.read_store()
        clone = dict(doc["drafts"]["M01"])
        clone["draft_id"] = "D-777"
        doc["drafts"]["M01"] = clone
        self.write_store(doc)
        self._assert_tamper_detected()


class TestWriteFailure(_Base):
    def test_failed_write_leaves_no_record(self):
        with mock.patch.object(Repository, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        self.assertIsNone(self.svc.get("M01"))
        self.assertEqual(self.svc.count(), 0)

    def test_failed_write_does_not_consume_draft_id(self):
        with mock.patch.object(Repository, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.svc.save(asset("M01"), "2026-11-05T18:00", rules_version=LV)
        result = self.svc.save(asset("M01"), "2026-11-05T18:00", rules_version=LV)
        self.assertEqual(result["draft_id"], "D-001", "失败的写入不应消耗序号")

    def test_retry_after_failure_succeeds(self):
        with mock.patch.object(Repository, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        result = self.svc.save(asset(), "2026-11-05T18:00", rules_version=LV)
        self.assertEqual(result["status"], DraftStatus.DRAFT_SAVED.value)
        self.assertEqual(self.svc.count(), 1)


class TestRulesVersionChange(_Base):
    def test_rules_change_recomputes_same_draft(self):
        first = self.svc.save(asset(), "2026-11-05T18:00", rules_version="v1")
        second = self.svc.save(asset(), "2026-11-05T18:00", rules_version="v2")
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertTrue(second["recomputed"])
        self.assertNotEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertEqual(second["previous_idempotency_key"], first["idempotency_key"])
        self.assertEqual(self.svc.count(), 1, "规则版本变化不得产生第二份草稿")

    def test_origin_request_rewrite_rejected(self):
        self.svc.save(asset(ts="2026-11-05T18:00"), "2026-11-05T18:00", rules_version=LV)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.save(asset(ts="2026-11-05T19:00"), "2026-11-05T19:00", rules_version=LV)
        self.assertEqual(ctx.exception.code, ErrorCode.E_INTEGRITY)


class TestMultiInstance(_Base):
    """多个实例共享同一草稿文件时的丢更新 / 编号重用问题。"""

    TS = "2026-11-05T18:00"

    def test_stale_instance_does_not_lose_other_drafts(self):
        a = self.svc
        a.save(asset("M01"), self.TS, rules_version=LV)
        b = DraftService(self.repo)  # B 的快照里目前只有 M01
        a.save(asset("M02"), self.TS, rules_version=LV)
        b.save(asset("M03"), self.TS, rules_version=LV)

        on_disk = json.loads((self.root / "drafts.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(on_disk["drafts"]), ["M01", "M02", "M03"])

    def test_stale_instance_does_not_reuse_draft_id(self):
        a = self.svc
        first = a.save(asset("M01"), self.TS, rules_version=LV)
        b = DraftService(self.repo)
        second = a.save(asset("M02"), self.TS, rules_version=LV)
        third = b.save(asset("M03"), self.TS, rules_version=LV)

        ids = [first["draft_id"], second["draft_id"], third["draft_id"]]
        self.assertEqual(ids, ["D-001", "D-002", "D-003"])
        self.assertEqual(len(set(ids)), 3, "draft_id 不得被复用")

    def test_ten_instances_produce_ten_unique_drafts(self):
        ids = set()
        for index in range(10):
            svc = DraftService(self.repo)
            ids.add(
                svc.save(asset(f"M{index:02d}"), self.TS, rules_version=LV)["draft_id"]
            )
        self.assertEqual(len(ids), 10)
        self.assertEqual(DraftService(self.repo).count(), 10)

    def test_stale_instance_still_replays_existing_draft(self):
        a = self.svc
        first = a.save(asset("M01"), self.TS, rules_version=LV)
        b = DraftService(self.repo)
        a.save(asset("M02"), self.TS, rules_version=LV)  # 让 B 的快照变旧
        replay = b.save(asset("M01"), self.TS, rules_version=LV)
        self.assertEqual(replay["draft_id"], first["draft_id"])
        self.assertEqual(DraftService(self.repo).count(), 2)


class TestInputValidation(_Base):
    def test_invalid_asset_id_rejected(self):
        for bad in ("../../x", "m01", "M1", "", None):
            with self.subTest(asset_id=bad):
                with self.assertRaises(ServiceError) as ctx:
                    self.svc.save({"asset_id": bad, "requested_publish_at": "2026-11-05T18:00"},
                                  "2026-11-05T18:00", rules_version=LV)
                self.assertEqual(ctx.exception.code, ErrorCode.E_SCHEMA_INVALID)

    def test_invalid_slot_rejected(self):
        for bad in ("2026-11-05 18:00", "18:00", "2026-11-05T25:00", ""):
            with self.subTest(slot=bad):
                with self.assertRaises(ServiceError) as ctx:
                    self.svc.save(asset(), bad, rules_version=LV)
                self.assertEqual(ctx.exception.code, ErrorCode.E_SCHEMA_INVALID)
        self.assertEqual(self.svc.count(), 0)

    def test_invalid_requested_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            self.svc.save({"asset_id": "M01", "requested_publish_at": "明天"},
                          "2026-11-05T18:00", rules_version=LV)
        self.assertEqual(ctx.exception.code, ErrorCode.E_SCHEMA_INVALID)


if __name__ == "__main__":
    unittest.main()
