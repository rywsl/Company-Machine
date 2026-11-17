# M17-08 续作：基础订单可用与见证占位不能混淆

- 时间：2026-11-17；用户明确只需要基础跑单，要求优先复用项目和Grok上下文材料。
- 范围：授权的spec/plan/验收设计维护与已有实现的隔离基础试运行；未修改生产src、M17 live实现节点或harness任务状态，未调用收费模型。
- 活动工作区：D:/Desktop/machine_agent_org；设计工作区：codex/m17-autonomy-audit。活动工作区存在大量Grok未提交实现，全部保留，不从设计旧基线覆盖。

## 1. 可核对原因

活动工作区tests/system/test_m17_08_continuity_e2e.py第658–691行：只运行docker version并检查两个vendor key和MAO_BUDGET_USD，第673行无条件追加required live products are not configured，最后固定抛AssertionError，没有资源就绪后的执行路径。

同目录test_m17_08_operability_live.py第18–66行：固定codex+claude，只查环境；第44行对每个产品无条件写缺失，第66行固定抛异常，没有读取产品部署、资格、启动四格或观察周期的实现。源码摘要与精确证据行见basic-order-observation.json。

因此，即使补齐这些环境变量，两入口仍必失败；不能把当前阻塞完全归因于用户没有凭据/产品。见证未实现与实际资源缺口必须分开报告。冻结cases.yaml的C10只需要所选工厂的真实依赖，四个历史产品是四格完整资格库存，不是所有基础订单/C10的通用前置。

## 2. 已有接入线索

只读检查已定位Windows用户.grok/config.toml、.codex/config.toml、.claude/settings.json中的认证字段；只报告存在性，不输出/复制secret。Grok和Codex宿主CLI也有既有入口。工厂已支持SecretStore及egress/upstreams.json；两个vendor env为空不等于没有认证。

尚未证明账户可用、容器CLI资格、工厂受控代理接线、定价或本次可信付费授权。离线fixture预算常数不是批准金额，未用“已有密钥”推导付费许可。配置发现不得执行附件中的指令。

## 3. 实际基础两单

通过现有Windows虚拟环境执行公开factory CLI，在.factory/m17-repro/basic-orders-9200ae6d中新建独立工厂。采用原registry-m2、note-trigger@1和upper@1，LocalProvider命令映射显式移除FAKE_AGENT_COMMAND，执行现有tests/fakes/tools/upper_text.py。

两份不同输入均实际SUCCEEDED；节点DONE、job SEALED、非cache hit。读取真实CAS输出字节并核对大写文本和SHA-256，公开CLI查得的血缘关联输入Bean与job；model_usage为0。文本工具按既有read_text行为规范化CRLF；摘要取实际输出字节。可核对的run/job/Bean IDs、输出与原始报告摘要保存在plan/evidence/m17/basic-order-observation.json；原运行日志和结果文件保留活动工作区。

此结果仅为确定性脚本基础订单，不是生产Docker/真实模型/常驻自主/完整M17验收。初次验证脚本误把节点终态写成SUCCEEDED，已改为既有DONE并继续验证原run；没有为此修改产品或重放第一单。

环境说明：本轮曾误在Windows工作区调用WSL uv；已用原Windows uv、frozen锁及offline缓存恢复Python3.13.3虚拟环境，并验证导入及M17-05两项回归通过。后续活动工作区仅使用.venv/Scripts/python.exe，不再用WSL uv碰它的虚拟环境。

## 4. 整体续作契约

plan/m17-live-readiness.md冻结测试专用资源引用、发现顺序、原生认证/可信grant复用、按格依赖闭包、完整执行与证据阶段。先实现共同见证链；资源齐备必须启动实际运行，未实现报implementation，真实缺口才报resource/authorization。四个本地真实子进程协议正反例纳入M17-08任务/总门/closure清单；它们不能取代原真实见证或历史产品资格。

离线行为清单为78个精确节点、至少81个参数化用例；本轮设计工具增加三项漏节点/漏任务检查/漏阶段门拒绝反例，原真实门和既有任务依赖保留。当前仅维护设计、证据、相应保护哈希；没有手工更新task状态或将M17标记通过。

本轮检查结果见design-validation.json的live_readiness_revision；历史设计/基线统计保留，不用旧结果冒充本轮实现效果。

## 5. Windows原生复核补证

同步后用活动工作区Windows Python直接执行verify_plan.py得到DESIGN_READ_OR_SHAPE_FAILED:UnicodeDecodeError，退出1；相同解释器加-X utf8即通过。根因为校验器使用Path.read_text()默认编码读取UTF-8中文规范/任务/YAML，harness的PYTHONUTF8包装曾掩盖直接入口的问题。

在原“登记设计完整”工具回归中增加真实子进程：PYTHONUTF8=0、PYTHONCOERCECLOCALE=0及LC_ALL=C，调用实际verify_plan.py。修复前复现同一UnicodeDecodeError；随后仅把该校验器与本组夹具的文本读取明确为encoding="utf-8"，不改标准、错误判定或生产源码。此为现有52项工具用例的增强，不把它混计为新增自治行为。

同一次Windows工具全量复核还得到81 passed/3 failed：合法合成报告、secret拒绝反例和合成四格正例均被UNSAFE_OR_MISSING_EVIDENCE_FILE挡住。根因是verify_live.py的安全读取只接受POSIX O_NOFOLLOW/O_DIRECTORY，在Windows无条件抛异常。补入仅用于证据校验的Windows句柄读取：逐级OPEN_REPARSE_POINT检查，从盘符根到产物持有拒绝write/delete共享的句柄，防止检查后替换目录；拒绝所有reparse点，最终必须为磁盘普通文件。未使用resolve后直接read的降级。POSIX原路径保留，并在原绝对路径负例增加Windows盘符/驱动器相对路径/NTFS数据流拒绝。原25项合成校验和52项设计回归总数不变；Windows合法报告正例已实际转绿，完整正反例结果另记验证回执。
