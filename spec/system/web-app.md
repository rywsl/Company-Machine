# M18 本地 Web App 用户入口与自治编排规范

> 本规范定义本地 Web App 的交互边界。它把 BusinessSystem、项目来源、执行图、节点依赖、物料关系、运行状态和动态控制投影成用户可理解的工厂与厂区视图。BusinessSystem 是业务数据聚合，Line、Station、OperatorRuntime 和 Machine 继续承担执行事实。

## 1. 目的与范围

M18 提供一个面向本地部署的独立 Web App。用户打开本地地址后直接进入看板，不经过登录、账号、租户或多用户流程。Web App 是现有控制面的用户交互入口，浏览器中的工厂和厂区是执行图的聚合视图。

产品关系固定为以下逻辑链路。

```text
人 → 工厂与 Foreman
Foreman → 创建厂区、管理 Operator 和 Machine、运行、验证、修复与恢复
Operator → 项目执行、资源发现、资源请求与运行处理
Machine → Operator 调用的开源项目、工作流或本地工具
```

Foreman 是工厂创造者和自治中枢。只要其 Harness CLI 可以运行，Foreman 可以根据用户目标创建厂区、装配员工、调整执行关系、处理 Operator 资源请求、运行、验证、修复和继续推进。普通业务流程不进入人工审批队列。

M18 交付四个入口，顺序固定为看板、编排、资产、日志。编排画布承担所有拓扑和节点配置，其他页面负责摘要和上下文进入。

## 2. 依赖

M18 依赖现有执行图和控制面能力，页面使用现有节点依赖、物料绑定、运行状态、动态控制、资产血缘、运行记录和宪法约束。Factory 和 Workarea 只是前端理解层名称，不能成为新的后端资源。

Foreman 与 Operator 的 Harness 通过官方 Harness 能力发现和声明式运行时接入。首批支持 Claude Code、Codex、DeepSeek Harness、Kimi Code、ZCode 和 Pi。ACP 是优先传输方式，官方 CLI 是兼容路径。控制面只负责启动、输入分发、事件接收和 transcript 解析。

M18 demo 可以使用原生 DOM、Pointer Events、SVG 和本地 mock 数据，也可以使用 React Flow。无论采用哪种实现，都必须保持本规范的图数据形状、节点类型约束和层级互斥关系。控制面不得调用 LLM API。

M18 的正式实现依赖 M17 阶段门通过。纯 UI demo 用于交互确认和浏览器回归，不替代 M17 或后端能力证据。

## 3. 接口

### 3.1 本地 Web App 入口

页面打开后直接进入看板。应用不提供登录、注册、账号切换、租户、邀请、用户菜单、远程 SaaS 工作区或多用户配置。

### 3.2 导航入口

侧边栏只包含以下四项。

1. 看板
2. 编排
3. 资产
4. 日志

侧边栏默认完全离屏。收起后保留一个可发现、可拖动的 `MAO` 悬浮按钮。按钮释放在窗口左半区时侧边栏从左侧滑入，释放在窗口右半区时从右侧滑入。按钮的垂直位置和停靠侧属于本地交互偏好。

Foreman 对话不属于侧边栏栏目。Foreman 只在编排画布中出现，并且默认隐藏。Foreman 对话开关与侧边栏开关相互独立。

### 3.3 编排上下文接口

编排页提供以下用户动作。

- 在工厂层查看厂区聚合拓扑
- 双击厂区进入厂区层
- 返回工厂层
- 选择节点打开详情
- 拖动节点整理布局
- 平移、缩放和适应画布
- 打开或关闭 Foreman 对话
- 在节点详情中查看和选择 Harness
- 查看 Operator 主动发起的资源请求
- 通过 Foreman 描述目标，自动生成、应用、验证、运行和修复拓扑

用户不需要点击应用候选方案、批准运行、发布确认或人工确认来推进普通流程。需要人管理的入口包括工厂级边界、Foreman 运行入口、宪法和 Foreman 无法继续处理的异常。

### 3.4 上下文详情接口

看板卡片、资产行、日志条目、节点详情和异常上下文都可以打开渐进式详情。资源配置从 Operator 节点、Foreman 对话或日志上下文进入，不设置独立密码柜导航栏目。密钥值不得出现在节点、日志、资产或普通运行状态中。

## 4. 数据格式

### 4.1 图模型

UI demo 和正式适配层使用接近以下形状的数据。

```js
{
  scope: "factory" | "workarea",
  id,
  title,
  nodes: [
    {
      id,
      type,
      group,
      label,
      position: { x, y },
      status,
      metadata
    }
  ],
  edges: [
    {
      id,
      source,
      target,
      kind: "material" | "condition" | "default" | "aggregate",
      condition,
      status,
      metadata
    }
  ]
}
```

边必须由显式 `edges` 数据生成。禁止根据节点数组顺序自动拼接连续箭头。

### 4.2 节点类型边界

工厂层的 `nodes` 中 `type` 只能是 `workarea`。工厂层禁止出现 Operator、Machine、Source、Sink、Validation、Gate、Instance、Asset 或其他辅助节点。

厂区层的 `nodes` 中 `type` 只能是 `operator` 或 `machine`。厂区内禁止加入 Source、Sink、Validation、Gate、Instance、Asset 或第三类元素。Operator 与 Machine 是厂区画布的完整节点集合。

Machine 是否承担 sink 或交付语义由 Foreman 和项目配置根据现有执行关系判断。页面不提供人工 sink 开关，也不要求人决定交付角色。

### 4.3 布局状态

浏览器可以保存工厂层和每个厂区层的节点位置、缩放、平移、MAO 停靠侧、MAO 垂直位置和展开偏好。布局只服务于阅读和整理，不参与业务执行，不写入后端业务事实。

## 5. 行为

### 5.1 工厂层

工厂层展示输入入口、厂区节点、条件路径、默认路径、物料流转、聚合状态和交付结果的宏观关系。输入、输出和交付不转成可编辑的额外节点。厂区节点可以显示活动数量、运行、验证、修复、异常和最近输出的短摘要。

单击厂区节点显示摘要。双击厂区节点进入对应厂区层。工厂层与厂区层必须互斥显示。

### 5.2 厂区层

厂区层只展示 Operator 和 Machine。节点卡片只显示对应项目或职责、Harness 摘要和简短状态。复杂执行细节由 Operator 控制，进入节点详情、Foreman 对话和日志上下文，不把不属于项目能力的控制项塞入画布。

Operator 详情可以展示负责的项目或目标、Harness、输入输出契约、最近验证和最近资源请求。Machine 详情可以展示开源项目、Git 地址、上传文件或工作流文件、输入类型、输出类型和当前验证摘要。Machine 详情不允许人设置 sink。

### 5.3 Foreman 自治

Foreman 对话位于编排画布左侧，实时拓扑位于右侧。对话默认隐藏，隐藏只影响画布中的对话面板，不影响侧边栏。用户描述目标后，Foreman 可以自动解析目标、创建厂区、装配 Operator 和 Machine、检查 Harness 能力、应用拓扑、运行、验证、修复和恢复。

Foreman 可以处理普通连接调整、资源请求、机器替换、流程修改、条件分支和运行恢复。只要 Foreman 仍然可以运行，页面应展示其处理进度和结果，不把问题变成人工审批。

### 5.4 Operator 资源请求

Operator 负责发现缺少的密钥或资源并主动提出请求。Foreman 根据请求生成检查逻辑，定位使用范围和绑定对象，协调人补充资源，恢复运行并记录结果。检查逻辑可以包括存在性、权限、与 Machine 匹配、过期状态和当前 Agent 使用范围。UI 展示请求来源、用途、范围、Agent 生成的检查项和状态。

### 5.5 渐进式详情

第一层详情显示名称、节点类型、对应项目和短状态。展开后显示 Harness、输入输出契约、项目文件、运行状态、验证结果、资源请求和底层引用摘要。看板卡片、资产列表和日志列表默认只显示摘要。

### 5.6 资产和日志

资产页面展示输入素材、输出资产、知识资产、封存状态、来源运行、来源节点、Validation Epoch 和 Lineage。资产详情可以使用时间线或折叠区域显示来源关系。

日志页面统一展示运行日志、Foreman 操作、自动验证、自动修复、资源请求、节点失败、条件结果、实例崩溃、密钥缺失、宪法冲突、ChangeSet、Epoch 和 Lineage。日志不是审批队列。普通日志使用自动执行时间线，异常日志进入上下文处理。

### 5.7 画布交互

画布支持节点自由拖动、画布平移、缩放和适应画布。节点拖动位移必须按当前缩放换算，只更新当前节点，不改变其他节点或视图层级。拖动结束静默保存，不显示位置更新提示。切换工厂层和厂区层后恢复各自布局，刷新后尽量恢复仍存在的布局。

## 6. 错误

以下情况进入异常上下文。

- Foreman Harness 不可用
- Harness 能力不匹配或版本不适配
- Operator 主动报告资源缺失或过期
- 宪法冲突
- 自动修复耗尽
- 实例崩溃
- 条件表达式错误
- 连接无效
- 无法恢复的节点失败

只要 Foreman Harness 仍可运行，异常默认进入 Foreman 自动处理链路。只有 Foreman 无法继续、宪法明确阻止或用户主动打开时，才显示人介入入口。任何错误状态都不能转化为普通审批队列。

## 7. 验收测试

1. 本地打开 Web App 默认进入看板，无登录界面。
2. 侧边栏只有看板、编排、资产、日志四项且顺序正确。
3. MAO 按钮可拖动，停靠侧和垂直位置可以恢复。
4. 工厂层只显示厂区节点和显式边。
5. 双击厂区后只显示 Operator 和 Machine。
6. 厂区层不存在第三类节点。
7. 工厂层和厂区层互斥显示，返回后恢复工厂层。
8. 节点拖动不会跳动、重排其他节点或弹出位置提示。
9. 画布支持平移、缩放和适应画布。
10. Foreman 只在编排画布出现，并与侧边栏独立开关。
11. Foreman 可以自动应用 mock 结构，不出现普通业务审批按钮。
12. Foreman 和 Operator 都可以选择 Harness CLI。
13. Harness 选择允许本地发现和自定义适配器，不依赖封闭厂商枚举。
14. Operator 可以主动提出资源请求，Foreman 可以继续处理请求。
15. Machine 没有人工 sink 开关。
16. 看板、资产和日志采用摘要与上下文展开。
17. 应用不存在登录、用户、租户和多用户入口。
18. 浏览器控制台无未处理错误。
19. 项目根目录没有临时截图、Playwright 输出或浏览器缓存。

## 8. 错误示范与正确示范

### 8.1 错误示范

```text
工厂层显示 Source → Operator → Machine → Validation → Sink。
用户在 Machine 卡片上手动打开 sink 开关。
侧边栏列出 Machine、Operator、密钥、宪法和审批。
Foreman 生成拓扑后等待用户点击批准运行。
```

### 8.2 正确示范

```text
工厂层只显示厂区节点和显式物料或条件边。
进入厂区后只显示 Operator 与 Machine。
Machine 的交付语义由 Foreman 根据项目配置和执行关系判断。
Operator 发现资源缺口后主动提出请求，Foreman 继续处理。
用户只在工厂、Foreman、宪法或不可恢复异常处介入。
```

## 9. 风险与注意事项

- 现有执行图的动态关系可能比 UI 聚合模型更丰富，适配层应保留来源引用和状态来源，不能为了填满画布创造新节点。
- Harness 版本、启动参数和能力声明可能变化，UI 需要允许本地发现、替换和未知适配器状态。
- 浏览器布局恢复只保存布局和交互偏好，不能把运行状态、资产事实、密钥值或宪法状态写入布局存储。
- React Flow 或原生 SVG 都可以作为实现选择。实现细节可以调整，只要保留四入口、两层节点边界、Foreman 自治和本地单机场景。
- M18 任务应为未来控制面适配保留空间。遇到后端暂未直接暴露的事实，优先展示来源、当前状态和 Foreman 处理进度。


## 11. 真实 Web App 集成

M18 的正式用户入口由 `factory web` 启动。服务只绑定 `127.0.0.1`，页面和 API 使用同源请求，不提供登录、注册、账号、租户、邀请、远程 SaaS 或多用户会话。已有 `factory console` 保持兼容，继续承担受保护的旧控制台职责，不能被新 Web App 反向依赖。

默认入口为 `http://127.0.0.1:<port>/`。页面直接进入看板。默认端口由 `web.port` 配置，未配置时使用本地 Web App 默认端口。端口为 `0` 时允许测试环境使用系统分配端口。

### 11.1 API 边界

真实前端通过以下同源 API 读取现有控制面事实。

- `GET /api/web/bootstrap` 返回版本、运行连接状态、工厂图、厂区图、看板摘要、资产摘要、日志摘要、Harness 能力和当前宪法版本
- `GET /api/web/graph?scope=factory` 返回工厂层聚合图
- `GET /api/web/graph?scope=workarea&workarea=<line-key>` 返回指定厂区内部图
- `GET /api/web/assets?limit=<n>` 返回脱敏后的资产摘要和来源引用
- `GET /api/web/logs?limit=<n>&since=<cursor>` 返回事件、运行、验证、修复、资源请求和异常摘要
- `GET /api/web/talk` 返回 Foreman 对话时间线
- `POST /api/web/talk` 接受 `{ "text": "..." }`，使用本地用户身份调用现有 `talk.post_human`，把消息投递给常驻 Foreman。控制面不在此请求中调用模型 API
- `GET /api/web/harnesses` 返回本地 Registry 中的 OperatorRuntime 和适配器能力摘要
- `GET /api/web/constitution` 返回当前 Epoch、宪法版本、生效状态和最近变更摘要

成功响应使用 `application/json`。业务失败返回 `4xx` 或 `409`，body 至少包含 `code`、`message` 和脱敏后的 `detail`。服务端不得把密钥值、Authorization、Cookie、完整本地凭据路径或未脱敏 transcript 写入响应。

### 11.2 真实读模型

Web App 不新增 Factory 或 Workarea 数据表。后端适配层从当前 Epoch 的 Registry、Line、Station、Machine、OperatorRuntime、Agent、runs、run_nodes、events、wakes、talk_messages、beans 和 lineage 读取事实。

- 工厂层的 `workarea` 节点由现有 `Line` 聚合而来，节点 id 使用 Line key，节点位置只来自浏览器布局
- 厂区层的 Operator 和 Machine 节点由 Line step 引用的 Station、operator.runtime 和 machine 解析而来
- Operator → Machine 边来自 Station 的实际绑定
- Machine → Operator 边来自 Line step 的输入绑定、switch 条件、subline 关系或现有执行依赖
- 无法从后端事实推导的边不生成。适配层不得按节点数组顺序补连续箭头
- 节点与边状态来自运行记录、run_nodes、events 和 Foreman 会话。状态缺失时使用 `unknown`，不得伪造运行事实
- `scope`、`workarea`、节点类型和边类型由服务端校验

### 11.3 写入边界

普通浏览器操作只修改本地布局偏好。Foreman 目标、Harness 替换意图和资源请求通过现有 Foreman 邮箱或 talk 入口进入自治链路。浏览器不直接修改 Registry、RunPlan、Line、Machine、Operator、Secret 或 Constitution，也不直接调用 LLM API。

`POST /api/web/talk` 成功只表示消息已经写入本地 Foreman 输入队列。前端优先订阅 SSE 事件流，断线时使用 `Last-Event-ID` 或事件游标补齐结果。SSE 建立连接时必须先登记订阅再回放游标，回放与实时事件按序去重，不能因连接建立竞态丢失 Foreman 增量。serve 未运行、Foreman 不可用、宪法冲突和不可恢复异常必须进入异常上下文。

### 11.4 前端连接状态

首屏必须区分 `connecting`、`connected`、`degraded` 和 `exception`。静态 demo 可以在独立文件打开时使用显式 mock 数据，但 `factory web` 入口不能静默退回 mock。后端连接失败时显示当前错误和本地服务恢复入口，不能把 mock 状态显示成真实运行状态。

### 11.5 Bun 业务控制面

生产 Web API 使用 Bun 与 TypeScript。默认数据库为 `.factory/factory.db`，新增业务表与旧 SQLite 数据共存，旧事件、资产和 talk 数据通过只读兼容投影进入 Web App。

- `GET /api/v1/business-systems` 与 `POST /api/v1/business-systems` 管理业务系统聚合
- `GET /api/v1/projects` 与 `POST /api/v1/projects/intake` 接收仓库链接、项目文件引用或本地文件夹来源。Intake 成功后自动写入一条 Foreman 输入队列，厂长可继续识别项目、创建基础单元和搭建 DAG，即使当前还没有配置 Harness 也不能丢失这条意图
- 项目公共投影只保留来源种类和可移植引用，`local_folder` 与保险箱引用不会返回给浏览器或业务系统导出
- `GET /api/v1/business-systems/<id>` 返回业务系统、项目和 Artifact 摘要
- `GET /api/v1/business-systems/<id>/artifacts` 与 `/materials` 返回该业务系统闭包内的语义化记录。`/materials` 只返回 `record_type=material` 的来料或中间物料
- `GET/POST /api/v1/business-systems/<id>/conversions` 维护产物到物料的语义化转换记录。转换只引用记录或文件，不嵌入内容
- `GET /api/v1/business-systems/<id>/export` 导出可迁移的业务系统文档。本地主机路径和秘密值不会进入导出
- `POST /api/v1/business-systems/import` 接受同一业务文档并重建项目、记录、转换和 DAG，导入过程只使用可移植引用。导入前复用与图写入相同的节点唯一、边引用和无环校验，校验失败时不创建部分业务系统
- `GET /api/v1/business-systems/<id>/units` 返回由 Operator 与 Machine 组成的业务基础单元。项目来源的宿主路径只用于内部执行，公共投影只返回来源种类
- `GET/POST /api/v1/business-systems/<id>/runs` 创建和读取业务运行，`GET /api/v1/runs/<id>` 读取运行及 Operator 执行状态，`POST /api/v1/runs/<id>/dispatch` 通过官方 Operator Harness 推进待执行单元
- 业务运行按 DAG 前置边决定就绪状态。前置 Operator 完成后，其产物引用会合并到后继 Operator 的输入物料，控制面不会并发启动未满足依赖的单元
- 前置 Operator 失败或被阻塞时，所有受影响的后继 Operator 必须进入 `blocked`，业务运行进入 `failed`，不能因没有新的可调度单元而永久停留在 `running`
- `GET/POST /api/v1/projects/<id>/artifacts` 读写带摘要、用途、文件引用、输入输出和 lineage 的物料 JSON
- `GET/POST /api/v1/business-systems/<id>/graph` 读取或接收外部 Foreman 维护的 DAG 快照。写入时节点标识唯一，边必须引用存在节点，图必须无环
- `POST /api/v1/talk` 写入人的意图，`POST /api/v1/talk/reply` 接收外部 Foreman 的回复
- `GET /api/v1/foreman/inbox`、`POST /api/v1/foreman/inbox/<id>/claim` 和 `POST /api/v1/foreman/inbox/<id>/done` 提供 Foreman 的持久信息分配队列
- `POST /api/v1/foreman/inbox/<id>/dispatch` 将已入队意图交给配置好的官方 Harness。控制面只启动外部 Harness、转发事件和保存回复，不调用模型 API 或自建 Harness
- Operator Harness 可以输出 `mao.secret_request` 事件。控制面只创建项目资源请求和脱敏状态，用户在 UI 中补充所需资源，密钥值进入保险箱
- ACP 传输必须通过官方 JSON-RPC 会话边界完成 `initialize`、`session/new` 或 `session/load`、`session/prompt`。业务请求进入官方 Harness 的 stdin，`session/update` 通知进入 SSE。控制面只解析协议事件，不实现模型循环
- Harness profile 提供 digest 固定的 `image` 时，官方 CLI 或 ACP 进程通过当前 ContainerProvider 启动。Podman 使用受限 `podman run`，Kiri 使用受限 `kiri run` 或 `docker --context kiri run`，都使用非 root、只读根文件系统、`cap-drop ALL` 和 `network none`
- 启动官方 Harness 时只继承运行所需的基础环境和显式声明的变量。宿主环境中的其他密钥不会被批量转发
- `GET /api/v1/events` 使用 SSE 推送 `mao.event`，事件数据携带真实类型、序号和聚合摘要
- `GET /api/v1/runtime` 返回当前 Podman 或 Kiri 健康状态、machine 和 rootless 能力，结果在控制面短时间缓存，避免首屏重复探测
- `POST /api/v1/runtime/ensure-ready` 请求当前运行时就绪。Podman 在 Windows 尝试启动 machine，Kiri 的 Linux headless 模式尝试启动 `kiri daemon`，Kiri Windows 模式使用自己的 CLI/daemon 状态。不可用时返回 503 和可读原因，不把另一运行时的状态伪装成在线
- Windows 用户目录安装的 Podman CLI 可以通过 `MAO_PODMAN_EXECUTABLE` 指定，控制面启动时必须优先使用该路径，避免桌面进程继承旧 PATH 后误报运行时缺失
- `POST /api/v1/secrets/requests` 创建项目资源请求，`POST /api/v1/secrets/requests/<id>/value` 将值直接加密写入本地保险箱

密钥值只在保险箱写入请求中出现。数据库、事件、Harness、Foreman 和普通 UI 只接触保险箱引用与状态。UI 展示语义化字段、图表、时间线和文件引用，底层 JSON 不作为用户详情内容渲染。

业务记录的 JSON 契约保留 `record_type`（`artifact` 或 `material`）、`kind`、`ref`、`title`、`description`、`summary`、`purpose`、`status`、`producer`、`files`、`inputs`、`outputs` 和 `lineage`。`title`、`description` 或 `summary` 至少有一个面向人的说明。`files` 只保存引用、路径、媒体类型和摘要，不内嵌文件字节。记录由官方 Harness 产生或转换后写入，控制面只保存和分发这些描述，不执行记录中的指令。

Intake 会为每个项目建立一个 Operator 单元和一个 Machine 单元。业务运行只调度 Operator，Machine 作为被操作的软件项目和 DAG 节点存在。Operator Harness 输出 `mao.record` 事件后，控制面把记录写入该项目并通过事件流返回产物或物料状态。

容器运行时默认采用 Podman，也可以通过 `MAO_CONTAINER_RUNTIME=kiri` 选择官方 Kiri。Windows 上 Podman 由应用管理 machine，Kiri 使用自己的 WSL/daemon 和 Docker Engine API 兼容边界。沙箱保持非 root、只读根文件系统、能力降级、资源限制和无宿主套接字约束。Docker 只作为历史兼容 provider。

后端性能基线由 `backend/bench.ts` 生成。交互 API 的本地单用户 p95 目标为 200ms，Foreman 首个事件目标为 1s，事件增量延迟目标为 2s。读模型在一次请求内复用状态、Harness、运行和资产来源查询，业务系统级产物与物料使用一次 JOIN 读取，项目来源与资源请求使用批量投影和索引，事件流使用游标增量发送。

## 12. 真实 Web App 验收

迁移后的 Bun 验收入口为 `bun run acceptance:bun`，执行 `backend/tests`、`backend/runtime` 和 `backend/mrl` 的 TypeScript 测试。`bun run verify:bun` 执行完整 Bun backend 集合和 Web TypeScript 检查。测试夹具通过注入的官方 Harness process、ACP JSON-RPC stream 和 Podman/Kiri command runner 运行，不调用模型 API，不依赖 shell 拼接。Python 验收目录只作为历史证据保留。

- `factory web` 只监听本机回环地址，打开 `/` 直接返回 M18 前端
- `/api/web/bootstrap` 能从真实 Context 返回图、运行、资产、日志和宪法摘要
- 前端首屏由 API 数据驱动，不依赖写死的工厂节点或运行记录
- 双击厂区后请求对应 `workarea` 图，工厂层和厂区层互斥
- 节点移动、缩放、平移和 MAO 偏好仍只保存在浏览器本地
- Foreman 输入通过 `POST /api/web/talk` 进入现有消息链路，不调用控制面 LLM API
- API 不要求登录，不返回秘密值，不暴露多用户入口
- serve 关闭时前端展示真实异常，不伪造自治运行成功
- 本地浏览器可完成看板、编排、资产、日志和 Foreman 对话的真实读写闭环
- Intake 创建的项目会进入 BusinessSystem 图并通过事件流刷新
- Foreman 回复可以通过外部 Harness 回写同一 talk 线程
- 项目资源请求只显示项目用途、范围、检查项和状态，密钥值进入保险箱
- 当前容器运行时缺失时显示实际依赖状态，不把另一运行时状态伪装成在线
- `/api/v1/runtime` 与 bootstrap 的 Podman 或 Kiri 状态来自实际探测，不能用另一运行时在线状态替代
- `POST /api/v1/runtime/ensure-ready` 在 Windows 尝试启动已配置的 Podman machine。选择 Kiri 时，Linux headless 路径会在探测失败后尝试启动官方 `kiri daemon`，已经由发行版 systemd 服务托管时则直接复用该服务。其他 Linux Podman 路径只报告 rootless 服务状态
