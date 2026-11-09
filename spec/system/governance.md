# 治理与 ChangeSet（governance.md）

> 规范性文档，M3。模块：`mao.governance`。
> epoch 从 M1 的目录哈希换成 Git commit。结构变更只能走 ChangeSet。宪法仍只有人能改。

---

## 1. 目的与范围

**做什么（M3）**

- 记录 Registry 的 Git commit 作为 epoch，保存当前指针，支持回滚指针。
- ChangeSet 流水线：`draft → verifying → verified → shadow → canary → active`，任一阶段可 `rejected` 或 `rolled-back`。
- 风险等级由 MRL 规则根据 diff 计算，提案者不能自报等级。
- 影子运行、金丝雀、按 SLO 自动回滚。
- 变更速率与封版窗口。

**不做什么**

- 不让厂长修改 `constitution/`。
- 不在控制面调用模型来评估风险。

---

## 2. 依赖

`Snapshot`（M3 起 `load` 仍可用目录，epoch 字段改为 commit）、Git 命令行、`ContractRunner`、`LineRunner`。风险规则放在受保护的 MRL 模块里，由人维护。

---

## 3. 接口

```python
class EpochStore:
    def current(self) -> str
    def checkout(self, commit: str) -> Snapshot
    def rollback(self, commit: str, *, by: str) -> str

@dataclass(frozen=True)
class ChangeSet:
    id: str
    base_epoch: str
    state: str
    risk: str
    def to_json(self) -> dict[str, Any]

class ChangePipeline:
    def submit(self, spec: Mapping[str, Any], *, by: str) -> ChangeSet
    def advance(self, cs_id: str) -> ChangeSet
```

`risk` 只能是 `low`、`medium`、`high`。计算失败 → `RISK_UNSCORED`，不得默认为 `low`。

---

## 4. 数据格式

ChangeSet 文档字段与架构文档 11.2 一致：`base_epoch`、`intent`、`changes`、`rollout`。`changes[]` 的 `op` 只能是 `upsert`、`deprecate`、`retire`。

指针文件：`$FACTORY_ROOT/epoch`，内容是一行 commit id。回滚不改写历史，只改指针。

---

## 5. 行为

1. `submit` 拒绝任何触碰 `constitution/` 的变更，错误码 `CONSTITUTION_LOCKED`。
2. 风险取所有变更里的最高等级。提案者提供的 `risk` 字段被忽略。
3. `verifying` 跑 `ContractRunner`。有失败则 `rejected`。
4. `shadow` 用新快照跑 `rollout.shadow.runs` 次，不发布。失败则 `rejected`。
5. `canary` 按 `percent` 分流。观察窗口内 SLO 不满足则 `rolled-back`，指针回到 `base_epoch`。
6. 封版窗口内 `submit` → `FREEZE_WINDOW`。窗口是 UTC 时段，来自人维护的配置，不来自厂长。
7. 速率：24 小时内 active 的 ChangeSet 超过配置上限 → `RATE_LIMITED`。

---

## 6. 错误

| 错误码 | 触发条件 | detail |
|---|---|---|
| `CONSTITUTION_LOCKED` | 变更碰到 constitution | `path` |
| `RISK_UNSCORED` | 风险规则没有给出等级 | `change` |
| `FREEZE_WINDOW` | 封版期间提交 | `until` |
| `RATE_LIMITED` | 超过变更速率 | `count` |
| `SLO_ROLLBACK` | 金丝雀 SLO 失败 | `metric` |

---

## 7. 验收测试

文件：`tests/acceptance/m3/test_m3_01_governance.py`。本里程碑开始前不创建该文件。

| 测试 | 验证 |
|---|---|
| `test_constitution_change_rejected` | 改宪法 → `CONSTITUTION_LOCKED` |
| `test_risk_ignores_proposer` | 自报 low 仍得到规则算出的更高等级 |
| `test_unscored_is_not_low` | 规则无结果 → `RISK_UNSCORED` |
| `test_rollback_moves_pointer_only` | 回滚后 Git 历史仍在，指针改变 |
| `test_freeze_window` | 窗口内 submit 失败 |

---

## 8. 错误示范与正确示范

**8.1 让厂长改宪法**

```python
# ✖ 宪法只有人能改
commit("constitution/invariants.mrl")
# ✔ 直接拒绝
raise FactoryError("CONSTITUTION_LOCKED", "宪法不能由 ChangeSet 修改", path=path)
```

**8.2 算不出风险就当低风险**

```python
# ✖ 未知不是安全
risk = "low"
# ✔ 没有等级就不能进入 verifying
raise FactoryError("RISK_UNSCORED", "没有风险等级", change=change_id)
```

**8.3 回滚用 reset --hard**

```python
# ✖ 破坏历史
git reset --hard base
# ✔ 只改工厂指针
epoch_store.rollback(base, by=actor)
```

---

## 9. 风险与注意事项

- **R18**：禁止破坏性 git 操作。回滚只动指针。
- **R14**：风险规则有歧义时停下来写 journal，不要猜一个等级。
- 影子运行必须绑定新快照的 epoch，不能改当前指针。
