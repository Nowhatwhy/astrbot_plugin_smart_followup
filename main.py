import asyncio
import re
from datetime import datetime
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.message_session import MessageSession

PROMPT_START = "<!-- smart_followup_prompt:start -->"
PROMPT_END = "<!-- smart_followup_prompt:end -->"
PROMPT_PATTERN = re.compile(
    rf"\n*{re.escape(PROMPT_START)}[\s\S]*?{re.escape(PROMPT_END)}"
)
VALID_TAG_PATTERN = re.compile(r"<<SMART_FOLLOWUP\|(NEVER|[1-9]\d*)>>", re.I)

EVENT_REVISION = "smart_followup_revision"
EVENT_REQUEST = "smart_followup_request"
EVENT_DECISION = "smart_followup_decision"
WAKE_EVENT = "smart_followup_wake"
WAKE_PROMPT = "smart_followup_wake_prompt"
LOG_PREFIX = "[smart_followup]"


class SmartFollowupPlugin(Star):
    """在用户沉默后重新唤醒当前会话的 Agent。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        """初始化会话版本和计时任务。

        Args:
            context: AstrBot 插件上下文。
            config: 插件配置。
        """
        super().__init__(context, config)
        self.config = config
        self._revisions: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def _eligible(self, event: AstrMessageEvent) -> bool:
        """判断事件是否应由插件处理。

        Args:
            event: 当前消息事件。

        Returns:
            插件应处理该事件时返回 `True`。
        """
        return not self.config["private_only"] or event.is_private_chat()

    @staticmethod
    def _parse_text(text: str) -> tuple[str, int | str | None]:
        """删除调度标签并读取最后一个有效值。

        Args:
            text: 模型返回的文本。

        Returns:
            清理后的文本，以及正整数秒数、`NEVER` 或 `None`。
        """
        matches = list(VALID_TAG_PATTERN.finditer(text))
        clean_text = VALID_TAG_PATTERN.sub("", text).strip()
        if not matches:
            return clean_text, None
        value = matches[-1].group(1).upper()
        return clean_text, value if value == "NEVER" else int(value)

    def _parse_response(self, response: LLMResponse) -> int | str | None:
        """从正文或思考内容读取调度决定并清除标签。

        Args:
            response: 模型响应。

        Returns:
            正整数秒数、`NEVER` 或 `None`。
        """
        response.completion_text, decision = self._parse_text(
            response.completion_text or ""
        )
        if decision is None and response.reasoning_content:
            response.reasoning_content, decision = self._parse_text(
                response.reasoning_content
            )
        return decision

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100000)
    async def record_message(self, event: AstrMessageEvent) -> None:
        """为用户消息生成新版本，并取消同会话旧计时器。

        Args:
            event: 当前消息事件。
        """
        if not self._eligible(event):
            if event.get_extra(WAKE_EVENT):
                event.stop_event()
            return

        umo = event.unified_msg_origin
        wake_revision = event.get_extra(WAKE_EVENT)
        if isinstance(wake_revision, int):
            if self._revisions.get(umo) != wake_revision:
                event.stop_event()
                return
            event.set_extra(EVENT_REVISION, wake_revision)
            event.set_extra("enable_streaming", False)
            return

        if (
            not event.get_message_str().strip()
            and not event.get_message_outline().strip()
        ):
            return

        task = self._tasks.pop(umo, None)
        if task and not task.done():
            task.cancel()

        revision = self._revisions.get(umo, 0) + 1
        self._revisions[umo] = revision
        event.set_extra(EVENT_REVISION, revision)
        event.set_extra("enable_streaming", False)

    @filter.on_llm_request(priority=-100000)
    async def inject_prompt(
        self, event: AstrMessageEvent, request: ProviderRequest
    ) -> None:
        """注入稳定规则和本轮临时上下文。

        Args:
            event: 当前消息事件。
            request: 即将发送给模型的请求。
        """
        if not self._eligible(event):
            return

        revision = event.get_extra(EVENT_REVISION)
        if not isinstance(revision, int):
            return
        if self._revisions.get(event.unified_msg_origin) != revision:
            event.stop_event()
            return

        system_prompt = PROMPT_PATTERN.sub("", request.system_prompt or "").rstrip()
        request.system_prompt = (
            f"{system_prompt}\n\n{PROMPT_START}\n"
            f"{str(self.config['decision_prompt']).strip()}\n{PROMPT_END}"
        ).strip()

        temporary_text = (
            "<smart_followup_context>\n"
            f"当前本地时间：{datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            f"{str(self.config['user_prompt_reminder']).strip()}\n"
            "</smart_followup_context>"
        )

        if event.get_extra(WAKE_EVENT):
            parts: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": str(
                        request.prompt
                        or event.get_extra(WAKE_PROMPT)
                        or self.config["wake_prompt"]
                    ).strip(),
                }
            ]
            for part in request.extra_user_content_parts:
                data = part.model_dump_for_context()
                data.pop("_no_save", None)
                parts.append(data)
            parts.append({"type": "text", "text": temporary_text})
            request.contexts.append(
                {"role": "user", "content": parts, "_no_save": True}
            )
            request.prompt = None
            request.extra_user_content_parts.clear()
        else:
            request.extra_user_content_parts.append(
                TextPart(text=temporary_text).mark_as_temp()
            )

        event.set_extra(EVENT_REQUEST, request)

    @filter.on_llm_response(priority=100000)
    async def read_decision(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """清除模型标签，并保存本轮计时决定。

        Args:
            event: 当前消息事件。
            response: 模型响应。
        """
        revision = event.get_extra(EVENT_REVISION)
        if not isinstance(revision, int):
            return
        if self._revisions.get(event.unified_msg_origin) != revision:
            if event.get_extra(WAKE_EVENT):
                response.completion_text = ""
            return

        original_length = len(response.completion_text or "")
        decision = self._parse_response(response)
        logger.info(
            "%s Decision parsed: session=%s revision=%s result=%s reply_chars=%s->%s",
            LOG_PREFIX,
            event.unified_msg_origin,
            revision,
            "MISSING" if decision is None else decision,
            original_length,
            len(response.completion_text or ""),
        )
        if decision is not None and not response.completion_text.strip():
            logger.warning(
                "%s Model returned a control tag without a reply body: "
                "session=%s revision=%s",
                LOG_PREFIX,
                event.unified_msg_origin,
                revision,
            )
        if decision == "NEVER":
            event.set_extra(EVENT_REQUEST, None)
            event.set_extra(EVENT_DECISION, None)
        elif isinstance(decision, int):
            event.set_extra(EVENT_REQUEST, None)
            event.set_extra(EVENT_DECISION, (revision, decision))
        else:
            event.set_extra(EVENT_DECISION, (revision, response.completion_text))

    @filter.after_message_sent(priority=100000)
    async def schedule(self, event: AstrMessageEvent) -> None:
        """必要时补做决策，并为当前会话启动计时器。

        Args:
            event: 回复已经发送完成的消息事件。
        """
        decision = event.get_extra(EVENT_DECISION)
        request = event.get_extra(EVENT_REQUEST)
        event.set_extra(EVENT_DECISION, None)
        event.set_extra(EVENT_REQUEST, None)
        if not self._eligible(event) or not isinstance(decision, tuple):
            return

        revision, value = decision
        umo = event.unified_msg_origin
        if self._revisions.get(umo) != revision:
            return

        if isinstance(value, str):
            if not isinstance(request, ProviderRequest):
                logger.warning(
                    "%s Retry skipped without original request: session=%s revision=%s",
                    LOG_PREFIX,
                    umo,
                    revision,
                )
                return
            logger.info(
                "%s Retry started: session=%s revision=%s",
                LOG_PREFIX,
                umo,
                revision,
            )
            contexts = list(request.contexts)
            user_context = await request.assemble_context()
            content = user_context.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        part.pop("_no_save", None)
            if content:
                contexts.append(user_context)
            contexts.append({"role": "assistant", "content": value})
            try:
                retry_response = await self.context.llm_generate(
                    chat_provider_id=await self.context.get_current_chat_provider_id(
                        umo
                    ),
                    prompt=str(self.config["retry_prompt"]).strip(),
                    contexts=contexts,
                    system_prompt=request.system_prompt,
                    tools=request.func_tool,
                    model=request.model,
                    session_id=request.session_id,
                )
            except Exception:
                logger.exception(
                    "%s Retry failed: session=%s revision=%s",
                    LOG_PREFIX,
                    umo,
                    revision,
                )
                return
            retry_original_length = len(retry_response.completion_text or "")
            value = self._parse_response(retry_response)
            logger.info(
                "%s Retry parsed: session=%s revision=%s result=%s chars=%s->%s",
                LOG_PREFIX,
                umo,
                revision,
                "MISSING" if value is None else value,
                retry_original_length,
                len(retry_response.completion_text or ""),
            )

        if not isinstance(value, int) or self._revisions.get(umo) != revision:
            return

        old_task = self._tasks.pop(umo, None)
        if old_task and not old_task.done():
            old_task.cancel()
        self._tasks[umo] = asyncio.create_task(
            self._wake_after(
                umo=umo,
                revision=revision,
                delay=value,
                self_id=event.get_self_id(),
            )
        )

    async def _wake_after(
        self, *, umo: str, revision: int, delay: int, self_id: str
    ) -> None:
        """等待后向原会话投递主动 Agent 事件。

        Args:
            umo: 原会话统一消息来源。
            revision: 创建计时器时的会话版本。
            delay: 等待秒数。
            self_id: 机器人账号 ID。
        """
        try:
            await asyncio.sleep(delay)
            if self._revisions.get(umo) != revision:
                return
            self._tasks.pop(umo, None)
            session = MessageSession.from_str(umo)
            wake_event = CronMessageEvent(
                context=self.context,
                session=session,
                message=str(self.config["wake_prompt"]).strip(),
                sender_id=self_id,
                sender_name="Smart Follow-up",
                message_type=session.message_type,
            )
            platform = self.context.get_platform_inst(session.platform_id)
            if platform:
                wake_event.platform_meta = platform.meta()
            wake_event.set_extra(WAKE_EVENT, revision)
            wake_event.set_extra(WAKE_PROMPT, self.config["wake_prompt"])
            wake_event.set_extra("enable_streaming", False)
            self.context.get_event_queue().put_nowait(wake_event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s Wake failed: session=%s", LOG_PREFIX, umo)
        finally:
            if self._tasks.get(umo) is asyncio.current_task():
                self._tasks.pop(umo, None)

    async def terminate(self) -> None:
        """插件停止时取消全部计时器。"""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
