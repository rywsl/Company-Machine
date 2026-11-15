# 设计说明：人与厂长的持久对话（M15）

> 背景：常驻厂长已经能续接自己的 CLI 会话（M13），人仍只能发一次性命令、看收件箱。两边没有一条工厂级的、可复查的对话。
> 规范见 `spec/system/talk.md`。阶段门要求 M14 已通过。

## 1. 决定

对话是意图通道。卡片是命令。控制面不调用模型，自由文本不改 Registry。

- **一条工厂级线程。** 角色只有 `human`、`foreman`、`system`。不按人拆线程，不做多人聊天。
- **持久交流有三个前提。** `foreman.mode = resident`，`factory.json` 有 `agents` 段，`factory serve` 的租约有效。复核者仍是一次性会话，不进这条线程。
- **人的话进线程，并 `Mailbox.post` 进厂长邮箱。** 厂长用 MCP 工具 `talk_reply` 写回同一条线程。厂长的 `by` 是运行时引用（`名字@版本`），允许 `@`；人名规则只用于人。本轮消费了 human talk、结果 `ok`、却没有成功的 `talk_reply`：这些消息标成未回复，各开一条收件箱事项。仍会重试的失败不标。邮箱消息被丢弃时标成未回复，线程不再一直显示待回复。
- **配置产线仍走 `changeset_propose`。** 常驻厂长正在跑的轮次里，工具成功就追加卡片，不要求该轮已经有 human talk。推广、影子、金丝雀、签核的规则不改。对话不调用 `advance`、`merge`、`rollback`。确认运行时的 epoch 走既有的 `EpochRouter`，与 `factory line run` 同一条 key。
- **产出产线先记卡片。** `talk_propose_run` 不调用 `start_run`，并开一条收件箱通知。人执行 `factory talk answer ID --confirm` 之后，控制面才调用 `LineRunner.start_run`，`trigger = {"on": "talk", "by": <human>, "message": <卡片 id>}`。关掉通知不启动 run。
- **秘密、审批、签核仍走 `Inbox.answer`。** 线程只渲染卡片。秘密值不进正文，回答载荷不抄进线程。
- **宪法和 `factory.json` 不经过对话写入。** 它们仍由人直接维护。

## 2. 和收件箱、常驻厂长的边界

| 已有机制 | 对话里怎么用 | 不做什么 |
|---|---|---|
| 常驻厂长的 CLI 会话 | 上下文仍在 CLI 里。线程是给人看的记录，不拿来重拼提示 | 不把历史 Bean、事件灌进每一轮 |
| `Mailbox` | human talk 是一种新的邮箱消息，厂长按现有轮次消费 | 不新开一套调度 |
| `Inbox.answer` | 秘密、审批、签核的写入路径不变；线程只显示卡片和后来的状态 | 不在线程正文里放秘密值，不用对话回答代替 `Inbox.answer` |
| `changeset_propose` 与发布循环 | 厂长仍提议；人仍用原命令推进、签核 | 对话不调用 `advance`、`merge`、`rollback` |
| `LineRunner.start_run` | 只有人确认运行卡片之后，控制面才调用 | 模型不直接启动 run；`run_start` 仍只属于 agent 角色 |

卡片只记这几类对象：常驻厂长轮次里成功调用的 `changeset_propose`、`escalate`、`secret_request`，以及 `talk_propose_run` 记的运行卡片。这些对象后来的状态写回线程，新消息带同一个 `ref_id`。全厂的 `bean.sealed` 不进线程。

## 3. 暂不做 Web

M15 不加控制台路由，不暴露到公网。

以后的页面只调用 `mao.talk` 的五个操作：`list_messages`、`show`、`post_human`、`talk_reply`、`answer`。不得直接调用 `Mailbox`、`Inbox`、`LineRunner`、`ChangePipeline`，也不得调用 `propose_run`。`talk_propose_run` 是厂长的 MCP 工具，由控制面追加卡片，不是第六个页面入口。

人用的页面实际用到前三个和 `answer`。`talk_reply` 放进这五个，是为了厂长侧和测试也只走这一个模块，而不是再开一条写入路径。
