import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

PROMPT_MARKER_START = "<!-- smart_followup_prompt:start -->"
PROMPT_MARKER_END = "<!-- smart_followup_prompt:end -->"
CONTROL_TAG_START = "<astrbot_smart_followup>"
CONTROL_TAG_END = "</astrbot_smart_followup>"
STATE_KEY = "session_state_v1"
EVENT_DECISION_KEY = "smart_followup_decision"
LOG_PREFIX = "[smart_followup]"


class SmartFollowupPlugin(Star):
    """在用户沉默后安排一条结合上下文生成的主动消息。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        """初始化运行时状态，但不在事件循环就绪前创建定时任务。

        Args:
            context: AstrBot 插件上下文。
            config: 根据 `_conf_schema.json` 生成的插件配置。
        """
        super().__init__(context, config)
        self.config = config
        self._sessions: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._state_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """恢复已持久化的待发送消息，并重新启动对应的定时任务。"""
        stored = await self.get_kv_data(STATE_KEY, {})
        if isinstance(stored, dict):
            sessions = stored.get("sessions", {})
            if isinstance(sessions, dict):
                self._sessions = {
                    str(umo): state
                    for umo, state in sessions.items()
                    if isinstance(state, dict)
                }

        logger.info(
            "%s Plugin initialized: enabled=%s private_only=%s "
            "delay_range=%s-%ss daily_limit=%s persisted_sessions=%s",
            LOG_PREFIX,
            self.config.get("enabled", True),
            self.config.get("private_only", True),
            max(1, int(self.config.get("min_delay_seconds", 30))),
            max(1, int(self.config.get("max_delay_seconds", 86400))),
            max(0, int(self.config.get("daily_limit", 3))),
            len(self._sessions),
        )

        if not self.config.get("enabled", True) or max(
            0, int(self.config.get("daily_limit", 3))
        ) == 0:
            changed = False
            for state in self._sessions.values():
                if state.get("pending") is not None:
                    state["pending"] = None
                    changed = True
            if changed:
                async with self._state_lock:
                    await self._persist_state()
            logger.info(
                "%s Scheduling is disabled; persisted pending jobs were cleared=%s",
                LOG_PREFIX,
                changed,
            )
            return

        restored_state_changed = False
        restored_timers = 0
        for umo, state in list(self._sessions.items()):
            pending = state.get("pending")
            if not isinstance(pending, dict):
                continue
            if self.config.get("private_only", True) and not state.get(
                "is_private", False
            ):
                state["pending"] = None
                restored_state_changed = True
                continue
            due_at = pending.get("due_at")
            revision = pending.get("revision")
            if isinstance(due_at, (int, float)) and isinstance(revision, int):
                self._start_timer(umo, revision, float(due_at))
                restored_timers += 1
        if restored_state_changed:
            async with self._state_lock:
                await self._persist_state()
        logger.info(
            "%s Restore completed: timers=%s discarded_non_private=%s",
            LOG_PREFIX,
            restored_timers,
            restored_state_changed,
        )

    def _is_eligible(self, event: AstrMessageEvent) -> bool:
        """判断当前事件是否应启用主动续聊处理。

        Args:
            event: 收到的 AstrBot 消息事件。

        Returns:
            当前会话需要由插件处理时返回 `True`。
        """
        if not self.config.get("enabled", True):
            return False
        if max(0, int(self.config.get("daily_limit", 3))) == 0:
            return False
        return not self.config.get("private_only", True) or event.is_private_chat()

    async def _persist_state(self) -> None:
        """持久化当前可序列化为 JSON 的会话状态。

        调用方必须先持有 `_state_lock`，避免消息事件与定时任务并发更新时
        相互覆盖状态。
        """
        await self.put_kv_data(STATE_KEY, {"sessions": self._sessions})

    def _cancel_timer(self, umo: str) -> None:
        """取消指定会话当前存在的内存定时任务。

        Args:
            umo: 用于标识会话的统一消息来源。
        """
        task = self._tasks.pop(umo, None)
        if task and not task.done():
            task.cancel()

    def _start_timer(self, umo: str, revision: int, due_at: float) -> None:
        """用最新决策对应的任务替换当前会话定时器。

        Args:
            umo: 用于标识会话的统一消息来源。
            revision: 生成本次调度决策时的用户消息版本号。
            due_at: 主动消息应发送时的 Unix 时间戳。
        """
        self._cancel_timer(umo)
        self._tasks[umo] = asyncio.create_task(
            self._wait_and_send(umo, revision, due_at),
            name="smart-followup-timer",
        )
        logger.info(
            "%s Timer started: session=%s revision=%s due_in=%.1fs",
            LOG_PREFIX,
            umo,
            revision,
            max(0.0, due_at - time.time()),
        )

    async def _wait_and_send(self, umo: str, revision: int, due_at: float) -> None:
        """等待发送时间，丢弃过期任务，并发送已保存的消息。

        Args:
            umo: 用于标识目标会话的统一消息来源。
            revision: 用于识别过期任务的预期用户消息版本号。
            due_at: 消息应发送时的 Unix 时间戳。
        """
        try:
            logger.info(
                "%s Timer waiting: session=%s revision=%s due_at=%s",
                LOG_PREFIX,
                umo,
                revision,
                datetime.fromtimestamp(due_at).astimezone().isoformat(
                    timespec="seconds"
                ),
            )
            await asyncio.sleep(max(0.0, due_at - time.time()))

            async with self._state_lock:
                state = self._sessions.get(umo)
                pending = state.get("pending") if state else None
                if (
                    not state
                    or state.get("revision") != revision
                    or not isinstance(pending, dict)
                    or pending.get("revision") != revision
                    or float(pending.get("due_at", -1)) != due_at
                ):
                    logger.info(
                        "%s Stale timer discarded: session=%s revision=%s",
                        LOG_PREFIX,
                        umo,
                        revision,
                    )
                    return

                today = datetime.now().astimezone().date().isoformat()
                if state.get("daily_date") != today:
                    state["daily_date"] = today
                    state["daily_count"] = 0
                if int(state.get("daily_count", 0)) >= max(
                    0, int(self.config.get("daily_limit", 3))
                ):
                    state["pending"] = None
                    await self._persist_state()
                    logger.info(
                        "%s Daily limit reached; pending message discarded: "
                        "session=%s count=%s",
                        LOG_PREFIX,
                        umo,
                        state.get("daily_count", 0),
                    )
                    return

                message = str(pending.get("message", "")).strip()
                state["pending"] = None
                await self._persist_state()

            if not message:
                logger.warning(
                    "%s Empty proactive message discarded: session=%s",
                    LOG_PREFIX,
                    umo,
                )
                return

            logger.info(
                "%s Timer due; sending proactive message: session=%s length=%s",
                LOG_PREFIX,
                umo,
                len(message),
            )
            sent = await self.context.send_message(umo, MessageChain().message(message))
            if not sent:
                logger.warning(
                    "%s Platform does not support proactive send: session=%s",
                    LOG_PREFIX,
                    umo,
                )
                return

            async with self._state_lock:
                state = self._sessions.get(umo)
                if state:
                    today = datetime.now().astimezone().date().isoformat()
                    if state.get("daily_date") != today:
                        state["daily_date"] = today
                        state["daily_count"] = 0
                    state["daily_count"] = int(state.get("daily_count", 0)) + 1
                    state["last_proactive"] = {
                        "revision": revision,
                        "sent_at": time.time(),
                        "message": message,
                    }
                    await self._persist_state()
            logger.info(
                "%s Proactive message sent successfully: session=%s",
                LOG_PREFIX,
                umo,
            )
        except asyncio.CancelledError:
            logger.info(
                "%s Timer cancelled: session=%s revision=%s",
                LOG_PREFIX,
                umo,
                revision,
            )
            raise
        except Exception:
            logger.exception(
                "%s Failed to send proactive message: session=%s revision=%s",
                LOG_PREFIX,
                umo,
                revision,
            )
        finally:
            current_task = asyncio.current_task()
            if self._tasks.get(umo) is current_task:
                self._tasks.pop(umo, None)

    def _extract_decision(self, text: str) -> tuple[str, dict[str, Any] | None]:
        """移除控制块，并校验模型给出的调度决策。

        Args:
            text: LLM 返回的完整文本。

        Returns:
            包含用户可见文本和已校验决策的二元组。模型选择不续聊或输出
            无效元数据时，决策为 `None`。
        """
        pattern = re.compile(
            rf"{re.escape(CONTROL_TAG_START)}([\s\S]*?){re.escape(CONTROL_TAG_END)}"
        )
        matches = list(pattern.finditer(text or ""))
        clean_text = pattern.sub("", text or "")

        # 即使末尾控制块格式损坏，也不能让它泄漏到用户可见回复中。
        if CONTROL_TAG_START in clean_text:
            clean_text = clean_text.split(CONTROL_TAG_START, 1)[0]
        clean_text = clean_text.replace(CONTROL_TAG_END, "").strip()
        if not matches:
            return clean_text, None

        raw_payload = matches[-1].group(1).strip()
        if raw_payload.startswith("```json"):
            raw_payload = raw_payload[7:]
        elif raw_payload.startswith("```"):
            raw_payload = raw_payload[3:]
        if raw_payload.endswith("```"):
            raw_payload = raw_payload[:-3]

        try:
            payload = json.loads(raw_payload.strip())
        except (json.JSONDecodeError, TypeError):
            logger.warning("%s Ignored malformed model control block", LOG_PREFIX)
            return clean_text, None

        if not isinstance(payload, dict) or payload.get("action") != "schedule":
            return clean_text, None

        after_seconds = payload.get("after_seconds")
        message = payload.get("message")
        if (
            isinstance(after_seconds, bool)
            or not isinstance(after_seconds, (int, float))
            or not isinstance(message, str)
            or not message.strip()
        ):
            return clean_text, None

        minimum = max(1, int(self.config.get("min_delay_seconds", 30)))
        maximum = max(minimum, int(self.config.get("max_delay_seconds", 86400)))
        delay = min(maximum, max(minimum, int(after_seconds)))
        max_length = max(1, int(self.config.get("max_message_length", 300)))
        return clean_text, {
            "action": "schedule",
            "after_seconds": delay,
            "message": message.strip()[:max_length],
        }

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100000)
    async def record_user_activity(self, event: AstrMessageEvent) -> None:
        """使旧定时任务失效，并更新近期用户活跃度统计。

        Args:
            event: 新收到的用户消息事件。
        """
        if not self._is_eligible(event):
            logger.debug(
                "%s Incoming message skipped: session=%s private=%s enabled=%s",
                LOG_PREFIX,
                event.unified_msg_origin,
                event.is_private_chat(),
                self.config.get("enabled", True),
            )
            return

        umo = event.unified_msg_origin
        timer_cancelled = umo in self._tasks and not self._tasks[umo].done()
        self._cancel_timer(umo)
        now = time.time()
        async with self._state_lock:
            state = self._sessions.setdefault(
                umo,
                {
                    "revision": 0,
                    "last_user_at": None,
                    "recent_intervals": [],
                    "pending": None,
                    "daily_date": "",
                    "daily_count": 0,
                    "last_proactive": None,
                    "is_private": event.is_private_chat(),
                },
            )
            previous = state.get("last_user_at")
            intervals = state.get("recent_intervals", [])
            if not isinstance(intervals, list):
                intervals = []
            if isinstance(previous, (int, float)):
                intervals.append(max(0, int(now - float(previous))))
            state["recent_intervals"] = intervals[-5:]
            state["last_user_at"] = now
            state["revision"] = int(state.get("revision", 0)) + 1
            state["pending"] = None
            state["updated_at"] = now
            state["is_private"] = event.is_private_chat()
            await self._persist_state()

            logger.info(
                "%s User activity recorded: session=%s revision=%s "
                "old_timer_cancelled=%s recent_intervals=%s",
                LOG_PREFIX,
                umo,
                state["revision"],
                timer_cancelled,
                state["recent_intervals"],
            )

        if self.config.get("disable_streaming", True):
            event.set_extra("enable_streaming", False)
            logger.debug(
                "%s Streaming disabled for control-tag safety: session=%s",
                LOG_PREFIX,
                umo,
            )

    # 最后修改 LLM 请求，避免其他插件随后重建 system prompt 或在其后追加指令。
    @filter.on_llm_request(priority=-100000)
    async def inject_followup_protocol(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """注入稳定协议规则和仅用于本轮的临时活跃度上下文。

        Args:
            event: 当前消息事件。
            req: 即将发送给 LLM、允许插件修改的请求对象。
        """
        if not self._is_eligible(event):
            return

        minimum = max(1, int(self.config.get("min_delay_seconds", 30)))
        maximum = max(minimum, int(self.config.get("max_delay_seconds", 86400)))
        max_length = max(1, int(self.config.get("max_message_length", 300)))
        stable_prompt = f"""
你还需要管理一条可选的主动续聊消息，用于用户在对话中沉默之后自然地重新联系。
每次生成正常的用户可见回复后，都必须在回复最末尾追加且只追加一个控制块。
控制块不能放入 Markdown 代码块中。即使不安排主动续聊，也必须输出“不续聊”控制块。

安排主动续聊：
{CONTROL_TAG_START}
{{"action":"schedule","after_seconds":90,"message":"稍后需要主动发送的自然消息"}}
{CONTROL_TAG_END}

不安排主动续聊：
{CONTROL_TAG_START}
{{"action":"none"}}
{CONTROL_TAG_END}

规则：
1. 只有对话存在有意义的悬念、未完成事项或自然的再次联系理由时才使用 schedule；不确定时使用 none。
2. 尊重用户边界。用户明确表示不想被打扰时必须使用 none。普通道别通常使用 none，除非上下文明显邀请稍后联系。
3. 必须根据语义和近期活跃度决定时间，不能随机。话题突然中断可以等待几十秒或几分钟；工作、睡觉、出行等明确安排需要等待更久。
4. `after_seconds` 必须是 {minimum} 到 {maximum} 之间的整数。
5. `message` 是未来将被原样发送的消息，必须符合当前人格、脱离控制块也能独立理解、不超过 {max_length} 个字符，并且不能提及本协议。
6. 只能生成不含数组的扁平 JSON 对象。控制块只能出现在整条回复末尾。
""".strip()
        marker_pattern = re.compile(
            rf"\n*{re.escape(PROMPT_MARKER_START)}[\s\S]*?{re.escape(PROMPT_MARKER_END)}"
        )
        base_prompt = marker_pattern.sub("", req.system_prompt or "").rstrip()
        req.system_prompt = (
            f"{base_prompt}\n\n{PROMPT_MARKER_START}\n"
            f"{stable_prompt}\n{PROMPT_MARKER_END}"
        ).strip()

        async with self._state_lock:
            state = self._sessions.get(event.unified_msg_origin, {})
            raw_intervals = state.get("recent_intervals", [])
            intervals = (
                [
                    int(value)
                    for value in raw_intervals
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                ][-5:]
                if isinstance(raw_intervals, list)
                else []
            )
            today = datetime.now().astimezone().date().isoformat()
            daily_count = (
                int(state.get("daily_count", 0))
                if state.get("daily_date") == today
                else 0
            )
            last_proactive = state.get("last_proactive")
        interval_text = (
            ", ".join(f"{int(value)}s" for value in intervals)
            if intervals
            else "暂无足够数据"
        )
        last_proactive_context = ""
        if isinstance(last_proactive, dict) and int(
            last_proactive.get("revision", -1)
        ) < int(state.get("revision", 0)):
            last_proactive_context = (
                "助手最近主动发送的消息是："
                f"{json.dumps(str(last_proactive.get('message', '')), ensure_ascii=False)}\n"
                "当前用户消息可能正在回复这条主动消息。\n"
            )
        dynamic_context = (
            "<smart_followup_context>\n"
            f"当前本地时间：{datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            f"近期用户消息间隔（从旧到新）：{interval_text}\n"
            f"今日已发送主动消息：{daily_count}/"
            f"{max(0, int(self.config.get('daily_limit', 3)))}\n"
            f"{last_proactive_context}"
            "这些是私有调度信息，不得在用户可见回复中引用或提及。\n"
            "格式提醒：本轮回复末尾必须输出一个 "
            f"{CONTROL_TAG_START} JSON {CONTROL_TAG_END} 控制块；"
            '不安排时输出 {"action":"none"}。\n'
            "</smart_followup_context>"
        )
        req.extra_user_content_parts.append(
            TextPart(text=dynamic_context).mark_as_temp()
        )
        logger.info(
            "%s LLM protocol injected as final request hook: session=%s "
            "revision=%s intervals=%s daily_count=%s bridged_proactive=%s "
            "system_prompt_length=%s extra_parts=%s",
            LOG_PREFIX,
            event.unified_msg_origin,
            state.get("revision", 0),
            intervals,
            daily_count,
            bool(last_proactive_context),
            len(req.system_prompt),
            len(req.extra_user_content_parts),
        )

    @filter.on_llm_response(priority=100000)
    async def parse_followup_decision(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """清理模型控制元数据，并把已校验的决策附加到事件。

        Args:
            event: 当前消息事件。
            response: 包含最终生成文本、允许插件修改的 LLM 响应。
        """
        if not self._is_eligible(event) or not response.completion_text:
            if self._is_eligible(event):
                logger.warning(
                    "%s Empty LLM response; no follow-up decision available: session=%s",
                    LOG_PREFIX,
                    event.unified_msg_origin,
                )
            return

        had_control_block = (
            CONTROL_TAG_START in response.completion_text
            and CONTROL_TAG_END in response.completion_text
        )
        clean_text, decision = self._extract_decision(response.completion_text)
        response.completion_text = clean_text
        if not decision:
            event.set_extra(EVENT_DECISION_KEY, None)
            if had_control_block:
                logger.info(
                    "%s Model chose no proactive follow-up: session=%s",
                    LOG_PREFIX,
                    event.unified_msg_origin,
                )
            else:
                logger.warning(
                    "%s Model returned no valid control block; no follow-up scheduled: "
                    "session=%s",
                    LOG_PREFIX,
                    event.unified_msg_origin,
                )
            return

        async with self._state_lock:
            state = self._sessions.get(event.unified_msg_origin, {})
            today = datetime.now().astimezone().date().isoformat()
            daily_count = (
                int(state.get("daily_count", 0))
                if state.get("daily_date") == today
                else 0
            )
            if daily_count >= max(0, int(self.config.get("daily_limit", 3))):
                event.set_extra(EVENT_DECISION_KEY, None)
                logger.info(
                    "%s Model scheduled a follow-up but the daily limit was reached: "
                    "session=%s count=%s",
                    LOG_PREFIX,
                    event.unified_msg_origin,
                    daily_count,
                )
                return
            decision["revision"] = int(state.get("revision", 0))
        event.set_extra(EVENT_DECISION_KEY, decision)
        logger.info(
            "%s Model scheduled a proactive follow-up: session=%s revision=%s "
            "delay=%ss message_length=%s",
            LOG_PREFIX,
            event.unified_msg_origin,
            decision["revision"],
            decision["after_seconds"],
            len(decision["message"]),
        )

    @filter.after_message_sent(priority=100000)
    async def schedule_after_reply(self, event: AstrMessageEvent) -> None:
        """仅在当前回复发送成功后持久化决策并启动定时任务。

        Args:
            event: 回复刚刚发送完成的消息事件。
        """
        decision = event.get_extra(EVENT_DECISION_KEY)
        event.set_extra(EVENT_DECISION_KEY, None)
        if not self._is_eligible(event):
            return

        umo = event.unified_msg_origin
        async with self._state_lock:
            state = self._sessions.get(umo)
            last_proactive = state.get("last_proactive") if state else None
            if (
                state
                and isinstance(last_proactive, dict)
                and int(last_proactive.get("revision", -1))
                < int(state.get("revision", 0))
            ):
                state["last_proactive"] = None
                await self._persist_state()

        if not isinstance(decision, dict):
            logger.info(
                "%s Reply sent with no pending proactive follow-up: session=%s",
                LOG_PREFIX,
                umo,
            )
            return

        revision = decision.get("revision")
        if not isinstance(revision, int):
            logger.warning(
                "%s Invalid follow-up revision discarded: session=%s",
                LOG_PREFIX,
                umo,
            )
            return
        due_at = time.time() + int(decision["after_seconds"])

        async with self._state_lock:
            state = self._sessions.get(umo)
            if not state or state.get("revision") != revision:
                logger.info(
                    "%s Follow-up decision became stale before scheduling: "
                    "session=%s decision_revision=%s current_revision=%s",
                    LOG_PREFIX,
                    umo,
                    revision,
                    state.get("revision") if state else None,
                )
                return
            state["pending"] = {
                "revision": revision,
                "due_at": due_at,
                "message": decision["message"],
            }
            state["updated_at"] = time.time()
            await self._persist_state()

        self._start_timer(umo, revision, due_at)
        logger.info(
            "%s Proactive follow-up persisted and scheduled: session=%s "
            "revision=%s delay=%ss",
            LOG_PREFIX,
            umo,
            revision,
            decision["after_seconds"],
        )

    async def terminate(self) -> None:
        """插件关闭时持久化状态，并取消全部内存定时任务。"""
        task_count = len(self._tasks)
        for umo in list(self._tasks):
            self._cancel_timer(umo)
        async with self._state_lock:
            await self._persist_state()
        logger.info(
            "%s Plugin terminated: cancelled_timers=%s persisted_sessions=%s",
            LOG_PREFIX,
            task_count,
            len(self._sessions),
        )
