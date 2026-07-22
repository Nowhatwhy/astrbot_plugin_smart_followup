# astrbot_plugin_smart_followup

让 AstrBot Agent 理解对话中的沉默，并在合适的时间自然地主动续上话题。

## 工作方式

插件不会让当前模型提前写好未来消息。每条回复通常都要决定下一次主动联系时间，并在正文末尾输出：

```text
<<SMART_FOLLOWUP|90>>
```

插件清除标记，并在当前回复发送完成后维护一次本地等待任务。时间到达后，插件会构造内部临时事件，重新进入与普通用户消息相同的 AstrBot 消息管线。新的 Agent 不再判断当前这条是否发送，只结合会话历史和人格生成消息、直接发送，并继续选择再下一次主动联系时间。

只有用户明确要求永久停止主动联系等极少数情况，才输出 `<<SMART_FOLLOWUP|NEVER>>`。省略标记不会被解释为“永远不联系”，插件会自动采用最长等待时间兜底。包含预生成消息的旧格式和 XML/JSON 控制块都会被拒绝。

主动联系是持续关系的默认行为，而不是只有对话异常时才使用的补救措施。当前话题已经聊完、用户正在忙或暂时没有合适话题，都应该选择更长的间隔，而不是把下次联系设成永远。等待期间用户一旦发来新消息，旧任务自动取消并根据新上下文重新选择时间。

等待期间只要用户发送新消息，插件就会删除旧任务；新一轮对话根据最新上下文重新决定。

## 提示词放置

- 稳定的决策规则放在 system prompt 末尾，可通过 `decision_prompt` 配置。
- 当前本地时间通过临时 `extra_user_content_parts` 放在本轮用户输入之后。
- 动态内容使用 `mark_as_temp()`，不会写入会话历史。
- 到点时消息总线只接收短标记 `<<SMART_FOLLOWUP_WAKE>>`；完整 `wake_prompt` 到最终 LLM 请求阶段才作为历史末尾的临时 user 消息加入，不修改 system prompt，也不会保存到历史。
- 不在用户提示词末尾重复完整决策协议，避免把稳定规则降级成普通用户内容。

普通对话和到点唤醒使用完全相同的 system prompt。已有的 system prompt 与长对话历史仍是共同前缀，只有末尾新增的临时唤醒消息需要重新计算，从而保留提供商的前缀缓存命中。

`decision_prompt` 不包含等待时间上下限等动态配置。模型只选择自然秒数，插件代码再根据 `min_delay_seconds` 和 `max_delay_seconds` 进行限制，因此修改时间范围不会改变 system prompt。解析器固定识别 `<<SMART_FOLLOWUP|秒数>>`，自定义提示词时应保留这一格式。

## 特性

- 当前回复决定是否主动回复及等待时间，不提前生成未来消息
- 到点后通过普通消息管线重新运行 AstrBot Agent
- 到点后的 Agent 只生成并直接发送，不再重复判断是否值得发送
- 每次主动发送后继续安排再下一次联系，形成可由用户消息随时重置的持续链条
- 唤醒指令仅作为临时 user 消息追加，不改写 system prompt
- 用户新消息自动取消旧任务
- 待执行时间由插件存储持久化，插件重载或 AstrBot 重启后可恢复
- 根据当前时间和对话语义决定等待时间
- 每个会话每日主动回复上限
- 默认只处理私聊
- 默认关闭流式回复，防止调度标记短暂泄漏
- 无第三方 Python 依赖

## 安装

在 AstrBot WebUI 中通过仓库地址安装，或者克隆到 AstrBot 的插件目录：

```bash
cd AstrBot/data/plugins
git clone https://github.com/Nowhatwhy/astrbot_plugin_smart_followup.git
```

然后在 WebUI 的插件管理页面重载插件。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `enabled` | `true` | 启用插件 |
| `private_only` | `true` | 仅处理私聊 |
| `disable_streaming` | `true` | 防止调度标记在流式输出中泄漏 |
| `min_delay_seconds` | `30` | 最短等待秒数 |
| `max_delay_seconds` | `31536000` | 最长等待秒数，默认一年 |
| `daily_limit` | `3` | 每个会话每日主动回复上限 |
| `decision_prompt` | 内置模板 | 当前轮的时间决策规则，注入 system prompt |
| `wake_prompt` | 内置模板 | 时间到达后交给主动 Agent 的任务指令 |
| `debug_full_payload` | `false` | 启用调度过程及完整请求、响应日志 |

## 日志诊断

插件默认不输出日常运行日志，只保留真正的异常。开启 `debug_full_payload` 后才会记录调度过程、完整 system prompt、本轮用户消息、模型正文和思考内容，可能包含敏感信息，仅应在排查时使用。

## 版本要求

本插件使用 AstrBot 4.26 的消息事件与临时内容能力，要求 AstrBot `>=4.26,<5`。

相关官方文档：

- [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)
- [消息事件与 LLM 钩子](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)
- [插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)
- [插件存储](https://docs.astrbot.app/dev/star/guides/storage.html)
