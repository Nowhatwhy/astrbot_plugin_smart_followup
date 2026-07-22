# astrbot_plugin_smart_followup

让 AstrBot Agent 在用户沉默后主动续聊。

## 原理

模型在正常回复中附加下一次联系时间：

```text
<<SMART_FOLLOWUP|90>>
```

插件发送前删除标签，并在回复发送后启动当前会话的独立计时器。用户在等待期间发来新消息时，旧计时器立即取消；时间到达后，插件向原会话投递一个普通 AstrBot Agent 事件，由 Agent 根据最新历史生成主动消息。

只有用户明确要求永久停止主动联系时才使用：

```text
<<SMART_FOLLOWUP|NEVER>>
```

主模型漏掉标签时，当前回复仍会先正常发送，然后插件复用相同模型、Agent 开始阶段捕获的 system prompt、历史和工具定义补做一次内部时间决策。补决策不会进入完整 Agent，也不会保存到对话历史；再次漏掉标签时不创建计时器。

## 提示词和缓存

- `decision_prompt` 是稳定规则，在 `on_agent_begin` 阶段追加到已经组装好的 system 消息。
- 当前时间和 `user_prompt_reminder` 是不保存的临时 user 内容。
- `retry_prompt` 只用于漏标签后的内部决策。
- `wake_prompt` 只用于到点后的完整 Agent 唤醒，并作为不保存的临时 user 内容进入最终请求。
- 普通回复和主动回复使用相同 system prompt，动态内容只出现在请求末尾。

## 配置

| 配置项 | 说明 |
| --- | --- |
| `private_only` | 仅在私聊中启用 |
| `decision_prompt` | 注入 system prompt 的稳定调度规则 |
| `user_prompt_reminder` | 追加到当前 user 消息末尾的临时格式提醒 |
| `retry_prompt` | 主回复漏标签后的内部时间决策提示词 |
| `wake_prompt` | 计时结束后交给完整 Agent 的临时指令 |

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/Nowhatwhy/astrbot_plugin_smart_followup.git
```

重载插件后生效。插件没有第三方 Python 依赖；内存计时器会在插件重载或 AstrBot 重启时取消。
