# 调和器与不变量审计（reconciler.md）

> 规范性文档，M3。模块：`mao.reconcile`（V6 审计、声明态对账、行为漂移）。
> 依据：架构文档 7.7、8.1、8.7、10.6、11.9、12.3（I1–I14）。
> 跨组件约定见 `README.md`。声明态的指针与 epoch 见 `governance.md`。厂长唤醒见 `foreman.md`。
> 本文件只增加接口，不改变 README 第 4 节已列出的签名与语义。

---

## 1. 目的与范围

**做什么（M3）**

- 周期性核对宪法不变量 I1–I14。发现违反就记录，能机械恢复的恢复到 epoch 声明的状态，不能恢复的隔离并唤醒厂长。
- 对账声明态与实际态：Git 引用、镜像 digest、凭据**名字**、部署指针。实际态偏离时，以数据库里的声明为准恢复。
- 行为与语义漂移：把工位近期指标与该工位的 Baseline 文档比较。界的数字来自 Baseline 或宪法 `limit`，不来自本文件。

**不做什么**

| 不做 | 归属 |
|---|---|
| 修改 Spec、提交 ChangeSet、移动 `active` 指针 | `governance.md`。调和器只恢复到声明，不发明新声明 |
| 调用 LLM，或根据日志“判断”该不该回滚 | 禁止（D10） |
| 熔断器的队列调度、背压、资源配额 | `scheduler.md`（若该文件尚未写，本文件只写 `station_holds`，不实现调度器） |
| 读取密钥的值 | 禁止（D13）。只比较密钥名字集合 |
| 为 PSI 或其他距离规定数值门槛 | 宪法没有相应 `limit` 时，不算这条，也不用 0.2 之类的例子代替 |

---

## 2. 依赖

| 依赖 | 用到的接口 |
|---|---|
| `Warehouse` | `verify`、`get`、`lineage`、`list` |
| `EpochStore`（`governance.md`） | `pointers`、`materialize`、`retain` |
| `Snapshot` | `load`（经 `materialize`）、`spec`、`result` |
| `mao.db` | 读 `jobs`、`runs`、`events`、`seals`；迁移 `022_audit.sql` |
| `mao.base` | `Clock`、`IdSource`、`canonical_json`、`digest_file` |
| `DockerProvider` 使用的 `docker` 命令 | 只读 `docker image inspect`。禁止 `--privileged`、禁止挂载 docker.sock 到业务容器（D11） |
| `WakeQueue`（`foreman.md`） | `file` |
| `JobCache` | 只读。审计不调用 `invalidate`，除非发现被复用的 Bean 未封存 |
| `ContractRunner`（`contract-tests.md`） | `run_validator`（I8） |

不新增第三方依赖（D2）。不使用 Docker SDK。

---

## 3. 接口

```python
class FindingState(StrEnum):
    OPEN = "open"
    RESTORED = "restored"
    HELD = "held"          # 已停止向该工位提交新 job，等下一次审计通过
    WAKE = "wake"          # 已写唤醒票，等外部 CLI

@dataclass(frozen=True)
class Finding:
    id: str                  # "au-0001"
    invariant: str           # "I1" … "I14"，或 "drift"
    subject: str
    state: FindingState
    detail: dict[str, Any]
    at: str

class Reconciler:
    def __init__(self, ctx: Context, epochs: "EpochStore", *,
                 contracts: Callable[[Snapshot], "ContractRunner"] | None = None) -> None
        # M5-01 增补（plan/drift.md D-13）。contracts 缺省与 governance.md 3.3 相同：
        # lambda snap: ContractRunner(ctx, snap, Runner(ctx, snap))。None 表示“用缺省”，不表示“跳过 I8”
    def audit_once(self, *, timeout_s: float) -> list[Finding]
        # 跑完第 5 节的全部检查。同一轮内相同 (invariant, subject) 不重复插入。
    def findings(self, *, state: str | None = None) -> list[Finding]
    def holds(self) -> list[str]
        # 当前被暂停提交的工位 Spec 键，升序
    def restore_refs(self, *, timeout_s: float) -> None
        # 只修复 Git 引用与可本地完成的镜像标签，见 5.2
```

对 `LineRunner.tick` 的增补：节点的工位在 `holds()` 里时，不提交该节点，run 保持原状态，不把节点标为 `FAILED`。这是暂停，不是失败。审计再次通过后删除 hold，下一次 `tick` 可以提交。

`timeout_s` 由调用方传入。CLI 从 `$FACTORY_ROOT/factory.json` 的 `docker_timeout` 读取；缺失 → `RESTORE_FAILED`。本文件不规定秒数。

### 3.1 CLI

```
factory audit once [--json]
factory audit ls [--state open|restored|held|wake] [--json]
factory drift ls [--json]
```

这些命令只读或只跑审计。不提供“忽略这条不变量”的开关。退出码：有 `open` 或 `wake` 的发现为 1，用法错误为 2，docker / git 故障为 4。

---

## 4. 数据格式

### 4.1 迁移 `src/mao/migrations/022_audit.sql`

```sql
CREATE TABLE audit_findings (
  id         TEXT PRIMARY KEY,
  invariant  TEXT NOT NULL,
  subject    TEXT NOT NULL,
  state      TEXT NOT NULL,
  detail     TEXT NOT NULL,             -- 规范 JSON
  at         TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE station_holds (
  station    TEXT PRIMARY KEY,          -- Spec 键
  finding_id TEXT NOT NULL,
  at         TEXT NOT NULL
);
CREATE UNIQUE INDEX audit_open ON audit_findings(invariant, subject) WHERE state IN ('open', 'held', 'wake');
```

`detail` 禁止出现密钥值、环境变量值、回执里的签名 URL。只有名字、digest、id、错误码。

### 4.2 发现的 detail

| invariant | detail 键 |
|---|---|
| `I1` | `digest, path, expected, actual` |
| `I2` | `bean, missing_input` |
| `I3` | `job, bean` |
| `I4` | `epoch, diags` |
| `I5` | `spec, referenced_by` |
| `I6` | `station, role, tier` |
| `I7` | `station, missing`（`idempotency_key` 或 `verify_receipt`） |
| `I8` | `validator, polarity, path` |
| `I9` | `kind`（`git-ref` / `image` / `secret-name`）, `declared, actual` |
| `I10` | `run, spent, cap` |
| `I11` | `commit, missing`（`changeset` / `report` / `review`） |
| `I12` | `commit, reason` |
| `I13` | `commit, path` |
| `I14` | `epoch, line, step, port, stored, recomputed` |
| `drift` | `station, metric, bound, actual` |

### 4.3 事件

| kind | data |
|---|---|
| `audit.finding` | `{id, invariant, subject, state}` |
| `audit.restored` | `{id, action}` |
| `audit.hold` | `{station, finding}` |
| `audit.release` | `{station}` |

---

## 5. 行为

`audit_once` 在一个进程里按下面的顺序做完。每一条检查独立：一条失败不跳过后面的检查。写发现与 hold 都在各自的事务里。同一 `(invariant, subject)` 已有未关闭发现时，只更新 `detail` 与 `updated_at`，不新开 id。

### 5.1 不变量

| 编号 | 怎么查 | 违反时 |
|---|---|---|
| I1 | 调用 `Warehouse.verify()`。另外按 digest 升序重读 CAS 文件并 `digest_file`，与索引比较 | 记录。不自动改字节。唤醒厂长 |
| I2 | 每个已封存 Bean 的血缘向上：输入 id 都能 `get` 到（墓碑也算存在）。没有来源证明的非外部 Bean 也算违反 | 记录并唤醒。不补写假证明 |
| I3 | `jobs.inputs` 里每个 Bean id，`get` 的 `sealed` 必须为真 | 记录。该 job 若未终态，工位进入 hold |
| I4 | 对 `active` commit `materialize` 后，`result` 的 error 必须为 0 | 记录并唤醒。不自动改 Registry |
| I5 | 任何 `lifecycle == retired` 的 Spec，不得出现在 `active` 或其他仍被 run 绑定的 epoch 的引用里 | 记录。ChangeSet 本应已拒绝；这里是持续审计 |
| I6 | 对每个工位读策略的出网等级。`publish` 只允许 `sink`，`ingress` 只允许 `source`。再查最近一次 attempt 的环境变量**名字**是否与策略声明一致 | 名字不一致 → hold 该工位并唤醒。不打印值 |
| I7 | 每个 `role == sink` 的工位都有 `idempotency_key` 与 `verify_receipt` | 记录并 hold |
| I8 | 对 active 快照里每个类型的每个 validator，用 `contracts(snapshot).run_validator(type_id, name, timeout_s=timeout_s)` 重跑正反样例。verdict 为 `fail`（反样例被接受或正样例被拒绝）即违反，`detail` 含 `validator` 与 `code`。重跑本身抛错（例如镜像缺失）记为 `detail.missing = "contract-runner"`、状态 `wake`，禁止记成通过。**不得**在没有重跑的情况下每轮都写 `missing`（plan/drift.md D-13：那会让安全模式永远开启） | 唤醒。不删除样例 |
| I9 | 见 5.2 | 能恢复的恢复；不能的唤醒 |
| I10 | 每个 run：`spent_usd > budget_cap_usd` 即违反。等于不算超 | 记录并唤醒。不改账 |
| I11 | `refs/factory/active` 的第一父链（`git rev-list --first-parent`）上：说明以 `changeset:` 开头的提交都能在 `changesets` 里按 `commit` 找到，且 T2/T3 的 `approvals` 条数不少于该行的 `reviews_required`；其他提交必须只改了 `constitution/`（`governance.md` 5.3 的 `adopt`） | 记录并唤醒。不合入、也不回滚指针（回滚是治理命令） |
| I12 | `retain` 为真的 commit 缺失检出目录，或某 run 的 epoch 在 `epochs` 表中不存在 | 记录。禁止为了“清磁盘”删除 |
| I13 | `git log` 中修改了 `constitution/` 的提交，`git verify-commit` 必须成功 | 失败 → 唤醒，并把该 commit 记入发现。不把 `active` 指过去 |
| I14 | 读 `$FACTORY_ROOT/epochs/<commit>/resolutions.json`（active epoch 冻结的解析表，规范 JSON 数组，项与 `check_registry` 的 `resolutions` 相同，`origin` 为 `precomputed`）。对 active Registry 再跑一次 `check_registry`，比较 `(line, step, port, status, chain)`。不读、不写 `resolution-runtime.json` | 不一致 → 唤醒。禁止用新结果覆盖 `resolutions.json` |

I14 的“再跑一次”必须用同一个 `check_registry`。禁止在调和器里重写解析算法。存储侧是 epoch 构建时写入的 `epochs/<commit>/resolutions.json`。`dynamic.md` 的 `epochs/<commit>/resolution-runtime.json` 只追加运行期新键，不得改写 `origin: precomputed` 的键，也不是这次比较的存储表。

### 5.2 声明态与实际态（I9）

声明态：

- 指针：`epoch_pointers` 表；
- 镜像：`active` epoch 里 Machine 的 `runtime.image`（`repo@sha256:64hex`）；
- 凭据绑定：策略与机器 `needs.secrets` 的**名字**列表；
- 部署指针：Git 引用 `refs/factory/active` 等。

实际态与动作：

1. **Git 引用**。`git show-ref` 与表不一致时，`git update-ref` 到表里的值。成功 → 发现状态 `restored`。失败 → `RESTORE_FAILED`，状态 `wake`。禁止 `git push`，禁止改数据库去迎合引用。
2. **镜像**。`docker image inspect <digest>`。digest 已在本地但不被任何标签指向：只记录，不拉取。digest 不在本地：不执行 `docker pull`（那会引入未声明的网络与凭据）。发现状态 `wake`，`detail.kind = "image"`。容器若在跑且镜像 digest 与声明不同：停止该容器（`docker stop`），不自动用别的标签启动。下一轮由正常的 `SandboxProvider.open` 按声明的 digest 创建。调和器不调用 `docker run`。
3. **凭据名字**。比较 job 记录里注入过的环境变量名字与策略允许的名字。多了或少了 → hold 工位，唤醒。不读取值，不把值写进 `detail`。
4. **工作区脏**。`active` 对应的 Registry 工作区 `git status --porcelain` 非空 → I9 发现，不自动 `git checkout` 覆盖人的未提交文件。状态 `wake`。

恢复动作必须可重复。第二次 `audit_once` 在已经对齐时不得再 `stop` 容器或再写一条 `restored`。

### 5.3 漂移

对每个在 `active` 中有 Baseline 的工位（Baseline 从 `epochs.materialize(active)` 的快照里用 `Snapshot.keys("Baseline")` 取；**禁止**扫描 `governance.registry` 工作区，那是人与厂长的工作目录，可能有未提议的修改。plan/drift.md D-15）：

1. 窗口长度取 Baseline 的 `spec.window`，用 `parse_duration`。没有 Baseline 的工位不做漂移检查，也不使用一份全局默认窗口。
2. 指标只计算 Baseline.`metrics` 里出现的键。每个键的对象里有哪些界（`max`、`min`、`max_p50` 等），就比较哪些。键名以 `max` 开头：实际值 `>` 界则违反；以 `min` 开头：实际值 `<` 界则违反。等于不算违反。
3. 实际值的算法与 `governance.md` 5.6 的三个运行指标相同的那些键，沿用那里的定义。Baseline 里还有别的键、而本模块不会算 → 发现 `drift`，`detail.reason = "metric-unsupported"`，状态 `wake`。不要把不会算当成 0。
4. 分布距离（包括 PSI）只有在基线宪法存在 `limit("drift_psi_max", _)` 时才计算并与该值比较。没有这条 `limit` 时，跳过距离检查，不使用任何写死的 0.2。
5. 违反 → `station_holds` 加一行，发现状态 `held`，`WakeQueue.file`，信号为 `drift`。下一次 `audit_once` 若该工位不再违反，删除 hold，写 `audit.release`。

调和器不修改 Baseline，不提交 ChangeSet。

### 5.4 与厂长的分工

能在 5.2 第 1 步用 `update-ref` 完成的，不唤醒。其余发现都 `WakeQueue.file`，载荷是发现 id 与 invariant，不含密钥。唤醒失败不得吞掉；把异常转成该发现的 `detail.wake_error`，状态保持 `open`。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `AUDIT_FAILED` | 审计过程自身损坏（表不可写、快照 `materialize` 失败且不是不变量违反） | `cause` |
| `RESTORE_FAILED` | `git update-ref` 或 `docker` 命令失败、超时，或超时配置缺失 | `action, code` |
| `INVARIANT_VIOLATION` | 调用方把某条未关闭发现当成拒绝操作的原因时抛出（审计本身不抛，只记录） | `invariant, subject, finding` |
| `HOLD_ACTIVE` | 其他组件在 hold 期间仍要求立刻提交该工位 | `station, finding` |

`docker` / `git` 的输出可以进 `detail.stderr`，但必须先删掉匹配密钥名字的赋值片段。删不干净时，`stderr` 置为空串，只留退出码。

不变量违反不是 `FactoryError` 的默认路径：`audit_once` 返回发现列表。只有 `audit_once` 自己无法完成时才抛 `AUDIT_FAILED`。

---

## 7. 验收测试

夹具用 `tests/acceptance/m3/gov_world.py`（与 `governance.md` 第 7 章相同的建库方式：Registry 是 Git 仓库，active 已 `init`）。`docker` 用一个假的可执行文件放在 `PATH` 前面，记录参数并按测试返回退出码。不访问网络。

| 文件 | 测试 | 验证什么 |
|---|---|---|
| `tests/acceptance/m3/test_m3_04_reconciler.py` | `test_cas_mismatch_is_i1` | 改一个 CAS 文件的一个字节后，`audit_once` 产生 I1，且不把文件改回去 |
| | `test_lineage_gap_is_i2` | 来源证明指向不存在的输入 → I2 |
| | `test_unsealed_input_is_i3` | job 输入未封存 → I3，并且该工位出现在 `holds()` |
| | `test_git_ref_restored` | 表里 active 是初始 commit、`refs/factory/active` 被改成另一个 commit 时，审计把引用改回，不改表 |
| | `test_missing_image_does_not_pull` | 假 docker 看不到声明的 digest 时，发现为 `wake`，参数列表里没有 `pull` |
| | `test_secret_values_absent` | 发现的规范 JSON 不含测试故意写入环境的密钥值 |
| | `test_baseline_breach_holds` | 夹具 Baseline 的 `max` 被实际值超过 → hold；下一次实际值回到界内 → `audit.release` |
| | `test_no_psi_without_limit` | 宪法没有 `drift_psi_max` 时，审计结果里没有 PSI 比较，也不出现 0.2 |
| | `test_unrestorable_wakes` | I1 产生唤醒票，载荷只有 finding id |
| | `test_hold_blocks_submit` | hold 期间 `tick` 不提交该工位的节点，run 不因此 `FAILED` |
| | `test_i14_compares_tables` | 篡改 `epochs/<commit>/resolutions.json` 里某一行的 `chain` 之后，I14 发现出现，该文件不被覆盖 |

```
uv run python tools/harness.py pytest tests/acceptance/m3/test_m3_04_reconciler.py
```

零失败、零跳过。不在本文件规定通过条数的下限。

---

## 8. 错误示范与正确示范

**8.1 声明态以数据库为准**

```python
# ✖ 错误：Git 引用被人改了，就把 epoch_pointers 更新成引用的值
# ✔ 正确：git update-ref 到表里的 commit。改不动就 RESTORE_FAILED 并唤醒
```

**8.2 不要为了审计去拉镜像**

```python
# ✖ 错误：本地没有 digest 就 docker pull，拉取过程会用到控制面不该持有的仓库凭据
# ✔ 正确：inspect 失败 → 发现 kind=image，状态 wake，argv 里没有 pull
```

**8.3 漂移界来自 Baseline，不是来自代码**

```python
# ✖ 错误：if psi > 0.2: hold(station)
# ✔ 正确：没有 limit("drift_psi_max") 就不算 PSI；比率只和 Baseline 文档里的 max/min 比
```

**8.4 审计不修改结构**

```python
# ✖ 错误：发现 I5 被引用的 Spec 被 retire 了，就自动提交一个 ChangeSet 把它改回 active
# ✔ 正确：记录 I5，唤醒厂长。结构变更只走 governance.md 的流水线
```

---

## 9. 风险与注意事项

- **全量扫描会慢**。本文件要求全量，不用抽样比例。要抽样必须先有人在宪法里增加 `limit` 并另改规范；实现者不要自行取“前 100 个”。
- **docker 假命令**。测试必须把假 `docker` 放在 PATH 前。生产路径与 `sandbox.md` 一样，只传参数列表。
- **hold 与 fail-fast**。hold 不是节点失败。不要让 `lines.md` 的 FAILING 逻辑把整条 run 停掉。只跳过提交。
- **并发**。`audit_once` 与 `tick` 可能同时跑。hold 的插入与“读取 holds 再提交”必须能看到已提交的事务。提交节点前再读一次 hold。
- **Windows**。`docker image inspect` 的输出按 UTF-8、`errors="replace"` 解码。digest 比较用小写 hex，不依赖平台路径。
---

## 10. 任务拆分

| ID | 标题 | 依赖 | 规范章节 | 允许修改的路径 | 验收 | 规模 |
|---|---|---|---|---|---|---|
| M3-04 | 不变量审计、引用修复与工位暂停 | M3-01 | 3–7 | `src/mao/reconcile.py`、`src/mao/migrations/022_audit.sql`、`src/mao/runs.py`（只增 `holds()` 暂停提交）、`src/mao/cli.py`、`src/mao/errors.py`、`tests/system/test_reconcile_*.py` | `test_m3_04_reconciler.py`（第 7 章全部测试通过） | L |

夹具建库方式与 `governance.md` 相同。`tests/acceptance/m3/` 属于预先写好的验收材料。任务执行者不得修改它们。
