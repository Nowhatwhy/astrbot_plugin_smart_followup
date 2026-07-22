import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.message_session import MessageSession

PROMPT_MARKER_START = "<!-- smart_followup_prompt:start -->"
PROMPT_MARKER_END = "<!-- smart_followup_prompt:end -->"
CONTROL_TAG_PREFIX = "<<SMART_FOLLOWUP|"
CONTROL_TAG_END = ">>"
STATE_KEY = "session_state_v2"
EVENT_DECISION_KEY = "smart_followup_decision"
WAKE_EVENT_KEY = "smart_followup_wake_revision"
WAKE_PROMPT_KEY = "smart_followup_wake_prompt"
WAKE_TRIGGER = "<<SMART_FOLLOWUP_WAKE>>"
LOG_PREFIX = "[smart_followup]"

# 配置 schema 是管理员可见默认值的唯一来源，避免 Python 与 JSON 各维护一份。
with (
    Path(__file__).with_name("_conf_schema.json").open(encoding="utf-8") as schema_file
):
    _CONFIG_SCHEMA = json.load(schema_file)
DEFAULT_MAX_DELAY_SECONDS = int(_CONFIG_SCHEMA["max_delay_seconds"]["default"])
DEFAULT_DECISION_PROMPT = str(_CONFIG_SCHEMA["decision_prompt"]["default"]).strip()
DEFAULT_WAKE_PROMPT = str(_CONFIG_SCHEMA["wake_prompt"]["default"]).strip()
del _CONFIG_SCHEMA


class SmartFollowupPlugin(Star):
    """在用户沉默后安排 AstrBot Agent 延迟主动回复。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        """初始化插件状态。

        Args:
            context: AstrBot 插件上下文。
            config: 根据 `_conf_schema.json` 生成的插件配置。
        """
        super().__init__(context, config)
        self.config = config
        self._sessions: dict[str, dict[str, Any]] = {}
        self._state_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task] = {}

    async def initialize(self) -> None:
        """恢复插件状态，并重新创建尚未到期的本地等待任务。"""
        stored = await self.get_kv_data(STATE_KEY, {})
        if isinstance(stored, dict):
            sessions = stored.get("sessions", {})
            if isinstance(sessions, dict):
                self._sessions = {
                    str(umo): state
                    for umo, state in sessions.items()
                    if isinstance(state, dict)
                }

        enabled = (
            self.config.get("enabled", True)
            and max(0, int(self.config.get("daily_limit", 3))) > 0
        )
        cron_manager = getattr(self.context, "cron_manager", None)
        state_changed = False
        for umo, state in self._sessions.items():
            if "last_user_at" in state:
                state.pop("last_user_at")
                state_changed = True
            if "recent_intervals" in state:
                state.pop("recent_intervals")
                state_changed = True

            pending = state.get("pending")
            if not isinstance(pending, dict):
                if pending is not None:
                    state["pending"] = None
                    state_changed = True
                continue

            # v0.2 使用核心 Cron 主动 Agent；升级时必须删除，避免旧路径改写
            # system prompt 并破坏长上下文缓存。
            job_id = pending.get("job_id")
            if job_id:
                if cron_manager:
                    try:
                        await cron_manager.delete_job(str(job_id))
                    except Exception:
                        logger.exception(
                            "%s Failed to delete legacy Cron job: job_id=%s",
                            LOG_PREFIX,
                            job_id,
                        )
                state["pending"] = None
                state_changed = True
                continue

            try:
                run_at = datetime.fromisoformat(str(pending.get("run_at")))
                revision = int(pending["revision"])
            except (KeyError, TypeError, ValueError):
                state["pending"] = None
                state_changed = True
                continue
            if not enabled or run_at.timestamp() <= time.time():
                state["pending"] = None
                state_changed = True
                continue

            wake_prompt = str(pending.get("wake_prompt") or DEFAULT_WAKE_PROMPT).strip()

            self._tasks[umo] = asyncio.create_task(
                self._wake_after(
                    umo=umo,
                    revision=revision,
                    run_at=run_at,
                    wake_prompt=wake_prompt,
                    self_id=str(pending.get("self_id") or "astrbot"),
                )
            )

        if state_changed:
            async with self._state_lock:
                await self._persist_state()

    def _is_eligible(self, event: AstrMessageEvent) -> bool:
        """判断当前消息事件是否启用主动续聊。

        Args:
            event: AstrBot 消息事件。

        Returns:
            当前会话需要由插件处理时返回 `True`。
        """
        if not self.config.get("enabled", True):
            return False
        if max(0, int(self.config.get("daily_limit", 3))) == 0:
            return False
        return not self.config.get("private_only", True) or event.is_private_chat()

    async def _persist_state(self) -> None:
        """持久化当前会话状态。

        调用方必须先持有 `_state_lock`，避免并发更新互相覆盖。
        """
        await self.put_kv_data(STATE_KEY, {"sessions": self._sessions})

    async def _wake_after(
        self,
        *,
        umo: str,
        revision: int,
        run_at: datetime,
        wake_prompt: str,
        self_id: str,
    ) -> None:
        """等待到期后向普通消息管线投递一次主动回复事件。

        Args:
            umo: 原会话的统一消息来源。
            revision: 安排任务时的会话版本号。
            run_at: 计划唤醒时间。
            wake_prompt: 追加在历史末尾的临时用户级指令。
            self_id: 原平台机器人的账号 ID。
        """
        try:
            await asyncio.sleep(max(0, run_at.timestamp() - time.time()))
            async with self._state_lock:
                state = self._sessions.get(umo)
                pending = state.get("pending") if state else None
                if (
                    not state
                    or state.get("revision") != revision
                    or not isinstance(pending, dict)
                    or pending.get("revision") != revision
                ):
                    return
                state["pending"] = None
                state["updated_at"] = time.time()
                await self._persist_state()
                self._tasks.pop(umo, None)

            session = MessageSession.from_str(umo)
            wake_event = CronMessageEvent(
                context=self.context,
                session=session,
                message=WAKE_TRIGGER,
                sender_id=self_id,
                sender_name="Smart Follow-up",
                message_type=session.message_type,
            )
            platform = self.context.get_platform_inst(session.platform_id)
            if platform:
                # 保持平台能力和工具集合与普通对话一致，避免请求结构发生变化。
                wake_event.platform_meta = platform.meta()
            wake_event.set_extra(WAKE_EVENT_KEY, revision)
            wake_event.set_extra(WAKE_PROMPT_KEY, wake_prompt)
            wake_event.set_extra("enable_streaming", False)
            self.context.get_event_queue().put_nowait(wake_event)
            if self.config.get("debug_full_payload", False):
                logger.info(
                    "%s Wake event queued: session=%s revision=%s",
                    LOG_PREFIX,
                    umo,
                    revision,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "%s Failed to queue local wake event: session=%s revision=%s",
                LOG_PREFIX,
                umo,
                revision,
            )
        finally:
            if self._tasks.get(umo) is asyncio.current_task():
                self._tasks.pop(umo, None)

    def _extract_decision(self, text: str) -> tuple[str, dict[str, int] | None]:
        """清理控制标记并解析等待秒数。

        Args:
            text: LLM 返回的完整文本。

        Returns:
            用户可见文本和已校验调度决策组成的二元组。没有调度时决策为
            `None`。
        """
        compact_pattern = re.compile(
            rf"{re.escape(CONTROL_TAG_PREFIX)}([\s\S]*?){re.escape(CONTROL_TAG_END)}"
        )
        legacy_pattern = re.compile(
            r"<astrbot_smart_followup>[\s\S]*?</astrbot_smart_followup>"
        )
        compact_matches = list(compact_pattern.finditer(text or ""))
        fenced_compact_pattern = re.compile(
            rf"```(?:json|text)?\s*{compact_pattern.pattern}\s*```",
            re.IGNORECASE,
        )
        fenced_legacy_pattern = re.compile(
            rf"```(?:json|text)?\s*{legacy_pattern.pattern}\s*```",
            re.IGNORECASE,
        )
        clean_text = fenced_compact_pattern.sub("", text or "")
        clean_text = fenced_legacy_pattern.sub("", clean_text)
        clean_text = compact_pattern.sub("", clean_text)
        clean_text = legacy_pattern.sub("", clean_text)

        # 格式损坏的尾部标记也不能泄漏给用户。
        if CONTROL_TAG_PREFIX in clean_text:
            clean_text = clean_text.split(CONTROL_TAG_PREFIX, 1)[0]
        clean_text = clean_text.strip()
        if not compact_matches:
            return clean_text, None

        payload = compact_matches[-1].group(1).strip()
        if payload.upper() == "NEVER":
            return clean_text, {"never": 1}
        try:
            after_seconds = int(payload)
        except ValueError:
            if self.config.get("debug_full_payload", False):
                logger.warning("%s Ignored malformed follow-up marker", LOG_PREFIX)
            return clean_text, None
        minimum = max(1, int(self.config.get("min_delay_seconds", 30)))
        maximum = max(
            minimum,
            int(self.config.get("max_delay_seconds", DEFAULT_MAX_DELAY_SECONDS)),
        )
        return clean_text, {
            "after_seconds": min(maximum, max(minimum, int(after_seconds)))
        }

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100000)
    async def record_user_activity(self, event: AstrMessageEvent) -> None:
        """记录用户活动，并取消当前会话的旧等待任务。

        Args:
            event: 新收到的用户消息事件。
        """
        wake_revision = event.get_extra(WAKE_EVENT_KEY)
        if isinstance(wake_revision, int):
            if not self._is_eligible(event):
                event.stop_event()
                return
            async with self._state_lock:
                current_revision = int(
                    self._sessions.get(event.unified_msg_origin, {}).get("revision", -1)
                )
            if current_revision != wake_revision:
                event.stop_event()
                if self.config.get("debug_full_payload", False):
                    logger.info(
                        "%s Stale wake event stopped: session=%s "
                        "scheduled_revision=%s current_revision=%s",
                        LOG_PREFIX,
                        event.unified_msg_origin,
                        wake_revision,
                        current_revision,
                    )
                return
            event.set_extra("enable_streaming", False)
            return

        if not self._is_eligible(event):
            return

        umo = event.unified_msg_origin
        now = time.time()
        async with self._state_lock:
            current_state = self._sessions.get(umo, {})
            pending = current_state.get("pending")
            pending_run_at = (
                pending.get("run_at") if isinstance(pending, dict) else None
            )

        pending_is_waiting = isinstance(pending, dict)
        if pending_is_waiting and pending_run_at:
            try:
                pending_is_waiting = (
                    datetime.fromisoformat(str(pending_run_at)).timestamp() > now
                )
            except ValueError:
                pending_is_waiting = True

        task = self._tasks.pop(umo, None)
        if task and not task.done():
            task.cancel()
            if self.config.get("debug_full_payload", False):
                logger.info(
                    "%s Pending wake cancelled by user message: session=%s",
                    LOG_PREFIX,
                    umo,
                )

        async with self._state_lock:
            state = self._sessions.setdefault(
                umo,
                {
                    "revision": 0,
                    "pending": None,
                    "daily_date": "",
                    "daily_count": 0,
                    "is_private": event.is_private_chat(),
                },
            )
            state["revision"] = int(state.get("revision", 0)) + 1
            state["pending"] = None
            state["updated_at"] = now
            state["is_private"] = event.is_private_chat()
            today = datetime.now().astimezone().date().isoformat()
            if pending_is_waiting and state.get("daily_date") == today:
                state["daily_count"] = max(0, int(state.get("daily_count", 0)) - 1)
            await self._persist_state()

        if self.config.get("disable_streaming", True):
            event.set_extra("enable_streaming", False)

    # 最后修改请求，避免后续插件覆盖稳定规则或在动态上下文之后追加内容。
    @filter.on_llm_request(priority=-100000)
    async def inject_followup_protocol(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """向 system prompt 注入稳定规则，并追加临时运行数据。

        Args:
            event: 当前消息事件。
            req: 即将发送给 LLM 的可变请求对象。
        """
        if not self._is_eligible(event):
            return

        wake_revision = event.get_extra(WAKE_EVENT_KEY)
        if isinstance(wake_revision, int):
            async with self._state_lock:
                current_revision = int(
                    self._sessions.get(event.unified_msg_origin, {}).get("revision", -1)
                )
            if current_revision != wake_revision:
                event.stop_event()
                return

        stable_prompt = str(
            self.config.get("decision_prompt", DEFAULT_DECISION_PROMPT)
            or DEFAULT_DECISION_PROMPT
        ).strip()
        marker_pattern = re.compile(
            rf"\n*{re.escape(PROMPT_MARKER_START)}[\s\S]*?{re.escape(PROMPT_MARKER_END)}"
        )
        base_prompt = marker_pattern.sub("", req.system_prompt or "").rstrip()
        req.system_prompt = (
            f"{base_prompt}\n\n{PROMPT_MARKER_START}\n"
            f"{stable_prompt}\n{PROMPT_MARKER_END}"
        ).strip()

        dynamic_context = (
            "<smart_followup_context>\n"
            f"当前本地时间：{datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            "</smart_followup_context>"
        )
        if isinstance(wake_revision, int):
            # 唤醒指令作为历史末尾的临时 user 消息参与推理。system prompt、历史
            # 前缀和平台工具结构均与普通对话保持一致。
            wake_prompt = str(
                event.get_extra(WAKE_PROMPT_KEY) or DEFAULT_WAKE_PROMPT
            ).strip()
            temporary_parts: list[dict[str, Any]] = [
                {"type": "text", "text": wake_prompt}
            ]
            for part in req.extra_user_content_parts:
                if hasattr(part, "model_dump_for_context"):
                    part_data = part.model_dump_for_context()
                elif isinstance(part, dict):
                    part_data = dict(part)
                else:
                    continue
                part_data.pop("_no_save", None)
                temporary_parts.append(part_data)
            temporary_parts.append({"type": "text", "text": dynamic_context})
            req.contexts.append(
                {
                    "role": "user",
                    "content": temporary_parts,
                    "_no_save": True,
                }
            )
            req.prompt = None
            req.extra_user_content_parts.clear()
        else:
            req.extra_user_content_parts.append(
                TextPart(text=dynamic_context).mark_as_temp()
            )

        if self.config.get("debug_full_payload", False):
            logger.info(
                "%s ===== SMART FOLLOWUP LLM REQUEST =====\n"
                "session=%s\n"
                "----- SYSTEM PROMPT -----\n%s\n"
                "----- USER PROMPT -----\n%s\n"
                "----- SMART FOLLOWUP TEMPORARY CONTEXT -----\n%s\n"
                "history_count=%s extra_parts_count=%s\n"
                "===== SMART FOLLOWUP LLM REQUEST END =====",
                LOG_PREFIX,
                event.unified_msg_origin,
                req.system_prompt,
                req.prompt,
                dynamic_context,
                len(req.contexts),
                len(req.extra_user_content_parts),
            )

    @filter.on_llm_response(priority=100000)
    async def parse_followup_decision(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """清理模型标记，并把等待时间附加到当前事件。

        Args:
            event: 当前消息事件。
            response: 当前 LLM 响应。
        """
        if not self._is_eligible(event):
            return

        is_wake = isinstance(event.get_extra(WAKE_EVENT_KEY), int)
        if not response.completion_text:
            if is_wake:
                logger.error(
                    "%s Wake Agent unexpectedly returned an empty response: session=%s",
                    LOG_PREFIX,
                    event.unified_msg_origin,
                )
            return

        if self.config.get("debug_full_payload", False):
            logger.info(
                "%s ===== SMART FOLLOWUP LLM RESPONSE =====\n"
                "session=%s\n"
                "----- COMPLETION TEXT -----\n%s\n"
                "----- REASONING CONTENT -----\n%s\n"
                "===== SMART FOLLOWUP LLM RESPONSE END =====",
                LOG_PREFIX,
                event.unified_msg_origin,
                response.completion_text,
                response.reasoning_content,
            )

        if is_wake:
            wake_revision = event.get_extra(WAKE_EVENT_KEY)
            async with self._state_lock:
                current_revision = int(
                    self._sessions.get(event.unified_msg_origin, {}).get("revision", -1)
                )
            if current_revision != wake_revision:
                response.completion_text = ""
                event.set_extra(EVENT_DECISION_KEY, None)
                if self.config.get("debug_full_payload", False):
                    logger.info(
                        "%s Wake reply suppressed after new user message: session=%s",
                        LOG_PREFIX,
                        event.unified_msg_origin,
                    )
                return

        had_control_marker = (
            CONTROL_TAG_PREFIX in response.completion_text
            and CONTROL_TAG_END in response.completion_text
        )
        clean_text, decision = self._extract_decision(response.completion_text)
        response.completion_text = clean_text
        if not had_control_marker and response.reasoning_content:
            reasoning_had_marker = (
                CONTROL_TAG_PREFIX in response.reasoning_content
                and CONTROL_TAG_END in response.reasoning_content
            )
            clean_reasoning, reasoning_decision = self._extract_decision(
                response.reasoning_content
            )
            response.reasoning_content = clean_reasoning
            if reasoning_had_marker:
                had_control_marker = True
                decision = reasoning_decision

        if decision and decision.get("never") == 1:
            event.set_extra(EVENT_DECISION_KEY, None)
            return

        if not decision:
            decision = {
                "after_seconds": max(
                    max(1, int(self.config.get("min_delay_seconds", 30))),
                    int(
                        self.config.get("max_delay_seconds", DEFAULT_MAX_DELAY_SECONDS)
                    ),
                )
            }
            if self.config.get("debug_full_payload", False):
                logger.warning(
                    "%s Missing next-contact marker; using maximum delay: "
                    "session=%s delay=%ss",
                    LOG_PREFIX,
                    event.unified_msg_origin,
                    decision["after_seconds"],
                )

        async with self._state_lock:
            state = self._sessions.get(event.unified_msg_origin, {})
            now = datetime.now().astimezone()
            today = now.date().isoformat()
            daily_count = (
                int(state.get("daily_count", 0))
                if state.get("daily_date") == today
                else 0
            )
            daily_limit = max(0, int(self.config.get("daily_limit", 3)))
            if daily_count >= daily_limit:
                next_day = datetime.combine(
                    now.date() + timedelta(days=1),
                    datetime.min.time(),
                    tzinfo=now.tzinfo,
                )
                deferred_seconds = max(1, int((next_day - now).total_seconds()) + 1)
                decision["after_seconds"] = max(
                    decision["after_seconds"], deferred_seconds
                )
            decision["revision"] = (
                int(event.get_extra(WAKE_EVENT_KEY))
                if is_wake
                else int(state.get("revision", 0))
            )
        event.set_extra(EVENT_DECISION_KEY, decision)

    @filter.after_message_sent(priority=100000)
    async def schedule_after_reply(self, event: AstrMessageEvent) -> None:
        """在当前回复发送后创建一次性本地等待任务。

        Args:
            event: 回复已经发送完成的消息事件。
        """
        decision = event.get_extra(EVENT_DECISION_KEY)
        event.set_extra(EVENT_DECISION_KEY, None)
        if not self._is_eligible(event) or not isinstance(decision, dict):
            return

        revision = decision.get("revision")
        after_seconds = decision.get("after_seconds")
        if not isinstance(revision, int) or not isinstance(after_seconds, int):
            return

        umo = event.unified_msg_origin
        run_at = datetime.now().astimezone() + timedelta(seconds=after_seconds)
        wake_prompt = str(
            self.config.get("wake_prompt", DEFAULT_WAKE_PROMPT) or DEFAULT_WAKE_PROMPT
        ).strip()
        self_id = event.get_self_id() or "astrbot"

        async with self._state_lock:
            state = self._sessions.get(umo)
            if not state or state.get("revision") != revision:
                return
            today = datetime.now().astimezone().date().isoformat()
            if state.get("daily_date") != today:
                state["daily_date"] = today
                state["daily_count"] = 0
            state["daily_count"] = int(state.get("daily_count", 0)) + 1
            state["pending"] = {
                "revision": revision,
                "run_at": run_at.isoformat(),
                "wake_prompt": wake_prompt,
                "self_id": self_id,
            }
            state["updated_at"] = time.time()
            await self._persist_state()
            old_task = self._tasks.pop(umo, None)
            if old_task and not old_task.done():
                old_task.cancel()
            self._tasks[umo] = asyncio.create_task(
                self._wake_after(
                    umo=umo,
                    revision=revision,
                    run_at=run_at,
                    wake_prompt=wake_prompt,
                    self_id=self_id,
                )
            )

        if self.config.get("debug_full_payload", False):
            logger.info(
                "%s Wake scheduled: session=%s delay=%ss run_at=%s",
                LOG_PREFIX,
                umo,
                after_seconds,
                run_at.isoformat(timespec="seconds"),
            )

    async def terminate(self) -> None:
        """插件关闭时取消内存任务并保留待恢复的持久化状态。"""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._state_lock:
            await self._persist_state()
