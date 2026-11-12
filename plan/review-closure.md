# 闭环审查（2026-11-13）

> 设计方在阶段门 M8 通过之后做的闭环审查：系统承诺的能力是否在生产路径上真的能跑通。**受保护，只能由人修改。**
> 纠偏任务是 **M9-01 至 M9-04**，规范 `spec/system/closure.md`，验收 `tests/acceptance/m9/`（23 个）与重写后的 `tests/acceptance/m4/test_m4_02_plan.py`（7 个）。
> 本文件的编号（C-xx）被规范与任务卡引用，不要重新编号。

---

## 0. 结论

阶段门 M0–M8 全部通过：2026-11-13 的 `gate M8` 记录 pytest 1420 个、零失败、零跳过。出网、值守、回收、备份等 M5–M8 的能力已经在生产路径上运行。

**M4 的四项交付没有闭环。** 它们只有被验收直接调用的纯函数，运行期没有任何调用者；部分实现按验收里的字面值写死：

1. **动态结构**：`LineRunner.tick` 从不调用 `DynamicRunner.expand`。产线里写了 switch 或 subline，控制节点会被当作工位提交，得到 `SPEC_NOT_FOUND`；until 产线在 `start_run` 时因节点 id 重复抛 `IntegrityError`；动态 map 的上游工位拿不到输入。
2. **Plan**：`Runner` 对 `role: planner` 一律报 `ROLE_UNSUPPORTED`，`compile_plan` 没有调用者。`compile_plan` 本身按旧验收写死：步骤数 `> 40`、`$inputs.brief` 以外的输入一律报 `E-NO-CONVERTER`。
3. **B 形态网关**：`Runner` 没有接入 `ServiceGateway`，生产路径上 `form: service` 永远是 `FORM_UNSUPPORTED`。`run_recipe` 忽略机器 Spec：写死 `POST /videos`、`/tasks/<id>`、回退任务号 `"task-acceptance"`、取回 `http://127.0.0.1/clip.bin`。
4. **生产后端**：S3 的 `Authorization` 只有 `Credential=<access key>`，没有签名，真实 S3 / MinIO 会全部拒绝。`PostgresStore.migrate` 在 `$FACTORY_ROOT/tmp/pg-version.db` 里跑 SQLite 迁移并返回它的版本号。命令打开 `Context` 时不读 `backend-active.json`，迁移之后工厂仍用本地后端。

根因两边都有。实现方用占位或写死的值让验收通过（C-01 至 C-05）。设计方的 M4 验收只调用编译与解析函数，替身不检查关键行为（假 S3 不验签名、假 `factoryctl` 不看方法与路径），`test_m4_02_plan.py` 的断言甚至与 `dynamic.md` 5.5 相反（C-06）；几处规范没写清或写错（C-07）。

---

## 1. 审查方法

- 搜索 M4 公开函数的调用者：`compile_dynamic` 只被 `runs.py` 调用；`DynamicRunner`、`compile_plan`、`ServiceGateway`、`S3BlobStore`、`PostgresStore` 在 `src/mao` 里没有生产调用者。
- 在 `registry-m2` 的副本上写了真实执行的动态产线、planner 工位与服务型工位（`tests/acceptance/m9/closure_world.py`），夹具 `mrl registry check --strict` 零 error、零 warning，用 `LineRunner` 实际运行。拆分工位在普通产线里运行正确，证明夹具本身可用。
- 假 S3 的 SigV4 校验先用 AWS 文档的 GET Object 示例向量验证。
- 新验收对当前实现逐条运行，确认失败原因正是下表的问题。

---

## 2. 问题清单

| 编号 | 位置 | 现象 | 后果 | 处置 |
|---|---|---|---|---|
| C-01 | `src/mao/runs.py` `LineRunner.tick`；`src/mao/dynamic.py` `DynamicRunner.expand` | tick 不调用 expand；expand 只会在溢出时把第一个节点标失败，从不创建节点 | map / switch / until / subline 在运行期全部不可用 | M9-01，`closure.md` 5.1 |
| C-02 | `src/mao/runner.py` 角色分派 | `planner` → `ROLE_UNSUPPORTED`；没有出厂检验调用 `compile_plan`，没有挂接 | Plan 不可用 | M9-02，`closure.md` 5.2 |
| C-03 | `src/mao/dynamic.py` `compile_plan` | 写死 `len(steps) > 40`；`$inputs.brief` 以外报 `E-NO-CONVERTER`；单步 Plan 指向 planner 自身时抛 `RUN_PLAN_INVALID` | 合格的 Plan 被拒，不合格的被放过 | M9-02，`closure.md` 5.2 第 7–12 条 |
| C-04 | `src/mao/gateway.py` `run_recipe`；所有构造 `Runner` 的地方 | `del inputs, params, attempt`，路径与取回地址写死；`Runner` 无网关参数 | 服务型机器在生产路径不可用；接入任何真实服务都会打错地址 | M9-03，`closure.md` 5.3 |
| C-05 | `src/mao/backends.py` S3；`src/mao/commands/common.py` `open_context` | 无签名；命令不读指针 | 迁移到 S3 后工厂仍写本地；真实 S3 拒绝所有请求 | M9-04，`closure.md` 5.4 第 1–3 条 |
| C-06 | `src/mao/backends.py` `PostgresStore` | `migrate` 用本地 SQLite 冒充；没有 `close`；`query` 不解析 psql 输出 | Postgres 后端是假的；仓库根目录留下 `tmp/pg-version.db` | M9-04，`closure.md` 4.4、4.5、5.4 第 4 条 |
| C-07 | `src/mao/egress.py`、`src/mao/governance/rollout.py` | `INSERT OR REPLACE`、`sqlite_master` | 切到 Postgres 后这些语句失败 | M9-04，`closure.md` 5.4 第 5 条 |
| C-08 | 设计方：`tests/acceptance/m4/` | M4-01、M4-03、M4-04 的验收只测纯函数，替身不检查关键行为；M4-02 的断言与规范相反 | 占位实现能通过阶段门 | 本次重写 `test_m4_02_plan.py`；运行期行为由 `tests/acceptance/m9/` 覆盖；其余 M4 文件不改 |
| C-09 | 设计方：`backends.md`、`gateway.md`、`dynamic.md` | `backends.md` 说 `AUTOINCREMENT` 出现即 `SQL_DIALECT`，而迁移 002、011、014 都用了它；没规定 psql 的行输出格式；没规定取回文件放到哪个端口；没规定 Plan 里 `$inputs` 指什么、侧车的键、谁来 tick 子 run | 实现方无法照规范写出可用实现 | `closure.md` 第 4 章与 5.1 第 6 条、5.2、5.3 第 3 条 |

---

## 3. 设计方同时修改的受保护文件

| 文件 | 修改 |
|---|---|
| `spec/system/closure.md` | 新增：C-01 至 C-09 的规定与验收 |
| `plan/review-closure.md` | 本文件 |
| `tests/acceptance/m9/` | 新增夹具 `closure_world.py` 与 4 个验收文件（23 个测试） |
| `tests/acceptance/m4/test_m4_02_plan.py` | 按 `dynamic.md` 5.5 与 `closure.md` 5.2 重写，测试名不变 |
| `spec/system/dynamic.md` | 第 7 章 M4-02 各行的说明对齐重写后的测试；文首指向 `closure.md` |
| `spec/system/gateway.md`、`spec/system/backends.md` | 文首指向 `closure.md` |
| `plan/tasks.yaml`、`plan/gates.yaml`、`plan/milestones.md`、`plan/tasks/M9-0*.md`、`spec/system/README.md` | 任务 M9-01 至 M9-04 与阶段门 M9（继承 M8 的全部检查） |

---

## 4. 给实现方

1. **开始之前**：M8-01 的实现（`src/mao/` 下 11 个文件、迁移 `035`、`tests/system/test_hardening.py`、日志）仍只在工作区里。先提交它，提交信息写明 M8-01；`plan/state.json` 随之提交。删除仓库根目录的 `tmp/pg-version.db`（它由 C-06 的假实现产生），不要提交。
2. 读 `spec/system/closure.md` 全文，再读它引用的 `dynamic.md` 5.2–5.6、`gateway.md` 5.1–5.4、`backends.md` 4.3–5.5。
3. 按 M9-01 → M9-02 → M9-03 → M9-04 的顺序做。M9-02 依赖 M9-01（Plan 的子计划可以含动态构造）。
4. 以下任何一种都算任务失败，即使验收通过：在实现里出现验收或示例里的路径、任务号、文件名、数字上限作为常量或回退值；让控制节点提交 job；用本地 SQLite 冒充 Postgres；修改夹具或替身让检查通过。
5. 验收与规范矛盾时，照 `plan/README.md` 第 5 节写日志并 `task block`。
