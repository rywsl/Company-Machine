# M15 最短产线

运行的是已经提交的 M15（`b810097`，人与常驻厂长的持久对话）。M16 只有计划，控制面代码与这次提交相同。模型代理把上游等待从 5 秒放到 90 秒，否则 `gpt-6.1-sol` 的一次补全经常来不及返回。

产线 `still-to-clip@1`：

1. `still@1`：操作工 `gpt-6.1-sol`（`https://openai.createsis.cn`）把说明收成一条静帧提示词。机器是 [OpenMontage](https://github.com/calesthio/OpenMontage) `fecb9a1`，生图沿用它的 `openai_image` 契约（`POST /v1/images/generations`），网关是 newapi `https://tbtk.asia`，模型 `gpt-image-2.5`。
2. `clip@1`：脚本把这张 PNG 以 data URI 交给 AutoDL MiniMax H3 工作流 `minimax_h3_zm_u24`，时长 1 秒，分辨率 `480p竖`，再收下 MP4。

厂长同样用 `openai-sol@1` / `gpt-6.1-sol`，`foreman.mode` 是 `resident`。`factory.json` 带 `agents` 段。`factory serve` 会把已建好的 epoch 交给常驻线程；人用 `factory talk send` 说话。厂长可以在自己的 Registry 克隆里写新文件、提交，再提议变更。T2 变更由人签核。

跳舞产线 `fluffy-jumpstyle@1` 由常驻厂长写进 Registry：工位 `jumpstyle-dance@1` 的操作工先让模型写动作提示，再调用旁边的 MiniMax 机器。机器接受参考图、原声音频和 brief；成片的画面来自工作流，音轨用 ffmpeg 换成输入里的原声。接口时长上限是 15 秒。

视频工位按 [ComfyUI API](https://autodl.art/docs/comfyui_api/) 提交：`Authorization` 是令牌原文，工作流 `minimax_h3_zm_u24`，参考图是 data URI。令牌必须在 [令牌管理](https://autodl.art/large-model/tokens) 里创建，并且分组选 ComfyUI。开发者 Token 和别的分组会被网关拒绝（HTTP 401，`Invalid authentication credentials`）。

密钥不进 git。准备三个环境变量后，在仓库根目录执行：

```bash
export MAO_OPENAI_API_KEY=...    # createsis，不要带 Bearer
export MAO_TBTK_API_KEY=...      # tbtk.asia
export MAO_AUTODL_AK=...         # AutoDL ComfyUI 分组令牌，原样放进 Authorization
uv run python deploy/m15-line/bootstrap.py
```

默认把工厂放在 `~/mao-m15/`。不要放在 `/tmp`、`/workspace` 或 `/factory` 下面：本地沙箱会把这些前缀当成工位挂载。`factory.json`、未武装的 `scheduler.json`、秘密和 `egress/upstreams.json` 在 `~/mao-m15/factory/`，命令映射在 `~/mao-m15/commands.json`。

对人说话要先让 serve 持有租约：

```bash
uv run factory --root ~/mao-m15/factory --provider local \
  --local-commands ~/mao-m15/commands.json serve
uv run factory --root ~/mao-m15/factory --provider local \
  --local-commands ~/mao-m15/commands.json talk send --by li "工厂在吗？"
uv run factory --root ~/mao-m15/factory --provider local \
  --local-commands ~/mao-m15/commands.json talk ls
```
