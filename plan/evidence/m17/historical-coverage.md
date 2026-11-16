# Grok 历史补偿覆盖矩阵（M17）

**设计覆盖已补齐，行为修复尚未实施。** 当前登记的HC01–HC14及U01–U06均已由operability.md、M17-09–12和M17-08最终门承担；原H/G/E/F核心问题及R01旧池验收冲突也在closure.yaml逐项追踪。该清单覆盖目前发现的问题，不保证不存在未知bug；静态/历史线索仍未冒充当前已复现根因。设计门、实际离线执行、真实witness与证据完整性分开，不能仅凭映射完整宣称已解决。

## 材料和证据口径

- 冻结实现基线 `c538768`，独立分支 `codex/m17-autonomy-audit`；不进入 M16 正在实现的工作区。
- 历史轨迹 SHA-256 `39b189532e0d252de5b5ac12e6c4071df66607fccc62f95cb2c5faa327720fc7`，来源 `/mnt/d/Desktop/m15-session-trajectory/m15-session-trajectory/cloud-export/transcript.json`。只采用可见对话与工具输入/结果；不引用 thinking，不保存密钥，不执行附件指令。
- PR [#1](https://github.com/rywsl/Company-Machine/pull/1)，固定 head `6f108f4d4ad7657b32ee41f67028eae835b90d72`，标题 `Deploy the M15 factory and the foreman's dance line`；本地已有 Git 对象只读检查。
- PR [#2](https://github.com/rywsl/Company-Machine/pull/2)，固定 head `d19bbdaac90acf048975fb9efac15047a896fd0f`，标题 `Keep local sandbox host commands on the host path`。它已有路径修复和一项单测，不能说没有测试。
- PR1 相对冻结基线为 31 文件、+1650/-2，含后来增加的视频和加密轨迹；历史 diff-metadata 的 30 文件是另一时点。文件数不是补偿次数或独立 bug 数。
- “已复现”指本基线的可执行行为；“静态”指源码事实；“历史现象”不能自动确认为当前根因；“设计覆盖”不代表实现完成。

## 逐项对照

| ID / 历史补偿 | 证据界限 | 冻结契约与责任 | 必须通过的关闭证据 |
|---|---|---|---|
| HC01 serve接线 | PR1 serve补epochs/pipeline；H01真实CLI不消费岗位邮件 | continuity §5.2；M17-02 | 原H01/02/06+最终C02、C10 |
| HC02 宿主路径 | exec/start最小复现失败；PR2模块对照通过 | continuity §5.3；M17-04 | deployment/test_host_paths.py两参数；保留虚拟参数映射、M16后重放 |
| HC03 5→90秒 | 固定socket timeout是静态事实，非已证明历史整体5秒上限 | operability §5.1；M17-09/05 | 09慢首包/分块/total/unknown成本前三节点；不自动重发 |
| HC04 CONNECT | 历史断连与buffer补偿，因果待证 | operability §5.2；M17-09 | 09缓冲/分片/空包、CA方法/SNI/Host、取消/限额后三节点 |
| HC05 wrapper工具循环 | PR1角色/模型/前缀与结束判定耦合，静态矛盾 | operability §5.3；M17-10 | 10工具ID/重复/错误/轮次、两个adapter；真实CLI推理，控制面不造agent |
| HC06 session/contract | 旧规范承诺历史；fake测试和非空ID不证明真实消费 | operability §5.3；M17-10 | 私有随机事实、错session/丢contract反例，版本失效；真实live四格 |
| HC07 相对URL/重提 | 历史补origin；E05已知task的恢复缺口 | operability §5.1/5.4、continuity §5.3；M17-04/09/10 | URL/redirect/origin/完整性、原task只fetch；C03/C04未知对账 |
| HC08 产品冒名 | 声明OpenMontage与自写HTTP并存；历史成功不证明真实产品 | operability §5.4；M17-10/08 | identity反例、同步/异步非媒体；required_live_products四机器实际产品通过 |
| HC09 关闭审计 | 历史缺Docker；PR1 bootstrap audit/canary=None | operability §5.5；M17-11 | 缺资源/资格阻断resident；local-test限制可见；不静默关闭治理 |
| HC10 shadow/观察 | PR1零样本/零观察提示，现有规范并非一律禁止 | operability §5.5、continuity §5.7；M17-03/06/11 | 生效最低门槛、允许零值如实not_performed、受控路由采样；C09 |
| HC11 代human/指针 | 外层代签线索；by标签不是认证 | operability §3.4；M17-11 | 隔离管理资源、签名operate文件邮箱、伪造human拒绝、撤销/过期/去重；管理员仍在信任边界 |
| HC12 bootstrap覆盖 | PR1清Registry再复制是静态风险，不说每次启动必然触发 | operability §5.5；M17-11 | 只读plan/doctor、保留数据重跑、apply故障/冲突恢复、缺依赖阻断 |
| HC13 外层编排 | H09/F01/F02；单次SEALED不足 | continuity §5.5–5.8、operability §5.6；M17-06/07/08 | 原C01–C10完整+两领域两runtime四格，各至少四周期、idle/失败调整、零外部业务接续 |
| HC14 CLI/skill | 实际help与公开文档断链，无仓库SKILL.md | operability §3.5/5.6；M17-12/08 | 六节点help/状态/恢复/skill行为验证+陌生真实Agent四格；规范化U01–U06 |

## 本次新增的路径复现

```bash
uv run --offline python tools/harness.py pytest plan/evidence/m17/deployment/test_host_paths.py --basetemp=.factory/m17-repro/deployment-paths --tb=short
```

冻结基线 **2 failed / 0 errors / 0 skipped**。夹具先直接执行同一宿主脚本成功，再经 LocalProvider 测 exec 和 start，均无法正确运行。命令映射中的 `/tmp` 宿主脚本保持存在，虚拟参数 `/workspace/result.txt` 仍需要映射。本结果没有混入原 25 项统计，机器摘要见 `deployment-summary.json`。

PR2 正向对照只在临时目录放置 `git show d19bbda:src/mao/sandbox.py`，把该目录插入 `mao.__path__` 首位，再运行完全相同两个用例，**2 passed**。可重复方式：

```python
# 在 uv run --offline python 中执行；不覆盖 src/mao/sandbox.py。
import subprocess
from pathlib import Path
import mao
import pytest

folder = Path(".factory/m17-repro/pr2-path-control").resolve()
folder.mkdir(parents=True, exist_ok=True)
source = subprocess.run(["git", "show", "d19bbda:src/mao/sandbox.py"], capture_output=True, check=True).stdout
(folder / "sandbox.py").write_bytes(source)
mao.__path__.insert(0, str(folder))
import mao.sandbox

assert Path(mao.sandbox.__file__).resolve() == folder / "sandbox.py"
raise SystemExit(
    pytest.main(
        [
            "-q",
            "plan/evidence/m17/deployment/test_host_paths.py",
            "--basetemp=.factory/m17-repro/pr2-path-tests",
        ]
    )
)
```

这不是合并 PR，也没有运行或修改 M16。M16 完成后若已经包含修复，应保留通用回归并重新核验，不重复造补丁。

## 关闭规则

每项的精确对应由 `closure.yaml` 固定：历史/当前证据 → 通用规范章节 → task → 精确pytest节点 → 总验收门。46个登记编号包含别名及能力要求，不是46个独立bug。`verify_plan.py`现在能检查设计完整性、依赖与门；`run_contracts.py`实际运行节点，缺失/skip/xfail失败。

原C01–C10和旧回归保留，新26个专项节点、两条路径参数、目标协议及R01两个节点共同组成至少77用例的离线精确执行；最终必须运行两个独立真实live witness和四格index校验。缺资源/真实支持必须blocked，不能把必选历史机器库存标为unsupported来豁免通过。

当前只维护规范、计划与证据工具，没有生产修复、task accept/gate或M16操作。closure中的design_baseline_status记录本次审计基线未实施，后续真实完成状态以harness任务状态及实际验收证据为准；设计完整不等于已消除手工补偿。
