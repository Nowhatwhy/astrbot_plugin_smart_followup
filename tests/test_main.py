import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.platform.platform_metadata import PlatformMetadata

from data.plugins.astrbot_plugin_smart_followup.main import (
    DEFAULT_WAKE_PROMPT,
    EVENT_DECISION_KEY,
    SmartFollowupPlugin,
    WAKE_EVENT_KEY,
    WAKE_PROMPT_KEY,
    WAKE_TRIGGER,
)


class SmartFollowupDecisionTest(unittest.TestCase):
    """验证等待时间控制标记的清理和解析。"""

    def setUp(self) -> None:
        """创建限制参数固定的解析器实例。"""
        self.plugin = object.__new__(SmartFollowupPlugin)
        self.plugin.config = {
            "min_delay_seconds": 30,
            "max_delay_seconds": 3600,
        }

    def test_extracts_delay_and_removes_marker(self) -> None:
        """新协议只需返回等待秒数，不再提前生成未来消息。"""
        clean_text, decision = self.plugin._extract_decision(
            "当前回复\n<<SMART_FOLLOWUP|90>>"
        )

        self.assertEqual(clean_text, "当前回复")
        self.assertEqual(decision, {"after_seconds": 90})

    def test_rejects_old_pre_generated_message_format(self) -> None:
        """包含预生成未来消息的旧标记必须拒绝调度。"""
        clean_text, decision = self.plugin._extract_decision(
            "回复<<SMART_FOLLOWUP|60|旧版未来消息>>"
        )

        self.assertEqual(clean_text, "回复")
        self.assertIsNone(decision)

    def test_no_marker_has_no_explicit_parser_decision(self) -> None:
        """没有控制标记时由运行时采用最长等待兜底。"""
        clean_text, decision = self.plugin._extract_decision("普通回复")

        self.assertEqual(clean_text, "普通回复")
        self.assertIsNone(decision)

    def test_extracts_explicit_never_decision(self) -> None:
        """只有 NEVER 才明确表示永久停止主动联系。"""
        clean_text, decision = self.plugin._extract_decision(
            "好的<<SMART_FOLLOWUP|NEVER>>"
        )

        self.assertEqual(clean_text, "好的")
        self.assertEqual(decision, {"never": 1})

    def test_rejects_legacy_json_block(self) -> None:
        """旧版 XML 与 JSON 控制块不得再生成调度决策。"""
        clean_text, decision = self.plugin._extract_decision(
            '回复```json\n<astrbot_smart_followup>{"action":"schedule",'
            '"after_seconds":45,"message":"旧消息"}'
            "</astrbot_smart_followup>\n```"
        )

        self.assertEqual(clean_text, "回复")
        self.assertIsNone(decision)

    def test_delay_is_clamped_to_configured_bounds(self) -> None:
        """越界等待时间应由插件确定性限制。"""
        _, short_decision = self.plugin._extract_decision("<<SMART_FOLLOWUP|1>>")
        _, long_decision = self.plugin._extract_decision("<<SMART_FOLLOWUP|99999>>")

        self.assertEqual(short_decision["after_seconds"], 30)
        self.assertEqual(long_decision["after_seconds"], 3600)


class SmartFollowupRuntimeTest(unittest.IsolatedAsyncioTestCase):
    """验证缓存友好的本地等待任务与唤醒请求。"""

    def setUp(self) -> None:
        """创建带有最小上下文替身的插件实例。"""
        self.plugin = object.__new__(SmartFollowupPlugin)
        self.plugin.config = {
            "enabled": True,
            "private_only": True,
            "disable_streaming": True,
            "debug_full_payload": False,
            "daily_limit": 3,
            "min_delay_seconds": 30,
            "max_delay_seconds": 3600,
        }
        self.plugin.context = SimpleNamespace()
        self.plugin.put_kv_data = AsyncMock()
        self.plugin._sessions = {}
        self.plugin._state_lock = asyncio.Lock()
        self.plugin._tasks = {}

    async def asyncTearDown(self) -> None:
        """取消测试中尚未到期的等待任务。"""
        tasks = list(self.plugin._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_initialize_removes_legacy_activity_intervals(self) -> None:
        """初始化时应清除旧版本持久化的用户活动间隔。"""
        self.plugin.get_kv_data = AsyncMock(
            return_value={
                "sessions": {
                    "umo": {
                        "revision": 1,
                        "last_user_at": 123.0,
                        "recent_intervals": [1, 2, 3],
                        "pending": None,
                    }
                }
            }
        )

        await self.plugin.initialize()

        state = self.plugin._sessions["umo"]
        self.assertNotIn("last_user_at", state)
        self.assertNotIn("recent_intervals", state)
        self.plugin.put_kv_data.assert_awaited()

    async def test_new_user_message_cancels_pending_agent_job(self) -> None:
        """新用户消息应删除旧任务并递增会话版本号。"""
        self.plugin._sessions["umo"] = {
            "revision": 7,
            "pending": {
                "revision": 7,
                "run_at": "2999-01-01T00:00:00+00:00",
            },
            "daily_date": "",
            "daily_count": 0,
        }
        old_task = asyncio.create_task(asyncio.sleep(3600))
        self.plugin._tasks["umo"] = old_task
        extras = {}
        event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            get_message_str=lambda: "用户消息",
            get_message_outline=lambda: "用户消息",
            get_extra=lambda key: extras.get(key),
            set_extra=extras.__setitem__,
        )

        await self.plugin.record_user_activity(event)

        await asyncio.sleep(0)
        self.assertTrue(old_task.cancelled())
        state = self.plugin._sessions["umo"]
        self.assertEqual(state["revision"], 8)
        self.assertIsNone(state["pending"])
        self.assertNotIn("last_user_at", state)
        self.assertNotIn("recent_intervals", state)
        self.assertFalse(extras["enable_streaming"])

    async def test_default_mode_does_not_log_routine_user_activity(self) -> None:
        """关闭调试日志时，普通用户活动不应产生插件 INFO 日志。"""
        event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            get_message_str=lambda: "用户消息",
            get_message_outline=lambda: "用户消息",
            get_extra=lambda key: None,
            set_extra=lambda key, value: None,
        )

        with patch(
            "data.plugins.astrbot_plugin_smart_followup.main.logger.info"
        ) as log_info:
            await self.plugin.record_user_activity(event)

        log_info.assert_not_called()

    async def test_empty_platform_event_does_not_cancel_pending_wake(self) -> None:
        """正在输入等空平台事件不应被当作用户发送消息。"""
        self.plugin._sessions["umo"] = {
            "revision": 7,
            "pending": {
                "revision": 7,
                "run_at": "2999-01-01T00:00:00+00:00",
            },
            "daily_date": "",
            "daily_count": 0,
        }
        pending_task = asyncio.create_task(asyncio.sleep(3600))
        self.plugin._tasks["umo"] = pending_task
        event = SimpleNamespace(
            unified_msg_origin="umo",
            get_message_str=lambda: "",
            get_message_outline=lambda: "",
            get_extra=lambda key: None,
        )

        await self.plugin.record_user_activity(event)

        self.assertFalse(pending_task.cancelled())
        self.assertEqual(self.plugin._sessions["umo"]["revision"], 7)
        self.assertIsNotNone(self.plugin._sessions["umo"]["pending"])

    async def test_prompt_rule_is_system_and_runtime_data_is_temporary(self) -> None:
        """稳定规则应进入 system prompt，动态数据应只用于本轮。"""
        self.plugin._sessions["umo"] = {
            "revision": 2,
            "daily_date": "",
            "daily_count": 0,
        }
        event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            get_extra=lambda key: None,
        )
        request = ProviderRequest(system_prompt="persona")

        await self.plugin.inject_followup_protocol(event, request)

        self.assertIn("<<SMART_FOLLOWUP|秒数>>", request.system_prompt)
        self.assertIn("主动联系是默认行为", request.system_prompt)
        self.assertIn("当前即时回复本身不能算作下一次联系", request.system_prompt)
        self.assertIn("必须按其要求安排", request.system_prompt)
        self.assertIn("<<SMART_FOLLOWUP|60>>", request.system_prompt)
        self.assertIn("必须出现在模型原始输出中", request.system_prompt)
        self.assertIn("旧安排会自动取消", request.system_prompt)
        self.assertIn("<<SMART_FOLLOWUP|NEVER>>", request.system_prompt)
        self.assertNotIn("30", request.system_prompt)
        self.assertNotIn("3600", request.system_prompt)
        self.assertEqual(len(request.extra_user_content_parts), 1)
        self.assertIn("当前本地时间", request.extra_user_content_parts[0].text)
        self.assertIn(
            "只在思考中决定时间不算完成",
            request.extra_user_content_parts[0].text,
        )
        self.assertIn(
            "<<SMART_FOLLOWUP|NEVER>>", request.extra_user_content_parts[0].text
        )
        self.assertIn(
            "不得裸输出数字或 NEVER", request.extra_user_content_parts[0].text
        )
        self.assertNotIn("近期用户消息间隔", request.extra_user_content_parts[0].text)
        self.assertNotIn("今日已安排主动回复", request.extra_user_content_parts[0].text)
        self.assertTrue(request.extra_user_content_parts[0]._no_save)

    async def test_user_prompt_reminder_can_be_disabled(self) -> None:
        """关闭配置后应保留临时时间，但不再拼接格式提醒。"""
        self.plugin.config["user_prompt_reminder_enabled"] = False
        self.plugin._sessions["umo"] = {"revision": 1}
        event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            get_extra=lambda key: None,
        )
        request = ProviderRequest(system_prompt="persona", prompt="用户消息")

        await self.plugin.inject_followup_protocol(event, request)

        self.assertEqual(len(request.extra_user_content_parts), 1)
        self.assertIn("当前本地时间", request.extra_user_content_parts[0].text)
        self.assertNotIn(
            "只在思考中决定时间不算完成",
            request.extra_user_content_parts[0].text,
        )
        self.assertTrue(request.extra_user_content_parts[0]._no_save)

    async def test_user_prompt_reminder_content_can_be_configured(self) -> None:
        """启用提醒时应使用管理员配置的自定义内容。"""
        custom_reminder = "自定义提醒：最终一行输出完整的调度标记。"
        self.plugin.config["user_prompt_reminder"] = custom_reminder
        self.plugin._sessions["umo"] = {"revision": 1}
        event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            get_extra=lambda key: None,
        )
        request = ProviderRequest(system_prompt="persona", prompt="用户消息")

        await self.plugin.inject_followup_protocol(event, request)

        temporary_context = request.extra_user_content_parts[0]
        self.assertIn(custom_reminder, temporary_context.text)
        self.assertNotIn("只在思考中决定时间不算完成", temporary_context.text)
        self.assertTrue(temporary_context._no_save)

    async def test_delay_config_does_not_change_system_prompt(self) -> None:
        """修改代码兜底范围不应改变稳定的 system prompt。"""
        self.plugin._sessions["umo"] = {"revision": 1}
        event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            get_extra=lambda key: None,
        )
        first_request = ProviderRequest(system_prompt="persona")

        await self.plugin.inject_followup_protocol(event, first_request)

        self.plugin.config["min_delay_seconds"] = 5
        self.plugin.config["max_delay_seconds"] = 10
        second_request = ProviderRequest(system_prompt="persona")

        await self.plugin.inject_followup_protocol(event, second_request)

        self.assertEqual(first_request.system_prompt, second_request.system_prompt)

    async def test_wake_uses_same_system_prompt_and_temporary_user_tail(self) -> None:
        """唤醒请求只应在历史末尾追加不保存的临时 user 消息。"""
        self.plugin._sessions["umo"] = {
            "revision": 4,
            "daily_date": "",
            "daily_count": 0,
        }
        normal_event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            get_extra=lambda key: None,
        )
        wake_event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            get_extra=lambda key: {
                WAKE_EVENT_KEY: 4,
                WAKE_PROMPT_KEY: DEFAULT_WAKE_PROMPT,
            }.get(key),
            stop_event=lambda: None,
        )
        normal_request = ProviderRequest(system_prompt="persona", prompt="用户消息")
        wake_request = ProviderRequest(
            system_prompt="persona",
            prompt=WAKE_TRIGGER,
            contexts=[{"role": "assistant", "content": "上一条回复"}],
        )

        await self.plugin.inject_followup_protocol(normal_event, normal_request)
        await self.plugin.inject_followup_protocol(wake_event, wake_request)

        self.assertEqual(wake_request.system_prompt, normal_request.system_prompt)
        self.assertIsNone(wake_request.prompt)
        self.assertEqual(wake_request.extra_user_content_parts, [])
        self.assertTrue(wake_request.contexts[-1]["_no_save"])
        self.assertIn(
            DEFAULT_WAKE_PROMPT,
            [part["text"] for part in wake_request.contexts[-1]["content"]],
        )
        self.assertTrue(
            any(
                "只在思考中决定时间不算完成" in part["text"]
                for part in wake_request.contexts[-1]["content"]
            )
        )
        self.assertIn("不要再次判断是否发送", DEFAULT_WAKE_PROMPT)
        self.assertIn("自然、非空的主动消息", DEFAULT_WAKE_PROMPT)

    async def test_schedule_creates_persistent_local_wait(self) -> None:
        """回复发送后应持久化时间并创建一次本地等待任务。"""
        self.plugin._sessions["umo"] = {
            "revision": 4,
            "pending": None,
            "daily_date": "",
            "daily_count": 0,
        }
        extras = {EVENT_DECISION_KEY: {"after_seconds": 45, "revision": 4}}
        event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            get_self_id=lambda: "bot-1",
            get_extra=lambda key: extras.get(key),
            set_extra=extras.__setitem__,
        )

        await self.plugin.schedule_after_reply(event)

        pending = self.plugin._sessions["umo"]["pending"]
        self.assertEqual(pending["revision"], 4)
        self.assertEqual(pending["self_id"], "bot-1")
        self.assertIn("run_at", pending)
        self.assertIn("umo", self.plugin._tasks)
        self.assertEqual(self.plugin._sessions["umo"]["daily_count"], 1)

    async def test_due_task_queues_normal_platform_event(self) -> None:
        """到期任务应保留原平台能力并进入普通事件队列。"""
        umo = "default:FriendMessage:736644851"
        queued_events = []
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
            get_event_queue=lambda: SimpleNamespace(put_nowait=queued_events.append),
        )
        self.plugin._sessions[umo] = {
            "revision": 6,
            "pending": {"revision": 6},
        }

        await self.plugin._wake_after(
            umo=umo,
            revision=6,
            run_at=datetime.now().astimezone(),
            wake_prompt=DEFAULT_WAKE_PROMPT,
            self_id="bot-1",
        )

        self.assertEqual(len(queued_events), 1)
        wake_event = queued_events[0]
        self.assertEqual(wake_event.unified_msg_origin, umo)
        self.assertEqual(wake_event.platform_meta, metadata)
        self.assertEqual(wake_event.message_str, WAKE_TRIGGER)
        self.assertEqual(wake_event.get_extra(WAKE_EVENT_KEY), 6)
        self.assertEqual(wake_event.get_extra(WAKE_PROMPT_KEY), DEFAULT_WAKE_PROMPT)
        self.assertFalse(wake_event.get_extra("enable_streaming"))

    async def test_schedule_can_be_recovered_from_reasoning_content(self) -> None:
        """推理区中的等待标记也应生成任务决策并被清理。"""
        self.plugin._sessions["umo"] = {
            "revision": 3,
            "daily_date": "",
            "daily_count": 0,
        }
        extras = {}
        event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            get_extra=lambda key: extras.get(key),
            set_extra=extras.__setitem__,
        )
        response = LLMResponse(
            role="assistant",
            completion_text="正常回复",
            reasoning_content="分析<<SMART_FOLLOWUP|45>>",
        )

        await self.plugin.parse_followup_decision(event, response)

        self.assertEqual(response.reasoning_content, "分析")
        self.assertEqual(extras[EVENT_DECISION_KEY]["revision"], 3)
        self.assertEqual(extras[EVENT_DECISION_KEY]["after_seconds"], 45)

    async def test_wake_keeps_reply_and_schedules_next_contact(self) -> None:
        """到点后的正文应直接发送，并继续安排下一次主动联系。"""
        self.plugin._sessions["umo"] = {
            "revision": 2,
            "daily_date": "",
            "daily_count": 0,
        }
        extras = {WAKE_EVENT_KEY: 2}
        event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            get_extra=lambda key: extras.get(key),
            set_extra=extras.__setitem__,
        )
        response = LLMResponse(
            role="assistant",
            completion_text="到点啦，你刚才想说什么？<<SMART_FOLLOWUP|30>>",
        )

        await self.plugin.parse_followup_decision(event, response)

        self.assertEqual(response.completion_text, "到点啦，你刚才想说什么？")
        self.assertEqual(extras[EVENT_DECISION_KEY]["revision"], 2)
        self.assertEqual(extras[EVENT_DECISION_KEY]["after_seconds"], 30)

    async def test_missing_marker_uses_maximum_delay_instead_of_forever(self) -> None:
        """模型漏掉必需标记时应使用最长等待，而不是永久停止。"""
        self.plugin._sessions["umo"] = {
            "revision": 5,
            "daily_date": "",
            "daily_count": 0,
        }
        extras = {}
        event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            get_extra=lambda key: extras.get(key),
            set_extra=extras.__setitem__,
        )
        response = LLMResponse(role="assistant", completion_text="普通回复")

        await self.plugin.parse_followup_decision(event, response)

        self.assertEqual(extras[EVENT_DECISION_KEY]["revision"], 5)
        self.assertEqual(extras[EVENT_DECISION_KEY]["after_seconds"], 3600)

    async def test_daily_limit_defers_instead_of_stopping_chain(self) -> None:
        """达到每日上限后应推迟到次日，而不是永久停止主动联系。"""
        today = datetime.now().astimezone().date().isoformat()
        self.plugin._sessions["umo"] = {
            "revision": 6,
            "daily_date": today,
            "daily_count": 3,
        }
        extras = {}
        event = SimpleNamespace(
            unified_msg_origin="umo",
            is_private_chat=lambda: True,
            get_extra=lambda key: extras.get(key),
            set_extra=extras.__setitem__,
        )
        response = LLMResponse(
            role="assistant",
            completion_text="稍后再聊<<SMART_FOLLOWUP|30>>",
        )

        await self.plugin.parse_followup_decision(event, response)

        self.assertEqual(response.completion_text, "稍后再聊")
        self.assertGreater(extras[EVENT_DECISION_KEY]["after_seconds"], 30)


if __name__ == "__main__":
    unittest.main()
