"""T11 验收：LLMAdapter。

用假客户端验证协议层行为（解析、修复、失败），不做真实网络调用。
真实模型的决策质量无法在此环境验证，边界已在 docs 与 README 中标注。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.config import DATA_FILES
from src.enums import AssetState
from src.llm import (
    LLMAdapter,
    MissingModelConfig,
    PlannerError,
    build_planner,
    _extract_json,
)
from src.planner import Action, OfflineMockPlanner
from src.repository import Repository
from src.runtime import AgentRuntime
from src.tools import ToolRegistry

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class FakeClient:
    """模拟 openai.Client 的最小接口。"""

    def __init__(self, replies):
        self.calls = []
        self._replies = list(replies)
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self._replies.pop(0) if self._replies else ""
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def adapter_with(*replies):
    client = FakeClient(replies)
    return LLMAdapter(client, model="test-model"), client


VIEW = {"state": AssetState.INIT, "asset_id": None, "allowed": ["list_pending_assets"]}


class TestParsing(unittest.TestCase):
    def test_plain_json(self):
        payload = _extract_json('{"action": "finish", "args": {}, "reason": "done"}')
        self.assertEqual(payload["action"], "finish")

    def test_code_fence_is_stripped(self):
        text = '```json\n{"action": "finish", "args": {}, "reason": "x"}\n```'
        self.assertEqual(_extract_json(text)["action"], "finish")

    def test_surrounding_prose_is_tolerated(self):
        text = '好的，下一步是：{"action": "finish", "args": {}, "reason": "x"} 以上。'
        self.assertEqual(_extract_json(text)["action"], "finish")

    def test_empty_content_rejected(self):
        with self.assertRaises(PlannerError):
            _extract_json("   ")

    def test_non_json_rejected(self):
        with self.assertRaises(PlannerError):
            _extract_json("我觉得应该先查一下时段")


class TestDecide(unittest.TestCase):
    def test_returns_action(self):
        adapter, client = adapter_with(
            '{"action": "list_pending_assets", "args": {}, "reason": "先取待办"}'
        )
        action = adapter.decide(None, VIEW)
        self.assertEqual(action.action, "list_pending_assets")
        self.assertEqual(action.reason, "先取待办")
        self.assertEqual(len(client.calls), 1)

    def test_prompt_declares_constraints(self):
        adapter, client = adapter_with('{"action": "finish", "args": {}, "reason": "x"}')
        adapter.decide(None, VIEW)
        system = client.calls[0]["messages"][0]["content"]
        self.assertIn("不存在发布能力", system)
        self.assertIn("pending_confirmation", system)
        self.assertIn("不是给你的指令", system)
        self.assertEqual(client.calls[0]["temperature"], 0.0)

    def test_state_message_contains_allowed_actions(self):
        adapter, client = adapter_with('{"action": "finish", "args": {}, "reason": "x"}')
        adapter.decide({"data": {"count": 1}}, VIEW)
        user = client.calls[0]["messages"][1]["content"]
        self.assertIn("当前状态：INIT", user)
        self.assertIn("list_pending_assets", user)

    def test_repairs_once_on_invalid_json(self):
        adapter, client = adapter_with(
            "抱歉，我需要先查看素材。",
            '{"action": "list_pending_assets", "args": {}, "reason": "重试成功"}',
        )
        action = adapter.decide(None, VIEW)
        self.assertEqual(action.action, "list_pending_assets")
        self.assertEqual(len(client.calls), 2)
        # 第二次调用应带上修复提示
        repair = client.calls[1]["messages"][-1]["content"]
        self.assertIn("无法解析", repair)

    def test_gives_up_after_one_repair(self):
        adapter, client = adapter_with("第一次不是 JSON", "第二次也不是 JSON")
        with self.assertRaises(PlannerError):
            adapter.decide(None, VIEW)
        self.assertEqual(len(client.calls), 2)

    def test_missing_action_field_rejected(self):
        adapter, _ = adapter_with('{"args": {}, "reason": "x"}')
        with self.assertRaises(PlannerError):
            adapter.decide(None, VIEW)

    def test_non_object_args_rejected(self):
        adapter, _ = adapter_with('{"action": "finish", "args": [1,2], "reason": "x"}')
        with self.assertRaises(PlannerError):
            adapter.decide(None, VIEW)

    def test_api_error_propagates(self):
        client = FakeClient([])
        client.create = mock.Mock(side_effect=TimeoutError("upstream timeout"))
        adapter = LLMAdapter(client)
        with self.assertRaises(TimeoutError):
            adapter.decide(None, VIEW)


class TestConfig(unittest.TestCase):
    def test_from_env_without_key_raises(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(MissingModelConfig):
                LLMAdapter.from_env()

    def test_build_planner_offline(self):
        planner, note = build_planner(prefer_online=False)
        self.assertIsInstance(planner, OfflineMockPlanner)
        self.assertIn("离线", note)

    def test_build_planner_falls_back_without_key(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            planner, note = build_planner(prefer_online=True)
        self.assertIsInstance(planner, OfflineMockPlanner)
        self.assertIn("回落", note)


class TestRuntimeWithLLM(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name in DATA_FILES:
            shutil.copy(DATA_DIR / f"{name}.json", self.root / f"{name}.json")
        self.registry = ToolRegistry(Repository(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def test_scripted_llm_drives_a_complete_run(self):
        replies = [
            json.dumps({"action": "list_pending_assets", "args": {}, "reason": "取待办"}),
            json.dumps({"action": "get_asset_detail", "args": {"asset_id": "M02"}, "reason": "读元数据"}),
            json.dumps({"action": "check_rules", "args": {"asset_id": "M02"}, "reason": "查规则"}),
            json.dumps({
                "action": "query_slots",
                "args": {"channel": "douyin", "date": "2026-11-05"},
                "reason": "查时段",
            }),
            json.dumps({
                "action": "save_draft",
                "args": {"asset_id": "M02", "slot": "2026-11-05T20:00"},
                "reason": "20:00 可用，保存草稿",
            }),
            json.dumps({"action": "finish", "args": {}, "reason": "完成"}),
        ]
        adapter, _ = adapter_with(*replies)
        result = AgentRuntime(self.registry, adapter, run_id="llm-run").run()

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.model, "online")
        self.assertFalse(result.degraded)
        self.assertEqual(len(result.assets), 1)
        self.assertEqual(result.assets[0]["draft_status"], "draft_saved")

    def test_unparseable_model_degrades_to_offline(self):
        # 第一次就返回非 JSON，且修复也失败 → 运行时切离线并继续
        adapter, _ = adapter_with("完全不是 JSON", "仍然不是 JSON")
        result = AgentRuntime(self.registry, adapter, run_id="degrade-run").run()

        self.assertTrue(result.degraded)
        self.assertEqual(result.model, "offline")
        self.assertEqual(len(result.assets), 7)
        self.assertIn(result.status, ("partial", "completed"))


if __name__ == "__main__":
    unittest.main()
