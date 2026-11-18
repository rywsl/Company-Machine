# M17-08 受阻：历史 run: 不能冒充 runtime.command

- 时间：2026-11-17
- 任务：M17-08 跨进程多周期与真实能力总验收
- 现象：隔离工厂已把 `governance.registry` 指到含 `6f108f4` 机器规格的 Git Registry。`epoch init` 退出码 0，`serve --once` 退出码 0。随后对 `Machine:openmontage@1`、`Machine:minimax-h3@1`、`Machine:minimax-dance@1`、`Machine:grok-dance@1` 执行 `machine qualify --profile live --allow-paid --budget 1`，四条都退出码 1：`MACHINE_IDENTITY_MISMATCH`，`reason=command`，消息「机器没有可执行命令」。这些规格只有配方 `run:`，没有 `runtime.command`，也不实现 `--self-check`。没有发起收费调用，没有手写 passed 回执。原文在 `{SCRATCH}/live-blocked.txt`。
- 规范依据：`spec/system/operability.md` §3.2，原文：“live 运行声明的已安装真实 CLI/产品……不会为通过而改 runtime、机器声明或引用其他产品。”同文件 §5.4，原文：“产品 qualification 固定声明 source commit/image/实际工具版本，记录真实执行链和 self-check。若声明 OpenMontage 却仅运行自写图像 HTTP 脚本，identity 检查失败……真实工件/调用日志/产品来源必须相符。”`spec/system/instances.md`，原文：“`runtime.command`：容器的主进程。”`spec/system/operator.md` §5.4 把 `recipes.R.run` 定为 script 工位调用 argv，不是资格命令。
- 我的理解：A. 把历史 `run:` 映射成 `runtime.command` 再包一层 `--self-check`，资格命令就能启动。这会改机器声明以通过验收，并可能用自写包装冒充产品。B. `run:` 与 `runtime.command` 是不同字段；资格只认声明的 `runtime.command` 和真实 self-check。历史规格缺这个命令时必须失败，不能为了通过去改声明。按 B 执行。
- 已经尝试：读 operability §3.2/§5.4、instances.md、operator.md §5.4，以及 `qualify_machine` 只读取 `runtime.command` 再追加 `--self-check`。重跑四台 `machine qualify`，全部因没有可执行命令失败。
- 建议：保持 M17-08 blocked。若要让历史 `run:` 成为资格命令，需先改冻结规范并授权修改 `src/mao/qualification.py`，且产品本身要提供真实 self-check。不要在见证目录改写机器声明或伪造回执。
