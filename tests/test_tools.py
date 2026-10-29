"""T7 验收：ToolRegistry 与五个工具。

通过标准（docs/06-task-plan.md T7）：
- 5 个工具可直调，返回统一信封
- 故障注入：M05 保存返回 E_STORAGE_WRITE，且**不产生草稿**
- 规则阻断：M04 保存被拒，且不产生草稿
- 硬性要求：M01 改期未确认被拒；确认后成功并标记待确认
- 重复提交返回同一 draft_id
- 任何异常都不穿透到调用方
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.config import DATA_FILES
from src.enums import ErrorCode
from src.repository import Repository
from src.services import AssetService
from src.tools import ToolRegistry

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class ToolCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name in DATA_FILES:
            shutil.copy(DATA_DIR / f"{name}.json", self.root / f"{name}.json")
        self.repo = Repository(self.root)
        self.registry = ToolRegistry(self.repo)

    def tearDown(self):
        self._tmp.cleanup()


class TestEnvelopeContract(ToolCase):
    def test_success_envelope_shape(self):
        result = self.registry.call("get_asset_detail", {"asset_id": "M01"})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.error)
        self.assertIsInstance(result.data, dict)

    def test_failure_envelope_shape(self):
        result = self.registry.call("get_asset_detail", {"asset_id": "M99"})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.data)
        self.assertEqual(
            set(result.error), {"code", "message", "retryable", "retry_hint"}
        )

    def test_to_dict_is_json_ready(self):
        payload = self.registry.call("check_rules", {"asset_id": "M04"}).to_dict()
        self.assertEqual(set(payload), {"ok", "status", "data", "error"})
        self.assertIn("hits", payload["data"])


class TestToolsHappyPath(ToolCase):
    def test_list_pending_assets(self):
        result = self.registry.call("list_pending_assets")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 7)

    def test_list_pending_assets_with_channel(self):
        result = self.registry.call("list_pending_assets", {"channel": "douyin"})
        self.assertEqual(result.data["count"], 7)

    def test_get_asset_detail(self):
        result = self.registry.call("get_asset_detail", {"asset_id": "M03"})
        self.assertTrue(result.ok)
        self.assertFalse(result.data["complete"])
        self.assertEqual(result.data["missing_fields"], ["has_cover", "has_body"])

    def test_check_rules(self):
        result = self.registry.call("check_rules", {"asset_id": "M04"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["risk_level"], "block")
        self.assertEqual(result.data["hits"][0]["rule_id"], "R2")

    def test_query_slots(self):
        result = self.registry.call(
            "query_slots", {"channel": "douyin", "date": "2026-11-05"}
        )
        self.assertTrue(result.ok)
        self.assertIn("18:00", [s["slot"] for s in result.data["occupied"]])
        self.assertIn("19:00", result.data["available"])

    def test_save_draft_success(self):
        result = self.registry.call(
            "save_draft", {"asset_id": "M02", "slot": "2026-11-05T20:00"}
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "draft_saved")
        self.assertFalse(result.data["able_to_publish"])

    def test_save_draft_repeat_returns_same_draft(self):
        first = self.registry.call(
            "save_draft", {"asset_id": "M02", "slot": "2026-11-05T20:00"}
        )
        second = self.registry.call(
            "save_draft", {"asset_id": "M02", "slot": "2026-11-05T20:00"}
        )
        self.assertEqual(first.data["draft_id"], second.data["draft_id"])
        self.assertTrue(second.data["idempotent_replay"])
        self.assertEqual(self.registry.drafts.count(), 1)


class TestSaveDraftGates(ToolCase):
    def test_blocked_asset_cannot_be_saved(self):
        result = self.registry.call(
            "save_draft", {"asset_id": "M04", "slot": "2026-11-06T12:00"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], ErrorCode.E_RULE_BLOCKED.value)
        self.assertFalse(result.error["retryable"])
        self.assertEqual(self.registry.drafts.count(), 0, "被阻断的素材不得产生草稿")

    def test_injected_failure_returns_storage_error(self):
        result = self.registry.call(
            "save_draft", {"asset_id": "M05", "slot": "2026-11-06T15:00"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], ErrorCode.E_STORAGE_WRITE.value)
        self.assertTrue(result.error["retryable"])
        self.assertEqual(self.registry.drafts.count(), 0, "写失败不得留下草稿")

    def test_hard_requirement_change_requires_confirmation(self):
        result = self.registry.call(
            "save_draft", {"asset_id": "M01", "slot": "2026-11-05T19:00"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], ErrorCode.E_INTEGRITY.value)
        self.assertEqual(self.registry.drafts.count(), 0)

    def test_hard_requirement_change_with_confirmation_succeeds(self):
        result = self.registry.call(
            "save_draft",
            {
                "asset_id": "M01",
                "slot": "2026-11-05T19:00",
                "pending_confirmation": True,
                "reason": "18:00 已被占用，建议 19:00",
            },
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["pending_confirmation"])
        self.assertEqual(result.data["origin_requested_at"], "2026-11-05T18:00")

    def test_save_draft_cannot_request_publish(self):
        # 工具签名里不存在发布参数，传了也会被忽略
        result = self.registry.call(
            "save_draft",
            {"asset_id": "M02", "slot": "2026-11-05T20:00", "able_to_publish": True},
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["able_to_publish"])


class TestToolErrors(ToolCase):
    def test_unknown_tool(self):
        result = self.registry.call("publish_now", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], ErrorCode.E_NOT_FOUND.value)

    def test_missing_required_arg(self):
        result = self.registry.call("get_asset_detail", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], ErrorCode.E_SCHEMA_INVALID.value)

    def test_invalid_asset_id_rejected(self):
        for bad in ("../../etc/passwd", "m01", "M1", "M001"):
            with self.subTest(asset_id=bad):
                result = self.registry.call("check_rules", {"asset_id": bad})
                self.assertFalse(result.ok)
                self.assertEqual(result.error["code"], ErrorCode.E_SCHEMA_INVALID.value)

    def test_unknown_asset(self):
        result = self.registry.call("get_asset_detail", {"asset_id": "M99"})
        self.assertEqual(result.error["code"], ErrorCode.E_NOT_FOUND.value)
        self.assertFalse(result.error["retryable"])

    def test_unknown_channel(self):
        result = self.registry.call(
            "query_slots", {"channel": "kuaishou", "date": "2026-11-05"}
        )
        self.assertEqual(result.error["code"], ErrorCode.E_NOT_FOUND.value)

    def test_non_dict_args(self):
        result = self.registry.call("check_rules", ["M01"])
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], ErrorCode.E_SCHEMA_INVALID.value)

    def test_unexpected_exception_does_not_escape(self):
        with mock.patch.object(
            AssetService, "detail", side_effect=RuntimeError("boom")
        ):
            result = self.registry.call("get_asset_detail", {"asset_id": "M01"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], ErrorCode.E_INTEGRITY.value)
        self.assertIn("boom", result.error["message"])


class TestScenarioCoverage(ToolCase):
    """用工具层走通各验收场景，确认闸门与放行边界。"""

    def test_m03_blocked_by_r1(self):
        result = self.registry.call(
            "save_draft", {"asset_id": "M03", "slot": "2026-11-06T10:00"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], ErrorCode.E_RULE_BLOCKED.value)
        self.assertIn("缺少要素", result.error["message"])

    def test_m07_saveable_after_supplement(self):
        detail = self.registry.call("get_asset_detail", {"asset_id": "M07"})
        self.assertTrue(detail.data["complete"], "M07 补查后应完整")
        result = self.registry.call(
            "save_draft", {"asset_id": "M07", "slot": "2026-11-06T17:00"}
        )
        self.assertTrue(result.ok)

    def test_m06_injection_does_not_change_behavior(self):
        rules = self.registry.call("check_rules", {"asset_id": "M06"})
        self.assertEqual(rules.data["risk_level"], "none")

        saved = self.registry.call(
            "save_draft", {"asset_id": "M06", "slot": "2026-11-06T16:00"}
        )
        self.assertTrue(saved.ok)
        # 草稿里不得出现任何"已发布"语义
        self.assertNotIn("已发布", str(saved.to_dict()))
        self.assertFalse(saved.data["able_to_publish"])

    def test_invalid_slot_rejected_by_tool(self):
        for bad in ("2026-11-05 20:00", "20:00", ""):
            with self.subTest(slot=bad):
                result = self.registry.call(
                    "save_draft", {"asset_id": "M02", "slot": bad}
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.error["code"], ErrorCode.E_SCHEMA_INVALID.value)

    def test_pending_confirmation_must_be_boolean(self):
        result = self.registry.call(
            "save_draft",
            {
                "asset_id": "M01",
                "slot": "2026-11-05T19:00",
                "pending_confirmation": "false",  # 字符串会被 bool() 判为真
                "reason": "x",
            },
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], ErrorCode.E_SCHEMA_INVALID.value)
        self.assertEqual(self.registry.drafts.count(), 0)

    def test_reason_must_be_string(self):
        result = self.registry.call(
            "save_draft",
            {
                "asset_id": "M01",
                "slot": "2026-11-05T19:00",
                "pending_confirmation": True,
                "reason": 123,
            },
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], ErrorCode.E_SCHEMA_INVALID.value)

    def test_bad_date_returns_not_found(self):
        result = self.registry.call(
            "query_slots", {"channel": "douyin", "date": "2026-11-07"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], ErrorCode.E_NOT_FOUND.value)


class TestAuditHook(ToolCase):
    def test_audit_receives_every_call(self):
        events = []

        def sink(name, args, result):
            events.append((name, args, result.ok))

        registry = ToolRegistry(self.repo, audit=sink)
        registry.call("list_pending_assets")
        registry.call("check_rules", {"asset_id": "M04"})
        registry.call("publish_now", {})

        self.assertEqual([e[0] for e in events], ["list_pending_assets", "check_rules", "publish_now"])
        self.assertEqual([e[2] for e in events], [True, True, False])

    def test_audit_not_required(self):
        # 未注入 audit 时不应报错
        self.assertTrue(ToolRegistry(self.repo).call("list_pending_assets").ok)


if __name__ == "__main__":
    unittest.main()
