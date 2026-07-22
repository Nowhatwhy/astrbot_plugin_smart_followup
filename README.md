# astrbot_plugin_smart_followup

让 AstrBot Agent 理解对话中的沉默，并在合适的时间主动续上话题。

## 工作方式

插件不会随机选择时间，也不会为时间判断额外调用一次模型。它在主模型的系统提示词末尾注入一份固定协议，让模型在正常回复后输出隐藏的结构化决策：

```text
<astrbot_smart_followup>
{"action":"schedule","after_seconds":90,"message":"所以你刚才想说什么呀？"}
</astrbot_smart_followup>
```

插件在发送回复前移除该控制块，并在主回复成功发送后创建定时任务。用户在等待期间发送任何新消息，旧任务都会立即失效；新一轮对话会根据最新上下文重新决定。

## 特性

- 单次主 LLM 调用同时生成当前回复和未来主动消息
- 根据当前时间、近期用户消息间隔和对话语义决定等待时间
- 一个会话始终最多保留一个待发送任务
- 用户新消息通过会话版本号可靠地使旧任务失效
- 待发送任务使用 AstrBot 插件 KV 存储，重载或重启后可以恢复
- 下一轮请求会临时补充上一条主动消息，让模型理解用户正在回复什么
- 每会话每日主动消息上限
- 默认只处理私聊
- 默认关闭流式回复，防止隐藏标签短暂泄漏
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
| `disable_streaming` | `true` | 防止控制标签在流式输出中泄漏 |
| `min_delay_seconds` | `30` | 最短等待秒数 |
| `max_delay_seconds` | `86400` | 最长等待秒数 |
| `daily_limit` | `3` | 每个会话每日主动消息上限 |
| `max_message_length` | `300` | 主动消息最大字符数 |

动态的当前时间和活跃度信息通过临时 `extra_user_content_parts` 追加在本轮用户输入之后，不会写入会话历史，也不会让 system prompt 每轮变化。固定协议只会在插件首次启用或配置发生变化时影响旧提示词缓存。

## 平台限制

主动消息依赖平台适配器对 `Context.send_message()` 的支持。部分平台可能禁止或限制主动消息；QQ 官方 API 适配器目前不支持该接口。NapCat/OneBot 等平台也可能受账号风控、频率限制或平台规则影响，请优先使用测试账号并控制发送频率。

## 开发

本插件要求 AstrBot `>=4.24,<5`，遵循官方插件开发指南：

- [最小插件实例](https://docs.astrbot.app/dev/star/guides/simple.html)
- [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)
- [消息事件与 LLM 钩子](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)
- [主动消息发送](https://docs.astrbot.app/dev/star/guides/send-message.html)
- [插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)
- [插件存储](https://docs.astrbot.app/dev/star/guides/storage.html)
