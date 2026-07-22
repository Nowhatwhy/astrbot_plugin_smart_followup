import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.platform_metadata import PlatformMetadata
from data.plugins.astrbot_plugin_smart_followup.main import (
    EVENT_DECISION,
    EVENT_REQUEST,
    EVENT_REVISION,
    WAKE_EVENT,
    WAKE_PROMPT,
    SmartFollowupPlugin,
)


class FakeEvent:
    """测试所需的最小消息事件。"""

    def __init__(self, message: str = "用户消息", extras: dict | None = None):
        """创建事件。

        Args:
            message: 消息文本。
            extras: 初始临时数据。
        """
        self.unified_msg_origin = "umo"
        self.message = message
        self.extras = extras or {}
        self.stopped = False

    def is_private_chat(self) -> bool:
        """返回私聊标记。"""
        return True

    def get_message_str(self) -> str:
        """返回消息文本。"""
        return self.message

    def get_message_outline(self) -> str:
        """返回消息摘要。"""
        return self.message

    def get_extra(self, key: str):
        """读取临时数据。

        Args:
            key: 数据键。
        """
        return self.extras.get(key)

    def set_extra(self, key: str, value) -> None:
        """写入临时数据。

        Args:
            key: 数据键。
            value: 数据值。
        """
        self.extras[key] = value

    def stop_event(self) -> None:
        """记录事件已停止。"""
        self.stopped = True

    def get_self_id(self) -> str:
        """返回机器人账号。"""
        return "bot-1"


class SmartFollowupTest(unittest.IsolatedAsyncioTestCase):
    """验证提示词、标签、计时和补决策。"""

    def setUp(self) -> None:
        """创建插件。"""
        schema = json.loads(
            Path(__file__).parents[1].joinpath("_conf_schema.json").read_text()
        )
        config = {key: value["default"] for key, value in schema.items()}
        self.plugin = object.__new__(SmartFollowupPlugin)
        self.plugin.config = config
        self.plugin.context = SimpleNamespace()
        self.plugin._revisions = {}
        self.plugin._tasks = {}

    async def asyncTearDown(self) -> None:
        """取消测试计时器。"""
        tasks = list(self.plugin._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def test_parse_tag(self) -> None:
        """解析器应删除标签并读取秒数或 NEVER。"""
        text, seconds = self.plugin._parse_text("回复<<SMART_FOLLOWUP|90>>")
        never_text, never = self.plugin._parse_text("结束<<SMART_FOLLOWUP|NEVER>>")
        malformed_text, malformed = self.plugin._parse_text(
            "回复<<SMART_FOLLOWUP|错误>>"
        )

        self.assertEqual((text, seconds), ("回复", 90))
        self.assertEqual((never_text, never), ("结束", "NEVER"))
        self.assertEqual((malformed_text, malformed), ("回复", None))

    async def test_user_message_replaces_timer(self) -> None:
        """新用户消息应取消旧计时器并增加会话版本。"""
        old_task = asyncio.create_task(asyncio.sleep(3600))
        self.plugin._tasks["umo"] = old_task
        event = FakeEvent()

        await self.plugin.record_message(event)
        await asyncio.sleep(0)

        self.assertTrue(old_task.cancelled())
        self.assertEqual(self.plugin._revisions["umo"], 1)
        self.assertEqual(event.get_extra(EVENT_REVISION), 1)

    async def test_empty_event_does_not_cancel_timer(self) -> None:
        """正在输入等空事件不应取消计时器。"""
        task = asyncio.create_task(asyncio.sleep(3600))
        self.plugin._tasks["umo"] = task

        await self.plugin.record_message(FakeEvent(""))

        self.assertFalse(task.cancelled())
        self.assertNotIn("umo", self.plugin._revisions)

    async def test_injects_stable_and_temporary_prompts(self) -> None:
        """稳定规则进入 system，动态内容只用于当前请求。"""
        self.plugin._revisions["umo"] = 1
        event = FakeEvent(extras={EVENT_REVISION: 1})
        request = ProviderRequest(system_prompt="persona", prompt="用户消息")

        await self.plugin.inject_prompt(event, request)

        self.assertIn(self.plugin.config["decision_prompt"], request.system_prompt)
        self.assertEqual(len(request.extra_user_content_parts), 1)
        self.assertIn("当前本地时间", request.extra_user_content_parts[0].text)
        self.assertTrue(request.extra_user_content_parts[0]._no_save)
        self.assertIs(event.get_extra(EVENT_REQUEST), request)

    async def test_wake_prompt_is_not_saved(self) -> None:
        """唤醒指令应成为历史末尾的不保存 user 消息。"""
        self.plugin._revisions["umo"] = 2
        event = FakeEvent(
            extras={
                EVENT_REVISION: 2,
                WAKE_EVENT: 2,
                WAKE_PROMPT: self.plugin.config["wake_prompt"],
            }
        )
        request = ProviderRequest(
            system_prompt="persona",
            prompt=f"{self.plugin.config['wake_prompt']}\n召回内容",
            contexts=[{"role": "assistant", "content": "上一条回复"}],
        )

        await self.plugin.inject_prompt(event, request)

        self.assertIsNone(request.prompt)
        self.assertTrue(request.contexts[-1]["_no_save"])
        self.assertIn("召回内容", request.contexts[-1]["content"][0]["text"])

    async def test_response_stores_delay_or_retry(self) -> None:
        """有效标签保存秒数，漏标签保存待补决策正文。"""
        self.plugin._revisions["umo"] = 3
        tagged = FakeEvent(extras={EVENT_REVISION: 3, EVENT_REQUEST: object()})
        missing = FakeEvent(extras={EVENT_REVISION: 3, EVENT_REQUEST: object()})
        tagged_response = LLMResponse(
            role="assistant",
            completion_text="回复<<SMART_FOLLOWUP|45>>",
        )
        missing_response = LLMResponse(role="assistant", completion_text="普通回复")

        await self.plugin.read_decision(tagged, tagged_response)
        await self.plugin.read_decision(missing, missing_response)

        self.assertEqual(tagged_response.completion_text, "回复")
        self.assertEqual(tagged.get_extra(EVENT_DECISION), (3, 45))
        self.assertIsNone(tagged.get_extra(EVENT_REQUEST))
        self.assertEqual(missing.get_extra(EVENT_DECISION), (3, "普通回复"))

    async def test_explicit_delay_creates_timer(self) -> None:
        """回复发送后应按明确秒数启动计时器。"""
        self.plugin._revisions["umo"] = 4
        event = FakeEvent(extras={EVENT_DECISION: (4, 60)})

        await self.plugin.schedule(event)

        self.assertIn("umo", self.plugin._tasks)

    async def test_missing_tag_retries_with_same_request(self) -> None:
        """漏标签时应复用原请求进行一次轻量补决策。"""
        self.plugin._revisions["umo"] = 5
        request = ProviderRequest(
            system_prompt="system",
            prompt="用户消息",
            contexts=[{"role": "assistant", "content": "历史回复"}],
            extra_user_content_parts=[TextPart(text="临时提醒").mark_as_temp()],
            model="model-1",
            session_id="session-1",
        )
        event = FakeEvent(
            extras={EVENT_DECISION: (5, "主回复"), EVENT_REQUEST: request}
        )
        self.plugin.context.get_current_chat_provider_id = AsyncMock(
            return_value="provider-1"
        )
        self.plugin.context.llm_generate = AsyncMock(
            return_value=LLMResponse(
                role="assistant", completion_text="<<SMART_FOLLOWUP|90>>"
            )
        )

        await self.plugin.schedule(event)

        call = self.plugin.context.llm_generate.await_args.kwargs
        self.assertEqual(call["system_prompt"], "system")
        self.assertEqual(call["prompt"], self.plugin.config["retry_prompt"])
        self.assertEqual(call["contexts"][-1]["content"], "主回复")
        self.assertIn("umo", self.plugin._tasks)

    async def test_failed_retry_does_not_schedule(self) -> None:
        """补决策仍无标签时不应构造兜底时间。"""
        self.plugin._revisions["umo"] = 6
        request = ProviderRequest(system_prompt="system")
        event = FakeEvent(
            extras={EVENT_DECISION: (6, "主回复"), EVENT_REQUEST: request}
        )
        self.plugin.context.get_current_chat_provider_id = AsyncMock(
            return_value="provider-1"
        )
        self.plugin.context.llm_generate = AsyncMock(
            return_value=LLMResponse(role="assistant", completion_text="无标签")
        )

        await self.plugin.schedule(event)

        self.assertNotIn("umo", self.plugin._tasks)

    async def test_new_message_during_retry_discards_result(self) -> None:
        """补决策期间出现新用户消息时应丢弃旧结果。"""
        self.plugin._revisions["umo"] = 7
        request = ProviderRequest(system_prompt="system")
        event = FakeEvent(
            extras={EVENT_DECISION: (7, "主回复"), EVENT_REQUEST: request}
        )
        self.plugin.context.get_current_chat_provider_id = AsyncMock(
            return_value="provider-1"
        )

        async def change_revision(**kwargs) -> LLMResponse:
            self.plugin._revisions["umo"] = 8
            return LLMResponse(
                role="assistant", completion_text="<<SMART_FOLLOWUP|90>>"
            )

        self.plugin.context.llm_generate = AsyncMock(side_effect=change_revision)

        await self.plugin.schedule(event)

        self.assertNotIn("umo", self.plugin._tasks)

    async def test_timer_queues_wake_event(self) -> None:
        """计时结束后应向原会话投递完整 Agent 事件。"""
        umo = "default:FriendMessage:736644851"
        queued = []
        metadata = PlatformMetadata(
            name="aiocqhttp",
            description="test",
            id="default",
            support_streaming_message=False,
            support_proactive_message=True,
        )
        self.plugin.context = SimpleNamespace(
            get_platform_inst=lambda platform_id: SimpleNamespace(
                meta=lambda: metadata
            ),
            get_event_queue=lambda: SimpleNamespace(put_nowait=queued.append),
        )
        self.plugin._revisions[umo] = 9

        await self.plugin._wake_after(
            umo=umo,
            revision=9,
            delay=0,
            self_id="bot-1",
        )

        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].message_str, self.plugin.config["wake_prompt"])
        self.assertEqual(queued[0].get_extra(WAKE_EVENT), 9)


if __name__ == "__main__":
    unittest.main()
