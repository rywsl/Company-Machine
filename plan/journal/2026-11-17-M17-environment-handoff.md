# M17 续作受阻：旧进程未加载私有env，旧goal仍停在77项

- 日期：2026-11-17。
- 用户反馈：Grok再次报告三级环境全空，要求补同一批凭据/预算后才能继续08。
- 本次证据：同一活动目录已经有37项本机输入及用户本轮20美元授权，实际模型目录、AutoDL参数/鉴权边界预检通过。资源就绪后Grok仍在18:10–18:13（Asia/Shanghai）做了6次三级env检查，没有使用env-file/加载器，没有调用task next，也未引用13。它保存的goal/plan.md未包含13，仍写至少77项。
- 原因：上次只配置私有文件与新子进程，没有进入既有Grok会话的环境；每次新开的工具shell也不保证继承上一次点加载。Grok另外沿用自己的旧goal计划，未重新读取已更新的任务图。
- 重现：去掉父进程37项本机变量，以实际Windows uv运行`uv run --offline --env-file .env.m17.local python .factory/m17-local/provider_preflight.py --from-process --network`退出0、missing_required为空；同样前缀执行harness task next退出0并返回M17-13。
- 维护：在AGENTS和M17既有实施/交接/13卡中明确每条命令的env-file入口，要求恢复旧goal时读取当前任务和81项标准，先做13，不以08 blocked停止独立实施。使用uv现有功能，不改控制面或harness，不写全局秘密。
- 边界：配置预检不是收费生成、产品资格或自治验收。两个live节点仍是固定blocked占位器，四项准备测试和共享执行器缺失；实施者应按13补全，而不是再次要求用户填同一批env。08及原C10/四格门槛保留。
- 保护：只同步本次审阅的设计/证据哈希；不改生产源码、原system测试、真实plan/state.json或Grok的goal/state.json，不代行task start/accept。
- 脱敏证据：plan/evidence/m17/environment-handoff-observation.json。既有密钥仅留本机忽略文件，未写入公开材料。
