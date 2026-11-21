# M18 本地 Web App 用户入口与自治编排规范

> 本规范定义本地 Web App 的交互边界。它把 BusinessSystem、项目来源、执行图、节点依赖、物料关系、运行状态和动态控制投影成用户可理解的工厂与厂区视图。BusinessSystem 是业务数据聚合，Line、Station、OperatorRuntime 和 Machine 继续承担执行事实。

## 1. 目的与范围

M18 提供一个面向本地部署的独立 Web App。默认模式下用户打开本地地址后直接进入看板，不经过登录、账号、租户或多用户流程。平台部署可以通过 API Bearer scope 开启受保护的业务系统和项目范围控制。Web App 是现有控制面的用户交互入口，浏览器中的工厂和厂区是执行图的聚合视图。

产品关系固定为以下逻辑链路。

```text
人 → 工厂与 Foreman
Foreman → 创建厂区、管理 Operator 和 Machine、运行、验证、修复与恢复
Operator → 项目执行、资源发现、资源请求与运行处理
Machine → Operator 调用的开源项目、工作流或本地工具
```

Foreman 是工厂创造者和自治中枢。只要其 Harness CLI 可以运行，Foreman 可以根据用户目标创建厂区、装配员工、调整执行关系、处理 Operator 资源请求、运行、验证、修复和继续推进。普通业务流程不进入人工审批队列。

M18 交付四个业务入口，顺序固定为看板、编排、资产、日志。设置作为最小的运行时和 Harness 依赖面板提供，不承载业务拓扑或独立的密钥柜导航。编排画布承担所有拓扑和节点配置，其他页面负责摘要和上下文进入。

## 2. 依赖

M18 依赖现有执行图和控制面能力，页面使用现有节点依赖、物料绑定、运行状态、动态控制、资产血缘、运行记录和宪法约束。Factory 和 Workarea 只是前端理解层名称，不能成为新的后端资源。

Foreman 与 Operator 的 Harness 通过官方 Harness 能力发现和声明式运行时接入。首批支持 Claude Code、Codex、Pi、Qwen Code、DeepSeek Harness、Kimi Code 和 ZCode。传输按官方 Harness 能力选择，Pi、Qwen Code 使用官方 CLI，DeepSeek Harness、Kimi Code 和 ZCode 使用官方 ACP stdio。控制面只负责启动、输入分发、事件接收和 transcript 解析。

M18 demo 可以使用原生 DOM、Pointer Events、SVG 和本地 mock 数据，也可以使用 React Flow。无论采用哪种实现，都必须保持本规范的图数据形状、节点类型约束和层级互斥关系。控制面不得调用 LLM API。

M18 的正式实现依赖 M17 阶段门通过。纯 UI demo 用于交互确认和浏览器回归，不替代 M17 或后端能力证据。

## 3. 接口

### 3.1 本地 Web App 入口

默认单机场景下页面打开后直接进入看板。应用不提供登录、注册、账号切换、租户、邀请、用户菜单或远程 SaaS 工作区。平台部署的身份和范围由服务端环境变量配置，浏览器不承担主体管理。

### 3.2 导航入口

侧边栏包含以下四个业务入口，设置作为依赖面板入口附加在其后。

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

厂区层只展示 Operator 和 Machine。节点卡片只显示对应项目或职责、Harness 摘要和简短状态。复杂执行细节由 Operator 控制，进入节点详情、Foreman 对话和日志上下文，不把不属于项目能力的控制项塞入画布。Foreman 只维护最简的业务架构边界，一个需要多个工具协作的完整职责应归并为一个 Machine，由 Operator 负责其内部工具选择、顺序和恢复。

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

资产列表显示总数，初始读取最近 100 项。用户可加载更早的资产或返回最新一页。收到资产新增事件后，页面更新总数和最新一页，继续保留用户正在查看的历史页。

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
17. 默认单机场景不存在登录、用户、租户和多用户入口。平台 scope 模式的访问控制由 API 服务端执行。
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
- React Flow 或原生 SVG 都可以作为实现选择。实现细节可以调整，只要保留四入口、两层节点边界、Foreman 自治和默认本地单机场景。
- M18 任务应为未来控制面适配保留空间。遇到后端暂未直接暴露的事实，优先展示来源、当前状态和 Foreman 处理进度。


## 11. 真实 Web App 集成

M18 的正式用户入口由 `factory web` 启动。默认服务绑定 `127.0.0.1`，页面和 API 使用同源请求，不提供登录、注册、账号、租户、邀请、远程 SaaS 或多用户会话。平台部署可以绑定非回环地址，并使用 `MAO_AUTH_TOKEN` 与 `MAO_AUTH_SCOPES_JSON` 执行 API 范围控制。已有 `factory console` 保持兼容，继续承担受保护的旧控制台职责，不能被新 Web App 反向依赖。

默认入口为 `http://127.0.0.1:<port>/`。页面直接进入看板。默认端口由 `web.port` 配置，未配置时使用本地 Web App 默认端口。端口为 `0` 时允许测试环境使用系统分配端口。

### 11.1 API 边界

真实前端通过以下同源 API 读取现有控制面事实。

- `GET /api/web/bootstrap` 返回版本、运行连接状态、工厂图、厂区图、看板摘要、资产摘要、日志摘要、Harness 能力和当前宪法版本
- 受保护的范围凭据可以读取同一个 bootstrap，但只得到其授权业务系统和项目的工厂图、资产、日志、对话与项目资源状态。控制面按 `business_system_id` 过滤业务事实，范围凭据不能看到全局 Harness 配置、其他业务系统或其他项目的对话。
- `GET /healthz` 返回控制面进程存活状态。`GET /readyz` 同时检查 SQLite 和当前 Podman/Kiri 运行时，运行时未就绪时返回 `503` 和恢复诊断
- `GET /api/web/graph?scope=factory` 返回工厂层聚合图
- `GET /api/web/graph?scope=workarea&workarea=<line-key>` 返回指定厂区内部图
- `GET /api/web/assets?limit=<n>` 返回脱敏后的资产摘要和来源引用，默认读取最近 100 条。SQLite 先按时间和标识倒序选择所需的业务资产，再用历史资产补足额度。响应沿用历史资产在前的展示顺序，附件授权只查询入选的资产。
- 资产附件只公开业务相对路径和已授权的下载引用。绝对宿主路径、`file://` 引用以及其他本地存储定位不会进入 Web 投影。
- 浏览器使用 `GET /api/web/bootstrap?assetLimit=100` 获取首屏资产。响应在 `assets` 中返回该窗口，同时提供总数 `assetTotal` 和 `assetOlderCursor`。省略 `assetLimit` 时，`assets` 返回完整列表。
- `GET /api/web/assets/page?limit=100&older=<cursor>` 返回 `items`、`total` 与 `olderCursor`。页大小接受 1 至 100，游标为空时读取最新一页。资产按历史库和业务库的展示顺序分页。在浏览较早资产期间新增的较新记录可回到最新页查看，继续翻页仍从当前游标读取更早的记录。
- `GET /api/web/logs?limit=<n>&since=<cursor>` 返回事件、运行、验证、修复、资源请求和异常摘要
- `GET /api/web/talk` 返回最近的 Foreman 对话，默认 80 条，`limit` 接受 1 到 1000。查询先取活动消息，条数不足时从归档对话补齐。页面引导响应使用相同查询。
- `GET /api/web/talk` 对范围凭据返回已授权业务上下文内的对话。进入具体厂区后，前端附带 `business_system_id`，以便保持当前 Foreman 线程上下文。
- `POST /api/web/talk` 接受 `{ "text": "...", "business_system_id": "...", "project_id": "..." }`，使用本地用户身份调用现有 `talk.post_human`，把消息投递给常驻 Foreman。范围凭据必须提供已授权的业务系统或项目上下文。控制面不在此请求中调用模型 API
- `POST /api/web/talk` 的响应必须包含本轮 `foremanTurn` 标识、持久化消息标识、当前状态和事件游标。响应只确认消息已经入队，不等待官方 Harness 完成，也不能把排队状态伪装成已回复。
- Foreman 尚未配置时，消息仍持久化进入队列，响应状态为 `waiting_foreman` 并带 `setup_required=true`。页面把它显示为配置前等待，不能显示为已完成
- 页面引导响应附带 SSE 游标和进行中的厂长回复。浏览器按邮箱消息 ID 累积 `foreman.reply.delta`，完成后用持久化消息替换流式内容，重连从已接收的事件继续。
- 从同一 Harness 更新提取的会话事件与可见回复增量按顺序写入同一个 SQLite 事务，提交后通知 SSE 连接。刷新页面或断线重连使用相同的增量记录。
- SSE 按游标分批从 SQLite 读取，单批最多 128 条，并随客户端读取推进。当前进程的事件直接唤醒等待中的流。CLI 在另一进程写入同一数据库的业务事件在空闲等待时每 250 毫秒检查一次。当前厂区收到对应 DAG 事件时重新读取图。断开连接或停止服务时释放订阅和计时器。
- 项目资源按项目和密钥名去重。资源卡显示发出请求的项目名称、密钥名称和用途，便于区分不同项目的同名密钥。工位发现缺失配置时等待本次执行所需的请求，全部可用后恢复调度，只向本次工位的 Harness 进程传入已绑定值。容器客户端连接和镜像认证配置由部署环境管理。消息和物料记录保存脱敏内容。
- 业务运行在创建时保存 DAG 依赖边和双向查询索引。编辑业务系统图会作用于之后创建的运行，已启动运行按自己的依赖继续推进。旧运行首次重新核对就绪状态时，从其冻结的 DAG 补建索引。
- 宿主机上的厂长 Harness 继承当前工厂的数据目录和数据库路径，CLI 把厂长意图和已授权业务运行写入同一数据库。服务进程利用 SQLite `data_version` 发现其他连接的提交并恢复待调度项。领取仍以数据库状态为准，服务重启后继续处理符合条件的待执行项。
- 手动导入支持仓库链接、本地目录引用和项目文件上传。仓库链接使用无凭证的 Git 地址，查询参数、片段和口令不进入项目登记。旧项目的仓库地址在 Web 投影和 Harness 上下文中移除凭证、查询参数和片段。项目文件通过 multipart 提交，内容进入 CAS。项目保留文件名、摘要及引用，厂长可取回有来源记录的文件。
- 接收项目时，业务系统、项目、基础单元、厂长请求和相应事件在同一个 SQLite 事务提交。提交后按记录顺序通知页面和派发厂长请求。目标业务系统不存在或任一记录写入失败时，事务回滚。
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

生产 Web API 使用 Bun 与 TypeScript。默认数据库为 `.factory/factory.db`，新增业务表与旧 SQLite 数据共存，旧事件、资产和 talk 数据通过只读兼容投影进入 Web App。使用独立历史库时，控制面以只读 SQLite URI 打开已归档的数据库文件，支持包含空格的本机路径。

- `GET /api/v1/business-systems` 与 `POST /api/v1/business-systems` 管理业务系统聚合
- `GET /api/v1/projects` 与 `POST /api/v1/projects/intake` 接收仓库链接、项目文件引用或本地文件夹来源。Intake 成功后自动写入一条 Foreman 输入队列，厂长可继续识别项目、创建基础单元和搭建 DAG，即使当前还没有配置 Harness 也不能丢失这条意图
- 厂区内的「添加项目」把目标业务系统 ID 随项目来源提交。仓库和文件夹采用 JSON 请求，浏览器上传文件采用 multipart 请求，两种入口都在当前厂区新建项目基础单元。项目登记事件刷新当前厂区，产物登记事件更新资产页。
- 项目公共投影只保留来源种类和可移植引用，`local_folder` 与保险箱引用不会返回给浏览器或业务系统导出
- `GET /api/v1/business-systems/<id>` 返回业务系统、项目和 Artifact 摘要
- `GET /api/v1/business-systems/<id>/artifacts` 与 `/materials` 返回该业务系统闭包内的语义化记录。`/materials` 只返回 `record_type=material` 的来料或中间物料
- `GET/POST /api/v1/business-systems/<id>/conversions` 维护产物到物料的语义化转换记录。转换只引用记录或文件，不嵌入内容。没有 `inputs` 或 `lineage` 的外部初始 Material/Artifact 不自动生成 Conversion，单独的 `producer` 也不构成输入来源
- 厂区的物料转换面板打开时读取该业务系统的记录，以输入引用、转换说明和状态、输出引用构成流程图。当前厂区收到转换事件时重新读取该面板。资产列表首选记录标题，其次使用业务摘要，记录标识可在详情中查看。
- `GET /api/v1/business-systems/<id>/export` 导出可迁移的业务系统文档。输出保留仓库、内容寻址文件及其他可移植引用。本机路径、仓库凭据、项目密钥值和 Harness 会话号在导出时剥离或脱敏。本地文件夹保留待接入引用
- `POST /api/v1/business-systems/import` 在一个事务中重建项目、来源、运行单元、物料记录、转换和 DAG，并重写相关 ID 与来源引用。导入前后复用图写入的节点唯一、边引用和无环校验。密钥按项目所需资源重新创建为待配置请求。历史运行作为归档来源保留，导入后的新运行使用重建的 DAG
- `GET /api/v1/business-systems/<id>/units` 返回由 Operator 与 Machine 组成的业务基础单元。项目来源的宿主路径只用于内部执行，公共投影只返回来源种类
- `PATCH /api/v1/business-systems/<id>/units/<unit-id>/binding` 更新该业务系统的 Operator。请求字段为 `harness_runtime` 和可空的 `model`。厂区节点详情和厂长的 `business bind` 使用同一入口。清空模型后继承该 Harness 配置的模型。绑定随业务单元导出
- `GET/POST /api/v1/business-systems/<id>/runs` 创建和读取业务运行，`GET /api/v1/runs/<id>` 读取运行及 Operator 执行状态，`POST /api/v1/runs/<id>/dispatch` 通过官方 Operator Harness 推进待执行单元
- 业务系统可声明 `metadata.kernel = {registry_root, line, inputs?, factory_root?, variants?, budget_cap_usd?, sink_mode?}` 将 BusinessRun 绑定到 MAO Registry。运行创建前控制面加载真实 Snapshot，验证 Line、Station 和 Machine 引用，并把脱敏后的 line、epoch、station 与 machine 集合固定到该运行的 metadata。提供按 Line 端口分组的 legacy Bean `inputs` 时，运行会由既有 `Runner` 和 `LineRunner` 接管，Machine unit_run 进入 running，完成后以 legacy Bean 输出收口并继续业务 DAG。没有 inputs 的 kernel 绑定仍只执行快照校验，不得伪造 Runner/Gate 已执行。
- legacy Runner 的每个封存 Bean 输出必须登记为当前业务系统的 `mao.business-record/1` Material，文件引用复用内容寻址摘要并授予业务项目下载权限，同时写入一条 `legacy-run-output` Conversion。重复恢复或重试必须按 legacy Bean ID 幂等复用已有 Material，不能重复生成产物或 Conversion。
- kernel 输入可以引用当前业务系统的 `material://`、`artifact://` 或 `record://` 记录。记录带有 legacy Bean 时直接复用，其他记录必须通过已登记文件和 `legacy_type`、`legacy_meta`、`legacy_facts` 物化为封存 Bean，并登记 `material-to-legacy-bean` Conversion。缺少可验证 Bean 类型或文件授权时，运行在提交前失败。
- Web 编排页读取业务运行列表并显示每个运行的状态和单元数量。`POST /api/v1/runs/<id>/cancel` 取消仍可控的业务运行，`POST /api/v1/runs/<id>/retry` 为失败运行创建可追踪的重试运行。取消请求会先停止本次运行仍在等待或读取输出的官方 Harness，并等待其控制面清理完成后返回，旧进程不能覆盖 `cancelled` 状态。取消后的重试清除本次取消标记，重新使用原始输入和 DAG，每个 Operator 最多启动一次新的 Harness 尝试，不重复登记已完成的 Machine 或 Conversion。后端未提供运行汇总时，看板显示“待观测”，不把缺失状态渲染为零。
- 创建业务运行默认同时记录派发授权。请求使用 `dispatch: false` 时保留待派发状态，直到调用显式派发接口。历史运行缺少派发记录且没有已完成的 Operator 时，继续通过显式接口派发。
- 业务运行按 DAG 前置边决定就绪状态。每个未归档的 Operator 和 Machine 都在运行快照中拥有一个 `unit_run`。普通 Machine 在满足前置条件后由控制面执行确定性的项目来源激活，状态从 `queued` 进入 `running`，并持久化 `machine_attempts` 的 attempt、session 和终态。来源激活会验证本地文件夹与上传文件的可用性，仓库 URL 只作为已登记来源事实保留，实际项目操作仍由官方 Operator Harness 负责。控制面重启后按 `machine:<unit-run-id>` session 恢复未完成的 Machine，失败会写入 `machine.run.failed` 并通过业务运行的 retry 重新建立下一次 attempt。kernel `inputs` 绑定的 Machine 则进入 `running`，由 legacy `LineRunner` 驱动真实 Job、Gate 和 Bean 输出，完成或失败后再推进同一份业务 DAG。Operator 成功完成后，控制面通过该次运行的依赖边索引检查其直接后继。后继在全部前置基础单元完成后才能领用，输入先保留原有物料，再按依赖快照中的边顺序合并产物引用并去重。首次派发、启动恢复、资源恢复和失败会按完整运行重新核对就绪状态。
- 普通 Machine 来源激活成功后登记一份 `project.source` Material，作为 Operator 的稳定输入引用。重复恢复和重试复用同一项目来源 Material。取消运行会等待 Machine 来源准备和 Operator Harness 清理完成后再写入终态。
- 前置 Operator 失败或被阻塞时，所有受影响的后继 Operator 必须进入 `blocked`，业务运行进入 `failed`，不能因没有新的可调度单元而永久停留在 `running`
- `GET/POST /api/v1/projects/<id>/artifacts` 读写带摘要、用途、文件引用、输入输出和 lineage 的物料 JSON
- `GET /api/v1/projects/<project-id>/artifacts/<artifact-id>/files/<sha256>` 通过项目与记录的文件授权返回下载。资产界面展示文件名和已授权下载入口。
- `GET/POST /api/v1/business-systems/<id>/graph` 读取或接收外部 Foreman 维护的 DAG 快照。写入时节点标识唯一。带有节点类型的活动图必须引用当前业务系统的 Operator 或 Machine，声明的节点类型必须与基础单元一致，边必须引用图中存在的节点且不能形成自环，图必须无环。未带节点类型的历史快照可以继续使用 `operator` 和 `machine` 两个兼容别名，服务端会映射到当前基础单元，其他未知节点和边端点拒绝写入。

Operator 详情把生效的 Harness 与模型和共享连接配置分别呈现。运行在领用单元时解析绑定，并记录官方 Harness ID 和生效的模型 ID。官方 CLI 接收模型参数。ACP 使用 `session/new` 或 `session/load` 提供的 `model` 配置选项，并在 `session/prompt` 前确认选择。Kimi Code 通过 `--model MODEL acp` 接收模型，Pi 通过 `--model MODEL` 接收模型，Qwen Code 通过 `--model MODEL` 接收模型。无法使用明确绑定的模型时，执行会报告原因。
- `POST /api/v1/talk` 写入人的意图，`POST /api/v1/talk/reply` 接收外部 Foreman 的回复。没有厂长 profile 时返回 `waiting_foreman`，可信容器 profile 尚未配置 digest 镜像时返回 `waiting_foreman_runtime`，两种状态都保留可恢复的 `queued` 轮次，不能把未启动的业务报告为 `accepted`。
- scoped Foreman 的对话写入必须带 `business_system_id` 或 `project_id`，邮箱操作和 SSE 事件按该业务上下文过滤。全局对话读取保留给管理员。
- 业务系统、DAG、运行、基础单元和 Conversion 的公开响应统一经过字段级脱敏。平台 scoped viewer 不能读取平台机器、可执行文件和运行时连接细节，只有管理员可以读取或启动全局运行时。
- 项目来源的 `fileRef`、运行输入输出、Conversion 输入输出和 Lineage 只公开可移植业务引用。绝对宿主路径、`file://`、`data:` 和其他本地定位在公开投影中替换为脱敏占位值。
- `GET /api/v1/foreman/inbox`、`POST /api/v1/foreman/inbox/<id>/claim` 和 `POST /api/v1/foreman/inbox/<id>/done` 提供 Foreman 的持久信息分配队列
- `POST /api/v1/foreman/inbox/<id>/dispatch` 将已入队意图交给配置好的官方 Harness。控制面只启动外部 Harness、转发事件和保存回复，不调用模型 API 或自建 Harness
- 保存厂长 Harness 配置后，控制面立即重新扫描此前已入队的厂长消息并派发，不要求重启服务或用户再次点击 dispatch。服务启动和恢复周期会按 `claimed_at` 回收超过 `MAO_FOREMAN_CLAIM_TIMEOUT_S` 的失联轮次，默认超时为 900 秒，并记录 `foreman.message.requeued` 事件。
- 容器厂长轮次按隔离邮箱并发派发。使用本机可恢复会话的厂长轮次只在同一 Harness profile 内保持顺序，避免共享官方 session，同时不让一个 profile 的轮次阻塞其他 profile 或容器轮次。
- `native_host` Operator 或显式要求平台预取的来源使用平台拥有的 SourceFetcher。SourceFetcher 使用持久化 `FACTORY_ROOT/source-cache`，优先使用无交互 Git 浅克隆，宿主缺少 Git 时对 GitHub HTTP(S) 来源使用官方仓库元数据和 codeload 归档，并校验归档路径、大小和摘要。来源 facts 记录工具、来源摘要和 revision，缓存目录不随 Harness 轮次清理。非 GitHub 来源缺少 Git 时产生 `SOURCE_TOOL_UNAVAILABLE`，Operator 将运行持久化为 `waiting_resources`，为项目创建 kind 为 `source-tool` 的资源请求并保留来源与工具名称。管理员或厂长在宿主准备工具后调用资源就绪入口，平台把请求标记为可用、重新排队原 UnitRun 并自动恢复 Operator。该请求不要求写入密钥值。
- `trusted_container` Foreman 不依赖 SourceFetcher 预先下载仓库。仓库链接、上传文件和本地目录作为项目来源数据交给官方 Harness。Foreman 启动后可以在容器内以 root 使用 bridge 网络，自行安装 Git、Scion、ffmpeg、音频工具和项目自定义依赖，执行脚本、下载、检验和切换源码，并把需要多个工具协作的内部时序交给 Operator。平台只准备已登记的上传文件和本地目录挂载，工作区限制在该项目的容器目录内，不能访问宿主引擎套接字、宿主网络或工作区之外的宿主路径。平台不得在 Foreman 启动前替它决定项目内部工具和执行时序。
- Bun HTTP 服务开始监听后，按持久队列顺序派发仍为 `queued` 的 Foreman 消息，并按正常 DAG 就绪规则推进已授权运行中的 `queued` 单元。历史运行中已完成的 Operator 可作为先前派发的依据。已领用的 Foreman 消息保留 `claimed` 状态，每个新领用的 Operator 单元在 SQLite 保存执行进程归属。Linux 服务启动时在同一内核启动和 PID 命名空间内检查归属，只有系统确认原 PID 已退出才将其置为 `failed` 并记录 `CONTROL_PLANE_RESTART`，保留已登记产物与会话编号。外部执行结果由用户核对后决定后续动作。活跃、无法核实以及历史没有归属记录的 `running` 单元保留原状态。等待资源的单元继续通过现有资源请求流程恢复。容器化 Operator 在领取单元前检查运行时。检查失败、超时或服务停止时，单元保留 `queued` 状态。成功执行 `POST /api/v1/runtime/ensure-ready` 后，通过正常 DAG 就绪规则恢复已授权的排队工作。服务在五秒内复用确认在线的运行时状态。
- kernel 绑定的 Machine 运行在 `unit_run.session_id=legacy:<unit-run-id>` 下，并在 BusinessRun metadata 保存 legacy Run ID。控制面重启后按该 Run ID 调用既有 `LineRunner` 恢复，避免重新提交 Job；若崩溃发生在 Run ID 持久化之前，则启动一个新的 legacy Run 并把旧的 Machine 运行标为失败或由用户重试。
- Operator Harness 可以输出 `mao.secret_request` 事件。控制面只创建项目资源请求和脱敏状态，用户在 UI 中补充所需资源，密钥值进入保险箱
- ACP 传输必须通过官方 JSON-RPC 会话边界完成 `initialize`、`session/new` 或 `session/load`、`session/prompt`。业务请求进入官方 Harness 的 stdin，`session/update` 通知进入 SSE。控制面只解析协议事件，不实现模型循环。Pi 的官方 CLI 使用 `--print --mode json`，Qwen Code 的官方 CLI 使用 `-p PROMPT --output-format stream-json`，两者均由控制面解析 stdout JSONL。DeepSeek Harness、Kimi Code 和 ZCode 没有可用 ACP 时必须直接报告 Harness 不可用，不能回退到未确认的通用 CLI 参数。没有容器镜像时，Operator 和 Foreman 都可以发现宿主 PATH 中与声明 Harness 相匹配的官方 ACP 可执行文件。Operator 和 Foreman profile 提供 digest 固定的容器镜像时，镜像内的官方 ACP 入口属于该轮的 ACP 能力，不要求宿主 PATH 另有 ACP 可执行文件
- ACP 使用官方 `sessionId`。只有 Harness 在初始化中声明 `loadSession` 时才请求恢复会话，恢复时收到的历史通知不进入当前回复。`session/prompt` 响应中的 `end_turn` 结束当前轮，无须等待 stdout 关闭。其他停止原因、JSON-RPC 错误和提前 EOF 终止当前轮并保留原因。握手限时 30 秒，单条消息最大 1 MiB，启动前暂存最多 128 条通知和 4 MiB。
- Harness profile 提供 digest 固定的 `image` 时，官方 Operator CLI 和 ACP 使用活动容器运行时。启动前控制面检查本地镜像，缺失时按 GeoIP 选择 Docker Hub 镜像源拉取并再次校验原始 digest。拉取失败或摘要不一致时，运行保持未启动并返回明确的镜像诊断。Linux Kiri 默认以 `docker --host unix:///run/kiri/docker.sock run` 启动。Operator 沙箱使用非 root、只读根文件系统、`--init`、`--pids-limit 256`、`--tmpfs /tmp` 和 `cap-drop ALL`，默认网络为 `none`。可信容器 Foreman 使用同一宿主路径命名空间下的独立工作区，以 root 身份运行并允许在容器私有根文件系统内安装和执行项目工具。可信 Foreman 默认使用容器 `bridge` 网络，使其可以自行下载仓库、包和项目工具。该网络只属于容器，禁止 `host` 网络，不能访问宿主引擎套接字，也不能挂载工作区之外的宿主路径。若显式选择 `none`，可信 Foreman 仍保持离线运行。官方 Harness 子进程的 stderr 只保留有界诊断，并在进入事件、日志和错误状态前替换部署凭据及常见密钥字段
- 容器内的 `HOME`、XDG 配置和缓存使用本轮的 `/tmp`。控制面给每个容器分配名称，会话停止时清理该名称对应的容器。
- 容器 Operator Harness 选择 `egress` 时，启动器先确认隔离代理就绪，再把官方 CLI 的 JSONL 或 ACP 的双向标准流接入会话。出网目标取已保存的 Base URL 主机和部署方配置的 `MAO_HARNESS_EGRESS_HOSTS`。该路径使用本地固定的 `MAO_EGRESS_RELAY_IMAGE`。可信 Foreman 选择 `bridge` 时不经过该白名单代理，由容器网络提供下载自由度。会话结束或取消时清理隔离代理及容器。 [出网规范](egress.md)
- Windows 原生 Claude Code 直接通过 PATH 找到的 `claude.exe` 启动。PATH 命中 npm 的 `claude.cmd` 时，优先启动同一安装目录下官方包附带的 `node_modules/@anthropic-ai/claude-code/bin/claude.exe`，使包含换行的提示词原样进入 CLI。找不到原生文件时沿用 PATH 启动。
- 取消 Windows 原生 Harness 会终止控制面本次启动且仍在运行的进程树。取消官方 CLI 会话也会取消正在等待的输出读取，避免 npm 命令包装器的子进程持有管道导致会话无法结束。
- 带 `image` 的 Foreman 和 Operator 会在启动官方 Harness 前调用当前 ContainerProvider 的 readiness。并发启动共享一次 readiness promise，失败时不会启动 Harness
- Linux Kiri 与 Podman 中的容器厂长通过当前轮的文件邮箱管理业务。容器获得 `/mao/foreman/requests` 写入权限和 `/mao/foreman/responses` 读取权限。请求包含 `id`、`op` 和 `args`，写入临时文件后以 0644 权限原子重命名为 `<id>.json`。MAO 调用固定的业务 API 来查询工位、更新 DAG、绑定 Operator、登记产物与转换、派发运行，并在同名响应文件返回结果。项目接收轮的操作范围是该项目及其所属业务系统，普通厂长轮可操作当轮列出的业务系统。文件通过引用登记。正常结束后处理已提交请求，取消时停止接收新请求，然后清理本轮目录。官方 Harness 子进程的有界 stderr 诊断遵循与邮箱、事件相同的凭据脱敏边界。
- 厂长邮箱除了写入业务图、绑定、记录、转换和运行，还可以读取 `business.runs`、`business.artifacts`、`business.materials` 和 `business.conversions`。返回值只保留状态、标识、引用和面向人的摘要，厂长据此继续执行失败恢复和转换维护。
- 厂长发现缺少必需配置时，使用邮箱操作 `project.secret.request` 为当前项目申请一个密钥，参数包括 `projectId`、`name` 和可选的 `purpose`、`scope`。响应只含请求 ID 和状态。项目接收轮只允许当前项目，普通对话轮可以操作该轮列出或新登记的业务系统中的项目。Web 收到 `secret.requested` 后更新厂长面板的项目资源入口，用户可查看请求项目和密钥名并在资源抽屉提交配置。
- 原生厂长通过 `factory project secret request <project-id> --name <KEY> --purpose <用途>` 提交同样的项目资源请求。每次指定一个实际缺少的配置，返回请求 ID 和状态。
- 在普通厂长对话中，`project.register` 使用 `repoUrl` 和可选的 `name`、`systemId` 登记仓库，返回 `projectId` 和 `systemId`。提供 `systemId` 时，该系统必须在当前轮范围内，包含本轮新建的系统。省略 `systemId` 时创建业务系统。可在同一轮使用返回的 ID 配置 Operator、DAG 和运行。省略名称时使用仓库路径最后一段并去除 `.git`。链接须为 HTTP(S)、SSH 或 Git URL，且不携带用户信息、查询参数或片段。登记后发布项目事件，当前厂长继续处理本轮。
- Harness 配置的 `session_timeout_s` 为单轮会话的超时秒数，适用于 Foreman 与 Operator 启动官方 CLI、建立 ACP 会话和读取回复。现有配置与新建时省略此值的配置默认 600 秒。接口接受 1 至 2147483 的整数，更新时省略该项保留已保存值。超时记录 `SESSION_TIMEOUT` 并取消本轮子进程，后续已排队的工作继续处理。配置页的连通性验证有独立超时。
- 同一 Foreman profile 的对话共享一条全局 transcript。容器厂长和没有可恢复官方会话的本机厂长从 SQLite 读取当前来信之前已完成的最近四轮对话，项目接入轮次也包含在其中，按入队顺序作为上下文数据传入。每条用户消息和回复最多读取一千个字符，并沿用厂长回复的脱敏规则。失败、取消和未完成轮次不进入历史。
- 本机官方 Harness 优先使用官方协议确认的会话 ID 恢复对话。控制面生成的内部临时 ID 只能用于当前轮诊断，禁止保存到恢复游标或回复元数据。已保存会话恢复失败且尚未产生工具副作用时，控制面清除无效游标并自动以有限 transcript 重试当前轮一次。已经产生工具、记录、密钥请求或其他业务副作用的轮次保留失败终态，避免重复执行。
- 本机厂长在普通对话中使用 `factory project register` 将仓库登记到指定业务系统或新建系统，并在同一轮继续搭建。修改 DAG 前，厂长可通过 `factory business graph <id>` 读取已有节点与边。接入已登记项目时，提示提供项目 ID、所属业务系统 ID 和来源说明。
- 启动官方 Harness 时只继承运行所需的基础环境和显式声明的变量。宿主环境中的其他密钥不会被批量转发
- 无桌面部署可为厂长的 Claude Code 提供 `MAO_FOREMAN_ANTHROPIC_API_KEY`，并为 `codex exec` 提供 `MAO_FOREMAN_CODEX_API_KEY`。MAO 将其分别传给对应的 `ANTHROPIC_API_KEY` 或 `CODEX_API_KEY`，同样用于厂长的连通性验证。缺省匹配官方 HTTPS 服务。自定义 Base URL 须与部署方设置的 `MAO_FOREMAN_ANTHROPIC_BASE_URL` 或 `MAO_FOREMAN_CODEX_BASE_URL` 匹配。页面仅显示部署凭证是否可用于当前配置。厂长的实时回复、事件和会话标识会屏蔽已传入的凭证值。
- 其他官方 Harness 的厂长凭据只能通过部署方显式设置 `MAO_FOREMAN_CREDENTIALS_JSON` 映射，映射必须声明 Harness ID、官方环境变量名、来源环境变量或项目 Vault 引用和可选 Base URL。控制面不猜测厂商变量、CLI 参数或认证方式，缺少声明时明确显示凭据未配置。
- `POST /api/web/harnesses/verify` 为配置创建一次连通性检查，返回 `202` 和 `requested` 状态。同一配置的并发请求复用当前检查。检查通过官方 CLI 或 ACP 开一个新会话，在临时工作目录请求固定的简短文本。项目数据、保险箱凭据和已有会话都不进入检查。启动带 `image` 的 Harness 前同样检查运行时就绪。
- 连接就绪、会话启动及响应共用 45 秒时限。收到预期文本且会话正常结束后保存 `verified`，其他情况保存 `failed` 和固定说明。`harness.profile.verification.completed` 事件和配置读模型只提供验证状态、说明和时间。重新保存配置将取消原检查，旧结果不会覆盖新配置。
- `GET /api/v1/events` 使用 SSE 推送 `mao.event`，事件数据携带真实类型、序号和聚合摘要
- SSE 发生读取错误时，前端连接状态进入 `degraded`，恢复收到事件后回到 `connected`。发送 Foreman 消息失败时保留输入内容并显示错误，不能清空输入后伪装成已提交。
- `GET /api/v1/runtime` 返回当前 Podman 或 Kiri 健康状态、machine 和 rootless 能力，结果在控制面短时间缓存，避免首屏重复探测。Linux headless Kiri 使用 `/run/kiri/docker.sock` 或显式 `MAO_KIRI_SOCKET`，WSL 中若宿主 80/443 已占用，应通过 Kiri systemd 环境设置 `KIRI_HTTP_PORT` 与 `KIRI_HTTPS_PORT` 后再启动 bridge 网络
- `POST /api/v1/runtime/ensure-ready` 请求当前运行时就绪。Linux Kiri 先探测套接字并复用在线服务，需要时请求 systemd 启动官方 `kiri.service`，包括自动发现的 headless Kiri `/var/lib/kiri-daemon/.kiri/run/docker.sock`。自定义套接字使用部署方的服务管理。Linux Podman 不满足 rootless 条件时返回明确的 MAO 用户和 `systemctl --user enable --now podman.socket` 恢复诊断，不把 rootful 或未知状态报告为在线。不可用时返回 503 和诊断。

并发的运行时状态查询共用一次在途探测，结果缓存 5 秒。主动就绪检查在并发的被动探测期间完成时，后返回的被动探测使用最新的就绪结果。
- Windows 用户目录安装的 Podman CLI 可以通过 `MAO_PODMAN_EXECUTABLE` 指定，控制面启动时必须优先使用该路径，运行时健康检查和带镜像的官方 Harness/ACP 启动必须复用同一解析结果，避免桌面进程继承旧 PATH 后误报运行时缺失
- `POST /api/v1/secrets/requests` 创建项目资源请求，`POST /api/v1/secrets/requests/<id>/value` 将值直接加密写入本地保险箱。kind 为 `source-tool` 的资源使用 `POST /api/v1/secrets/requests/<id>/ready` 标记宿主工具已准备，不接收密钥值。资源可用后，SQLite 在同一事务内更新请求状态、记录可用事件并重新排队所需请求均已可用的工位。事件推送与派发在提交后执行。若保险箱已写入而 SQLite 提交失败，用户可重新提交同一请求。
- 密钥请求状态支持 `requested`、`available`、`denied`、`revoked` 和 `expired`。已经 `available` 的请求不能覆盖写入，轮换必须先调用 `rotate` 生成新的请求和保险箱引用。`deny`、`revoke`、`rotate` 和 `audit` 路由只返回请求标识、状态和脱敏审计记录，审计记录不能包含密钥值。Operator 读取可用密钥时登记使用事件，并按项目请求和工位绑定范围恢复运行。
- SecretRequest 元数据与审计明细在存储边界统一脱敏，历史记录读取也重新经过脱敏，避免内部 Harness 事件绕过 Web 路由保护。
- 服务启动恢复时，已授权运行中等待资源的工位在所有已绑定的本项目请求均为可用时重新排队，再按 DAG 派发。缺少资源关联、请求仍未完成或归属其他项目的工位继续等待。

本地保险箱将加密记录写入同目录的私有临时文件，再原子替换正式文件。首次写入时允许正式文件缺失。读取出错或记录格式损坏时停止写入，保留正式文件供恢复。

本机回环地址默认采用单用户模式。设置 `MAO_AUTH_TOKEN` 后，API 和 SSE 必须使用对应的 Bearer token，写请求还需提供 `X-MAO-CSRF`，值由 `MAO_CSRF_TOKEN` 指定。绑定到非回环地址时也必须配置身份验证。CORS 不再默认开放到任意来源，跨源访问需要显式设置 `MAO_CORS_ORIGIN`。

平台部署可以设置 `MAO_AUTH_SCOPES_JSON` 开启多主体范围控制。对象键是 Bearer token，值支持 `role`（`admin`、`foreman`、`operator`、`viewer`）、`business_systems` 和 `projects` 数组，数组中的 `*` 表示该维度的显式全范围授权。启用后即使请求来自回环地址也必须提供匹配的 Bearer token。业务系统、项目、运行和密钥请求按范围检查，DAG、Conversion、Harness 配置和厂长邮箱控制需要 `foreman` 或 `admin`，Harness 配置与业务系统导入需要 `admin`。范围凭据查询全局聚合投影会被拒绝，避免通过列表接口侧漏其他业务系统。

密钥值只在保险箱写入请求中出现。数据库、事件、Harness、Foreman 和普通 UI 只接触保险箱引用与状态。UI 展示语义化字段、图表、时间线和文件引用，底层 JSON 不作为用户详情内容渲染。

业务记录的 JSON 契约版本固定为 `mao.business-record/1`，保留 `record_type`（`artifact` 或 `material`）、`kind`、`ref`、`title`、`description`、`summary`、`purpose`、`status`、`producer`、`files`、`inputs`、`outputs` 和 `lineage`。`title`、`description` 或 `summary` 至少有一个面向人的说明。`files` 只保存引用、路径、媒体类型和摘要，不内嵌文件字节。`inputs` 和 `lineage` 引用必须属于当前业务系统，未知或跨业务引用拒绝写入。记录由官方 Harness 产生或转换后写入，控制面只保存和分发这些描述，不执行记录中的指令。

下游 Operator 获得输入引用及当前业务系统内匹配的记录说明和文件引用。数据库先按记录 ID 解析，再按引用查询该业务系统内最新匹配。未登记的输入仍按原引用传递。

本地 Linux Kiri 和 Podman 工位可把本轮文件写入 `/mao/outputs`，通过 `mao.record` 的 `files` 项声明 `staged` 文件名和可选显示路径。控制面在官方 Harness 成功结束并关闭会话后检查普通文件，流式计算摘要并拒绝包含本轮注入凭据的文件。官方 Harness 没有产生有效 `mao.record` 时，Operator 运行失败，不能仅凭会话正常结束标记完成。业务记录、文件授权、成功状态和对应事件在同一事务提交。后继获得稳定记录 ID，并从同一业务系统的输入记录读取已授权的 `/mao/inputs/<序号>` 只读挂载。运行结束清理暂存区。

已校验的业务包导入恢复随包交付的文件授权。直接导入 JSON 保存文件引用。备份记录活动文件授权和对应 CAS 文件。

厂长与 Operator 使用同一份项目来源投影。仓库 URL 的密码和凭据查询参数在交给 Harness 前移除。容器运行的 `repo_url` 来源先由控制面用无交互 Git 浅克隆固定到具体 commit，再以 `/mao/sources/<序号>` 只读挂载，并在轮次 facts 中保留 commit。公开来源不携带凭据，当前未绑定官方 Git 认证的私有仓库必须明确失败，不能把凭据写进 URL。上传文件使用内容摘要引用，可通过项目文件路由读取。本地 Linux Kiri 和 Podman 中，接收项目的厂长和执行该项目的 Operator 都可在来源摘要中看到文件名或文件夹名，以及 `/mao/sources/<序号>` 下的只读位置。每个位置只挂载该项目登记的上传文件或用户选择的本地目录。宿主目录须为规范绝对路径，符号链接路径及与工厂存储重叠的目录不可挂载。本地目录沿用宿主文件访问权限。私有上传文件按轮暂存，正常结束、失败或取消后清理。主机上的 Harness 可读取用户提交的原始文件夹位置，其他容器运行方式使用来源引用。

Intake 会为每个项目建立一个 Operator 单元和一个 Machine 单元。业务运行同时保留两类基础单元的生命周期，Machine 负责项目来源激活，Operator 负责通过官方 Harness 操作软件项目。Operator Harness 输出 `mao.record` 事件后，控制面把记录写入该项目并通过事件流返回产物或物料状态。

容器运行时默认采用 Podman。设置 `MAO_CONTAINER_RUNTIME=kiri` 后，Linux 通过 `/run/kiri/docker.sock` 或自动发现的 headless Kiri 私有套接字与官方服务通信。Windows 上 Kiri 使用自己的 WSL 后端。沙箱保持非 root、只读根文件系统、能力降级、资源限制和无宿主套接字约束。

前端 API 请求可从 `VITE_MAO_AUTH_TOKEN`、`VITE_MAO_CSRF_TOKEN` 或本地 `mao-auth-token`、`mao-csrf-token` 存储读取平台凭据。事件流使用带 Bearer header 的 fetch SSE，并从最后游标自动重连，不能依赖无法附加凭据的原生 EventSource。

后端性能基线由 `backend/bench.ts` 生成。交互 API 的本地单用户 p95 目标为 200ms，Foreman 首个事件目标为 1s，事件增量延迟目标为 2s。读模型在一次请求内复用状态、Harness、运行和资产来源查询，业务系统级产物与物料使用一次 JOIN 读取，项目来源与资源请求使用批量投影和索引，事件流使用游标增量发送。

厂区和业务导出查询按业务系统 ID 限定项目、来源与密钥请求。工厂图只读取业务系统标识、名称、说明和状态，并用 SQL 聚合项目数量。普通厂长轮次使用同一份业务系统摘要。全量业务系统接口仍返回已保存的 DAG。运行历史用两次查询读取运行和对应基础单元，结果维持原有顺序。DAG 校验按边累计入度并迭代移除就绪节点，时间复杂度随节点和边线性增长。

Operator 完成时，SQLite 在同一个事务里保存运行输出、用状态索引汇总业务运行状态并同步基础单元状态。普通成功路径按直接后继及其前置依赖读取数据，长链中的每次完成无需读取整个运行的单元列表。

### 11.6 Harness 配置与对话反馈

配置厂长或 Operator 时，首屏只要求三类连接信息。用户可以填写或选择 `Base URL`、项目资源中的 `API key` 和 `Model`。每个字段都提供已保存配置的可选项。选择已保存项只引用其保险箱引用或脱敏值，页面和 API 永远不返回 API key 明文。Harness 适配器、运行时和连接方式由所选官方 Harness 能力解析，页面不能要求用户填写 CLI 参数或内部协议细节。

运行参数归入“设置”的共享高级配置，厂长和 Operator 默认读取同一份配置。共享配置包括 `thinking_effort`、`context`、`image`、`network`、`mode`、`max_turns`、`session_timeout_s`、`session_budget_usd`、`daily_budget_usd`、`max_attempts` 和 `backoff_s`。页面应使用可读标签显示这些值，不把高级参数堆叠在基础连接表单中。除非用户明确保存角色覆盖值，Foreman 与 Operator 使用共享值。Foreman 的 `thinking_effort` 默认值为 `max`，历史配置缺少该字段时也按 `max` 读取并在下一次保存时补齐。

Foreman profile 的 `execution_mode` 默认为 `trusted_container`，并且要求 digest 固定的官方 Harness 镜像。镜像尚未配置或运行时未就绪时，bootstrap 和对话轮次分别报告 `waiting_configuration` 或 `blocked_environment`，消息保持可恢复的 `queued` 状态，业务结果不能标记为已应用。显式选择 `native_host` 才允许使用本机官方 Harness。可信容器以 root、bridge 和项目独立工作区运行，前端显示该运行模式和阻塞原因，但不把容器内工具安装细节拆成额外的用户配置项。

项目主动申请的密钥在 UI 中标记为“项目需要配置的资源”或等价文案，显示项目、用途、范围和状态。不得把资源请求显示成 Agent 向用户索取私人密钥，也不得要求用户预先配置未被当前项目触及的密钥。

每一轮 Foreman 对话必须持久化 `queued_at`、`started_at`、`last_event_at`、`completed_at`、`elapsed_ms` 和 `status`，并通过 `inbox_id` 把用户消息、流式回复、最终回复和终态绑定到同一轮。`status` 至少支持 `queued`、`processing`、`waiting_foreman`、`completed`、`failed`、`cancelled`。业务写入结果另存为 `business_status` 和脱敏的 `business_reason`，至少区分 `not_started`、`in_progress`、`applied`、`no_change`、`blocked_environment`、`blocked_input` 和 `failed`。前端在 `queued` 或 `processing` 状态显示从 `queued_at` 或 `started_at` 起算的实时计时，在重连时使用服务端时间戳恢复计时，不因刷新归零。收到 `foreman.reply.delta` 时更新 `last_event_at`，完成事件到达后用持久化回复替换临时增量，并在对话列表中紧跟对应的用户消息显示。

若超过 `session_timeout_s` 仍未完成，控制面必须取消本轮官方 Harness 子进程，将状态写为 `failed`，错误原因为 `SESSION_TIMEOUT`，并通过 SSE 或下一次游标读取把终态推送给页面。Foreman 未配置时使用 `waiting_foreman`，页面显示配置入口和已等待时长。任何不可恢复的启动、协议、退出或持久化错误都必须在本轮内写入 `failed` 和脱敏原因，禁止让用户看到无限期的“处理中”。

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
- Harness 基础配置只要求 Base URL、API key 和 Model，并为每个字段提供已保存配置的可选项，页面不显示 API key 明文
- Foreman 与 Operator 读取同一份设置页高级运行参数，Foreman 默认 `thinking_effort=max`
- Foreman 对话在页面显示入队、处理、等待、完成或失败状态及持续时间，刷新或 SSE 重连后计时连续
- 官方 Harness 超时或退出会在 `session_timeout_s` 内进入带原因的失败终态，页面不会无限显示处理中
- 带镜像的 Operator 启动前会等待运行时 readiness，运行时不可用时不启动 Harness。镜像内声明的官方 ACP 能力不依赖宿主 PATH
- 并发的 runtime readiness 请求与带镜像的业务启动共享一次探测和启动操作
- 项目资源请求只显示项目用途、范围、检查项和状态，密钥值进入保险箱
- 当前容器运行时缺失时显示实际依赖状态，不把另一运行时状态伪装成在线
- `/api/v1/runtime` 与 bootstrap 的 Podman 或 Kiri 状态来自实际探测，不能用另一运行时在线状态替代
- `POST /api/v1/runtime/ensure-ready` 在 Windows 请求已配置的 Podman machine 就绪。Linux Kiri 验收覆盖标准和 headless 私有 socket 的官方服务复用与启动失败诊断。Linux Podman rootless 不满足时返回明确的 MAO 用户服务恢复命令，不把 rootful 或未知状态报告为在线。
