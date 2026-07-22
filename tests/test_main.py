import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.provider import ProviderRequest

from data.plugins.astrbot_plugin_smart_followup.main import SmartFollowupPlugin


class SmartFollowupDecisionTest(unittest.TestCase):
    """验证模型控制块能够被安全清理和校验。"""

    def setUp(self) -> None:
        """创建仅用于解析测试且限制参数固定的插件实例。"""
        self.plugin = object.__new__(SmartFollowupPlugin)
        self.plugin.config = {
            "min_delay_seconds": 30,
            "max_delay_seconds": 3600,
            "max_message_length": 20,
        }

    def test_extracts_schedule_and_removes_control_block(self) -> None:
        """有效调度决策应被返回，且控制元数据不得泄漏。"""
        text = (
            "当前回复\n"
            '<astrbot_smart_followup>{"action":"schedule",'
            '"after_seconds":90,"message":"稍后再聊"}'
            "</astrbot_smart_followup>"
        )

        clean_text, decision = self.plugin._extract_decision(text)

        self.assertEqual(clean_text, "当前回复")
        self.assertEqual(
            decision,
            {
                "action": "schedule",
                "after_seconds": 90,
                "message": "稍后再聊",
            },
        )

    def test_none_action_produces_no_schedule(self) -> None:
        """明确选择不续聊时只清理响应，不生成调度决策。"""
        clean_text, decision = self.plugin._extract_decision(
            '再见\n<astrbot_smart_followup>{"action":"none"}'
            "</astrbot_smart_followup>"
        )

        self.assertEqual(clean_text, "再见")
        self.assertIsNone(decision)

    def test_delay_is_clamped_to_configured_bounds(self) -> None:
        """模型输出的越界等待时间应被确定性规则限制。"""
        _, short_decision = self.plugin._extract_decision(
            '<astrbot_smart_followup>{"action":"schedule",'
            '"after_seconds":1,"message":"短"}</astrbot_smart_followup>'
        )
        _, long_decision = self.plugin._extract_decision(
            '<astrbot_smart_followup>{"action":"schedule",'
            '"after_seconds":99999,"message":"长"}</astrbot_smart_followup>'
        )

        self.assertEqual(short_decision["after_seconds"], 30)
        self.assertEqual(long_decision["after_seconds"], 3600)

    def test_malformed_trailing_block_is_hidden(self) -> None:
        """格式损坏的控制元数据不得出现在用户可见回复中。"""
        clean_text, decision = self.plugin._extract_decision(
            "正常内容<astrbot_smart_followup>{broken"
        )

        self.assertEqual(clean_text, "正常内容")
        self.assertIsNone(decision)


class SmartFollowupRuntimeTest(unittest.IsolatedAsyncioTestCase):
    """验证定时发送及新用户消息使旧任务失效的行为。"""

    def setUp(self) -> None:
        """创建由异步测试替身支持的运行时插件实例。"""
        self.plugin = object.__new__(SmartFollowupPlugin)
        self.plugin.config = {
            "enabled": True,
            "private_only": True,
            "disable_streaming": True,
            "daily_limit": 3,
            "min_delay_seconds": 30,
            "max_delay_seconds": 3600,
            "max_message_length": 300,
        }
        self.plugin.context = SimpleNamespace(
            send_message=AsyncMock(return_value=True)
        )
        self.plugin.put_kv_data = AsyncMock()
        self.plugin._sessions = {}
        self.plugin._tasks = {}
        self.plugin._state_lock = asyncio.Lock()

    async def test_due_timer_sends_and_records_proactive_message(self) -> None:
        """当前待发送任务应只发送一次，并把主动消息衔接到下一轮。"""
        due_at = time.time() - 1
        self.plugin._sessions["umo"] = {
            "revision": 4,
            "pending": {
                "revision": 4,
                "due_at": due_at,
                "message": "还想继续聊聊吗？",
            },
            "daily_date": "",
            "daily_count": 0,
        }

        await self.plugin._wait_and_send("umo", 4, due_at)

        self.plugin.context.send_message.assert_awaited_once()
        state = self.plugin._sessions["umo"]
        self.assertIsNone(state["pending"])
        self.assertEqual(state["daily_count"], 1)
        self.assertEqual(
            state["last_proactive"]["message"], "还想继续聊聊吗？"
        )

    async def test_new_user_message_invalidates_pending_job(self) -> None:
        """任何新用户输入都应清除旧调度并递增消息版本号。"""
        self.plugin._sessions["umo"] = {
            "revision": 7,
            "last_user_at": time.time() - 20,
            "recent_intervals": [],
            "pending": {"revision": 7, "due_at": time.time() + 60},
            "daily_date": "",
            "daily_count": 0,
        }
        extras = {}
        event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            set_extra=extras.__setitem__,
        )

        await self.plugin.record_user_activity(event)

        state = self.plugin._sessions["umo"]
        self.assertEqual(state["revision"], 8)
        self.assertIsNone(state["pending"])
        self.assertEqual(extras["enable_streaming"], False)

    async def test_dynamic_context_is_temporary_and_system_rule_is_stable(
        self,
    ) -> None:
        """动态活跃信息不得进入影响缓存的稳定系统提示前缀。"""
        self.plugin._sessions["umo"] = {
            "revision": 2,
            "recent_intervals": [12, 18],
            "daily_date": "",
            "daily_count": 0,
        }
        event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
        )
        request = ProviderRequest(system_prompt="persona")

        await self.plugin.inject_followup_protocol(event, request)

        self.assertEqual(request.system_prompt.count("smart_followup_prompt:start"), 1)
        self.assertNotIn("Recent intervals between user messages", request.system_prompt)
        self.assertEqual(len(request.extra_user_content_parts), 1)
        part = request.extra_user_content_parts[0]
        self.assertIn("12s, 18s", part.text)
        self.assertTrue(part._no_save)


if __name__ == "__main__":
    unittest.main()
