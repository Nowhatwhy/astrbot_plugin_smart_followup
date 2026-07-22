# astrbot_plugin_smart_followup

让 AstrBot Agent 理解对话中的沉默，并在合适的时间重新运行主动 Agent，决定是否自然地续上话题。

## 工作方式

插件不会让当前模型提前写好未来消息。当前回复只负责决定是否需要在未来重新评估；需要时在正文末尾输出：

```text
<<SMART_FOLLOWUP|90>>
```

插件清除标记，并在当前回复发送完成后维护一次本地等待任务。时间到达后，插件会构造内部临时事件，重新进入与普通用户消息相同的 AstrBot 消息管线，由新的 Agent 根据到点时的上下文决定是否发送，以及发送什么内容。

不需要主动续聊时不输出任何标记。解析器只接受 `<<SMART_FOLLOWUP|等待秒数>>`；包含预生成消息的旧格式和 XML/JSON 控制块都会被拒绝，不会创建任务。

等待期间只要用户发送新消息，插件就会删除旧任务；新一轮对话根据最新上下文重新决定。

## 提示词放置

- 稳定的决策规则放在 system prompt 末尾，可通过 `decision_prompt` 配置。
- 当前时间、近期消息间隔和每日调度次数通过临时 `extra_user_content_parts` 放在本轮用户输入之后。
- 动态内容使用 `mark_as_temp()`，不会写入会话历史。
- 到点后的 `wake_prompt` 只作为历史末尾的临时 user 消息，不修改 system prompt，也不会保存到历史。
- 不在用户提示词末尾重复完整决策协议，避免把稳定规则降级成普通用户内容。

普通对话和到点唤醒使用完全相同的 system prompt。已有的 system prompt 与长对话历史仍是共同前缀，只有末尾新增的临时唤醒消息需要重新计算，从而保留提供商的前缀缓存命中。

`decision_prompt` 支持以下占位符：

- `{{min_delay_seconds}}`
- `{{max_delay_seconds}}`

解析器固定识别 `<<SMART_FOLLOWUP|秒数>>`，自定义提示词时应保留这一格式。

## 特性

- 当前回复只选择重新评估时间，不提前生成未来消息
- 到点后通过普通消息管线重新运行 AstrBot Agent
- 唤醒指令仅作为临时 user 消息追加，不改写 system prompt
- 用户新消息自动取消旧任务
- 待执行时间由插件存储持久化，插件重载或 AstrBot 重启后可恢复
- 根据当前时间、近期活跃度和语义决定等待时间
- 每个会话每日主动评估上限
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
| `max_delay_seconds` | `86400` | 最长等待秒数 |
| `daily_limit` | `3` | 每个会话每日主动评估上限 |
| `decision_prompt` | 内置模板 | 当前轮的时间决策规则，注入 system prompt |
| `wake_prompt` | 内置模板 | 时间到达后交给主动 Agent 的任务指令 |
| `debug_full_payload` | `false` | 临时记录续聊提示词与模型响应 |

## 日志诊断

运行日志统一以 `[smart_followup]` 开头，覆盖用户活动、旧任务取消、决策解析、本地等待任务创建、唤醒事件入队和异常。每次请求还会记录 `request_kind` 与 `system_prompt_sha256`；普通请求和唤醒请求的哈希应相同，可以直接用来确认 system prompt 未变化。`debug_full_payload` 只应在排查时开启；它会记录完整 system prompt、本轮用户消息、续聊临时数据、模型正文和思考内容，可能包含敏感信息。

## 版本要求

本插件使用 AstrBot 4.26 的消息事件与临时内容能力，要求 AstrBot `>=4.26,<5`。

相关官方文档：

- [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)
- [消息事件与 LLM 钩子](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)
- [插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)
- [插件存储](https://docs.astrbot.app/dev/star/guides/storage.html)
