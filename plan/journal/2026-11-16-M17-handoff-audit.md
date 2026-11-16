# M17 交接审阅与设计交付

- 时间：2026-11-16。
- 用户明确授权：复核spec/plan清晰度，执行 `uv run python tools/harness.py lock --update`，随后提交并推送M17分支；这次命令由用户明确指定，覆盖本次设计交付的默认禁止，不扩展为未来实现的通用锁刷新许可。
- 范围：设计、计划、脱敏证据和校验工具；不改生产src、已有tests、plan/state.json、M16文件和原工作区，不task start/accept/gate，不合并进行中的M16。

## 补清的实施风险

1. 原依赖允许09先创建046、07后补045；db.py使用user_version跳过低号，已升级数据库会漏目标表。固定09依赖07，并给043/044/045/046逐份冻结创建者和表集合；新增反例拒绝移除此依赖。若最终M16尾号冲突，只允许M17首次运行前四槽整体重映射。
2. 四格的help-only/skill原先未固定分配。现为交叉各两格，每领域/每runtime都覆盖两种入口；先help-only、每格独立会话/凭据/根，index与report一致。新增五个反例拒绝缺字段、全同模式、按领域或runtime绑定、报告不一致。
3. 新增plan/m17-implementation.md，十二卡均列必读：M16完成和门证据、单一实施顺序、源码入口、02解析兼容、06/07权限分工、迁移、各类测试和真实资源顺序、有限维护例外及受阻程序。不让实现者自行定义API/降验收/预填业务动作。

## 本次实际验证

- 三个可执行设计/证据工具文件：81 passed、0 failed/errors/skipped，内含新49项追踪/索引工具用例；不能当生产行为通过。
- verify_plan：46个登记项、issues为空；run_contracts --plan：74个精确节点、至少77个参数化行为用例，live不包含在离线清单。
- lint通过、276文件格式通过、doccheck通过。
- m17-contracts最终门改为原45项加49项工具用例，至少94且零失败/错误/跳过；其中13项目标契约仍待M16/M17实现，因此本轮不报告整个契约门通过。
- 原C10和两领域×两native runtime四格真实witness均未执行；设计覆盖不等于修复或真实自治能力。
- 锁刷新、完整快速检查和提交前边界复核结果于本日志尾部追加；Git提交与远端结果以交付后的真实Git状态为准。

## 锁与最终边界复核

- 用户指定的lock --update实际执行成功，438个受保护文件、715个一致性用例；lock --verify通过。相对原HEAD的55个新/变更受保护文件均属于授权M17设计范围，旧一致性哈希和无关文件哈希未变。
- check --fast实际通过：lint/format/lock/doccheck、715项conformance零失败、所有诊断/规则覆盖、strict Registry零错误/警告、4个场景全通过。仅此快速检查，未宣称完整旧回归或任何真实live通过。
- git diff --check通过；原tasks.yaml与gates.yaml字节前缀完全保留。所有56个交付路径均复核；未改src、旧tests、state或M16，常见凭据模式扫描未命中。
- 本结果记录落盘后再次按用户指定命令刷新锁并verify，以使日志/JSON与最终保护基准一致。提交及推送只针对codex/m17-autonomy-audit；提交hash以Git结果为准，不在自引用文件中预填。
