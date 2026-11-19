"use strict";

const app = document.getElementById("app");
const LAYOUT_KEY = "mao-m18-web-layout";
const STATUS_LABELS = {
  connected: "在线",
  degraded: "降级",
  exception: "异常",
  connecting: "连接中",
};
const NAV = [
  ["dashboard", "看板", "概况"],
  ["compose", "编排", "工厂与厂区"],
  ["assets", "资产", "输入与输出"],
  ["logs", "日志", "运行与自治"],
];

const storedLayout = loadLayout();

const state = {
  page: "dashboard",
  connection: "connecting",
  error: "",
  bootstrap: null,
  graph: null,
  scope: "factory",
  workarea: null,
  selected: null,
  detailOpen: false,
  foremanOpen: false,
  sidebarOpen: false,
  sidebarDock: storedLayout.sidebarDock || "left",
  mao: storedLayout.mao || { top: Math.round(window.innerHeight * 0.5) },
  layout: storedLayout,
  pan: { x: 50, y: 70 },
  zoom: 1,
  dragging: null,
  panning: null,
  talkText: "",
  talkState: "",
  talk: [],
  harnessChoice: {},
  activeLog: null,
  activeAsset: null,
  activeSecret: null,
  foremanHarnessChoice: "",
};

function loadLayout() {
  try {
    const raw = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}");
    return raw && typeof raw === "object" ? raw : {};
  } catch (_error) {
    return {};
  }
}

function saveLayout() {
  const payload = { sidebarDock: state.sidebarDock, mao: state.mao, graphs: state.layout.graphs || {} };
  localStorage.setItem(LAYOUT_KEY, JSON.stringify(payload));
}

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safe(value, fallback = "—") {
  return value == null || value === "" ? fallback : escapeHtml(value);
}

function statusLabel(value) {
  return STATUS_LABELS[value] || value || "未知";
}

function statusTone(value) {
  return ["running", "connected", "ready", "done"].includes(value) ? "good" :
    ["repairing", "degraded", "waiting", "pending"].includes(value) ? "warn" :
    ["exception", "failed", "FAILING"].includes(value) ? "bad" : "quiet";
}

function currentGraph() {
  return state.graph || (state.bootstrap && state.bootstrap.graph && state.bootstrap.graph.factory) || { nodes: [], edges: [] };
}

function graphKey(graph = currentGraph()) {
  return graph.scope + ":" + graph.id;
}

function graphLayout(graph = currentGraph()) {
  const key = graphKey(graph);
  state.layout.graphs ||= {};
  state.layout.graphs[key] ||= { nodes: {}, pan: { x: 50, y: 70 }, zoom: 1 };
  return state.layout.graphs[key];
}

function graphNodes(graph = currentGraph()) {
  const layout = graphLayout(graph);
  return (graph.nodes || []).map((node) => {
    const stored = layout.nodes[node.id];
    return stored ? { ...node, position: { x: stored.x, y: stored.y } } : node;
  });
}

function graphNode(id, graph = currentGraph()) {
  return graphNodes(graph).find((node) => node.id === id) || null;
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { Accept: "application/json", ...(options.headers || {}) }, ...options });
  let body = null;
  try { body = await response.json(); } catch (_error) { body = null; }
  if (!response.ok) {
    const error = new Error(body && body.message ? body.message : "本地服务请求失败");
    error.payload = body;
    error.status = response.status;
    throw error;
  }
  return body;
}

async function boot() {
  try {
    state.bootstrap = await api("/api/web/bootstrap");
    state.connection = state.bootstrap.connection.state;
    state.talk = state.bootstrap.talk || [];
    state.graph = state.bootstrap.graph.factory;
    const layout = graphLayout(state.graph);
    state.pan = layout.pan || { x: 50, y: 70 };
    state.zoom = layout.zoom || 1;
    if (state.bootstrap.graph.workarea && state.scope === "workarea") state.graph = state.bootstrap.graph.workarea;
  } catch (error) {
    state.connection = "exception";
    state.error = error.message;
  }
  render();
}

async function refreshTalk() {
  try {
    state.talk = await api("/api/web/talk?limit=40");
    if (state.bootstrap) state.bootstrap.talk = state.talk;
  } catch (error) {
    state.talkState = error.message;
  }
  if (state.page === "compose") renderCompose();
}

async function openWorkarea(id) {
  state.scope = "workarea";
  state.workarea = id;
  state.selected = null;
  state.detailOpen = false;
  try {
    state.graph = await api("/api/web/graph?scope=workarea&workarea=" + encodeURIComponent(id));
    const layout = graphLayout(state.graph);
    state.pan = layout.pan || { x: 50, y: 70 };
    state.zoom = layout.zoom || 1;
  } catch (error) {
    state.error = error.message;
  }
  render();
}

function returnToFactory() {
  state.scope = "factory";
  state.workarea = null;
  state.graph = state.bootstrap && state.bootstrap.graph.factory;
  state.selected = null;
  state.detailOpen = false;
  const layout = graphLayout(state.graph);
  state.pan = layout.pan || { x: 50, y: 70 };
  state.zoom = layout.zoom || 1;
  render();
}

function navigate(page) {
  state.page = page;
  state.sidebarOpen = false;
  state.selected = null;
  state.detailOpen = false;
  if (page === "compose" && state.scope === "factory" && state.bootstrap) state.graph = state.bootstrap.graph.factory;
  render();
}

function render() {
  app.innerHTML = [
    renderSidebar(),
    renderMao(),
    '<main class="app-main">',
    state.error ? renderErrorBanner(state.error) : "",
    state.page === "dashboard" ? renderDashboard() : "",
    state.page === "compose" ? renderCompose() : "",
    state.page === "assets" ? renderAssets() : "",
    state.page === "logs" ? renderLogs() : "",
    state.activeSecret ? renderSecretContext(state.activeSecret) : "",
    "</main>",
  ].join("");
  bindShellEvents();
  if (state.page === "compose") bindCanvasEvents();
}

function renderErrorBanner(message) {
  return '<div class="error-banner"><span class="signal bad"></span><strong>本地服务异常</strong><span>' + safe(message) + '</span><button data-action="retry">重试</button></div>';
}

function renderSidebar() {
  const side = state.sidebarDock;
  return '<aside class="sidebar ' + side + (state.sidebarOpen ? " open" : "") + '" aria-label="主导航">' +
    '<div class="sidebar-brand"><div class="brand-mark">M</div><div><strong>MAO</strong><small>machine agent org</small></div><button class="icon-button" data-action="close-sidebar" aria-label="关闭侧边栏">×</button></div>' +
    '<nav class="nav-list">' + NAV.map((item) => '<button class="nav-item ' + (state.page === item[0] ? "active" : "") + '" data-page="' + item[0] + '"><span class="nav-glyph">' + item[0].slice(0, 1).toUpperCase() + '</span><span><strong>' + item[1] + '</strong><small>' + item[2] + '</small></span></button>').join("") + '</nav>' +
    '<div class="sidebar-foot"><span class="status-dot ' + statusTone(state.connection) + '"></span><span>' + safe(STATUS_LABELS[state.connection]) + '</span><span class="mono">LOCAL</span></div>' +
    '</aside>';
}

function renderMao() {
  const vertical = Math.max(42, Math.min(window.innerHeight - 42, Number(state.mao.top) || window.innerHeight / 2));
  const style = state.sidebarDock === "right" ? 'right: 16px; top:' + vertical + 'px;' : 'left: 16px; top:' + vertical + 'px;';
  return '<button class="mao-float ' + state.sidebarDock + '" style="' + style + '" data-action="mao" aria-label="打开 MAO 侧边栏" title="拖动停靠 MAO，点击打开侧边栏"><span>MAO</span><i></i></button>';
}

function renderDashboard() {
  const status = state.bootstrap && state.bootstrap.status || {};
  const graph = state.bootstrap && state.bootstrap.graph && state.bootstrap.graph.factory || { nodes: [] };
  const nodes = graph.nodes || [];
  const active = Number((status.runs || {}).RUNNING || 0) + Number((status.runs || {}).PENDING || 0);
  const exceptions = (status.alerts || []).length;
  const assets = (state.bootstrap && state.bootstrap.assets || []).length;
  const constitution = state.bootstrap && state.bootstrap.constitution || {};
  const secrets = state.bootstrap && state.bootstrap.secrets || [];
  const configuredSecrets = secrets.filter((item) => item.configured).length;
  return '<section class="page dashboard-page">' +
    '<div class="page-heading"><div><span class="eyebrow">FACTORY OVERVIEW</span><h1>看板</h1><p>工厂由 Foreman 驱动，人管理边界和方向。</p></div><div class="connection-pill ' + statusTone(state.connection) + '"><span class="status-dot"></span>' + safe(statusLabel(state.connection)) + '</div></div>' +
    '<div class="metric-grid">' +
    metricCard("当前运行", active, active ? "正在由员工处理" : "当前无运行", "running") +
    metricCard("自治完成", "—", "由 Foreman 会话统计", "blue") +
    metricCard("厂区", nodes.length, "从现有 Line 聚合", "violet") +
    metricCard("异常", exceptions, exceptions ? "需要进入日志上下文" : "Foreman 当前可继续", exceptions ? "bad" : "good") +
    '<button class="metric-card metric-action" data-action="secrets"><span class="eyebrow">密钥资源</span><strong class="metric-value violet">' + configuredSecrets + '/' + secrets.length + '</strong><small>点击打开密码柜摘要</small></button>' +
    '</div>' +
    '<div class="dashboard-columns"><section class="panel overview-panel"><div class="panel-head"><div><span class="eyebrow">WORKAREAS</span><h2>厂区</h2></div><button class="text-button" data-page="compose">打开编排 <span>→</span></button></div>' +
    (nodes.length ? '<div class="workarea-list">' + nodes.map((node) => '<button class="workarea-row" data-workarea="' + escapeHtml(node.id) + '"><span class="status-ring ' + statusTone(node.status) + '"></span><span class="row-main"><strong>' + safe(node.label) + '</strong><small>' + safe(node.metadata && node.metadata.brief) + '</small></span><span class="row-state ' + statusTone(node.status) + '">' + statusLabel(node.status) + '</span><span class="chevron">›</span></button>').join("") + '</div>' : '<div class="empty-state">当前 Registry 没有可展示的厂区。</div>') +
    '</section><section class="panel boundary-panel"><div class="panel-head"><div><span class="eyebrow">HUMAN BOUNDARY</span><h2>厂长边界</h2></div><span class="status-badge ' + statusTone(state.connection) + '">' + safe(statusLabel(state.connection)) + '</span></div><div class="boundary-line"><span>Foreman</span><strong>' + (status.supervisor && status.supervisor.online ? "运行中" : "等待本地服务") + '</strong></div><div class="boundary-line"><span>宪法版本</span><button class="link-value" data-action="constitution">' + safe(constitution.version, "未配置") + '</button></div><button class="boundary-line boundary-action" data-action="secrets"><span>密钥资源</span><strong>' + configuredSecrets + ' / ' + secrets.length + ' 已配置</strong><b>›</b></button><div class="boundary-line"><span>资产摘要</span><strong>' + assets + ' 项可见</strong></div><p class="boundary-note">Foreman 管理 Operator、Machine、运行、验证和修复。人只处理工厂边界与无法自动恢复的异常。</p></section></div>' +
    '<div class="dashboard-columns lower"><section class="panel recent-panel"><div class="panel-head"><div><span class="eyebrow">RECENT ACTIVITY</span><h2>最近动态</h2></div><button class="text-button" data-page="logs">查看日志 <span>→</span></button></div>' + renderLogRows((state.bootstrap && state.bootstrap.logs || []).slice(-5).reverse()) + '</section><section class="panel asset-panel"><div class="panel-head"><div><span class="eyebrow">ASSETS</span><h2>资产</h2></div><button class="text-button" data-page="assets">查看资产 <span>→</span></button></div>' + renderAssetRows((state.bootstrap && state.bootstrap.assets || []).slice(-4).reverse()) + '</section></div>' +
    '</section>';
}

function metricCard(label, value, note, tone) {
  return '<div class="metric-card"><span class="eyebrow">' + label + '</span><strong class="metric-value ' + tone + '">' + safe(value) + '</strong><small>' + note + '</small></div>';
}

function renderCompose() {
  const graph = currentGraph();
  const scopeText = state.scope === "factory" ? "工厂" : safe(graph.title);
  const back = state.scope === "workarea" ? '<button class="back-button" data-action="back-factory">← 工厂</button>' : '';
  return '<section class="page compose-page"><div class="compose-head"><div><span class="eyebrow">ORCHESTRATION</span><div class="title-line">' + back + '<h1>' + scopeText + '</h1><span class="scope-badge">' + (state.scope === "factory" ? "FACTORY" : "WORKAREA") + '</span></div><p>' + (state.scope === "factory" ? "双击厂区进入，工厂层只保留厂区聚合。" : "厂区内只显示 Operator 与 Machine，细节由 Foreman 管理。") + '</p></div><div class="compose-actions"><span class="connection-mini ' + statusTone(state.connection) + '"><span class="status-dot"></span>' + safe(statusLabel(state.connection)) + '</span><button class="outline-button" data-action="fit">适应画布</button><button class="solid-button" data-action="foreman">' + (state.foremanOpen ? "关闭 Foreman" : "打开 Foreman") + '</button></div></div>' +
    '<div class="compose-body"><div class="canvas-panel"><div class="canvas-tools"><span class="canvas-hint">' + (state.scope === "factory" ? "双击厂区节点进入" : "拖动整理拓扑") + '</span><span class="zoom-readout">' + Math.round(state.zoom * 100) + '%</span><button data-action="zoom-out">−</button><button data-action="zoom-in">＋</button></div><div class="canvas-viewport" id="canvas-viewport"><div class="canvas-world" id="canvas-world"><svg class="edge-layer" id="edge-layer" aria-hidden="true"></svg><div class="node-layer" id="node-layer"></div></div><div class="canvas-empty" id="canvas-empty">没有可展示的拓扑</div></div><div class="canvas-legend"><span><i class="legend-dot good"></i>运行状态</span><span><i class="legend-line"></i>物料流转</span><span><i class="legend-line condition"></i>条件表达</span></div></div>' + (state.foremanOpen ? renderForeman() : '') + (state.detailOpen && state.selected ? renderNodeDetail() : '') + '</div></section>';
}

function renderForeman() {
  const foreman = state.bootstrap && state.bootstrap.foreman || {};
  const options = (state.bootstrap && state.bootstrap.harnesses || []).filter((item) => (item.roles || []).includes("foreman"));
  const current = state.foremanHarnessChoice || foreman.runtime || "";
  const harnessOptions = options.map((item) => '<option value="' + escapeHtml(item.id) + '" ' + (current === item.id ? "selected" : "") + '>' + safe(item.name) + (item.source === "local" ? " · 本地" : "") + '</option>').join("");
  return '<aside class="foreman-panel"><div class="drawer-head"><div><span class="eyebrow">FOREMAN</span><h2>厂长对话</h2></div><button class="icon-button" data-action="foreman">×</button></div><div class="foreman-harness"><span>Harness CLI</span><select data-foreman-harness><option value="">选择本地 Harness</option>' + harnessOptions + '</select></div><div class="autonomy-note"><span class="signal good"></span><span>可运行时，Foreman 会创建厂区、装配员工并继续处理。</span></div><div class="talk-stream">' + (state.talk.length ? state.talk.map(renderTalkMessage).join("") : '<div class="talk-empty">描述你希望工厂完成的目标。</div>') + (state.talkState ? '<div class="talk-error">' + safe(state.talkState) + '</div>' : '') + '</div><form class="talk-form" data-form="talk"><textarea name="text" placeholder="告诉 Foreman 你要完成什么…">' + safe(state.talkText, "") + '</textarea><button class="solid-button" type="submit">发送目标 <span>↗</span></button><small>发送后显示 queued，运行状态来自本地服务。</small></form></aside>';
}

function renderTalkMessage(item) {
  const role = item.role === "foreman" ? "foreman" : item.role === "system" ? "system" : "human";
  return '<div class="talk-message ' + role + '"><span class="talk-role">' + (role === "human" ? "你" : role === "foreman" ? "Foreman" : "系统") + '</span><p>' + safe(item.body) + '</p><small>' + safe(item.reply_state) + '</small></div>';
}

function renderNodeDetail() {
  const node = graphNode(state.selected);
  if (!node) return "";
  const meta = node.metadata || {};
  const options = (state.bootstrap && state.bootstrap.harnesses || []).filter((item) => (item.roles || []).includes("operator")).map((item) => '<option value="' + escapeHtml(item.id) + '" ' + (state.harnessChoice[node.id] === item.id || (!state.harnessChoice[node.id] && meta.harness && item.id.includes(meta.harness)) ? "selected" : "") + '>' + safe(item.name) + (item.source === "local" ? " · 本地" : "") + '</option>').join("");
  return '<aside class="detail-panel"><div class="drawer-head"><div><span class="eyebrow">NODE DETAIL</span><h2>' + safe(node.label) + '</h2></div><button class="icon-button" data-action="close-detail">×</button></div><div class="detail-status"><span class="status-dot ' + statusTone(node.status) + '"></span><strong>' + safe(node.type) + '</strong><span>' + statusLabel(node.status) + '</span></div><div class="detail-summary"><div><small>对应项目</small><strong>' + safe(meta.project || meta.machine || meta.station) + '</strong></div><div><small>输入</small><strong>' + safe((meta.inputs || []).join(" · ")) + '</strong></div><div><small>输出</small><strong>' + safe((meta.outputs || []).join(" · ")) + '</strong></div></div><details open><summary>配置与责任</summary><div class="detail-section">' + (node.type === "operator" ? '<div class="detail-line"><span>Harness CLI</span><select data-harness="' + escapeHtml(node.id) + '"><option value="">选择本地 Harness</option>' + options + '</select></div><div class="detail-line"><span>模型入口</span><strong>' + safe(meta.model) + '</strong></div><div class="detail-line"><span>资源请求</span><strong>' + (meta.resource_request ? "Operator 已提出请求" : "暂无请求") + '</strong></div>' : '<div class="detail-line"><span>开源项目</span><strong>' + safe(meta.project) + '</strong></div><div class="detail-line"><span>Git 地址</span><strong class="mono wrap">' + safe(meta.repo) + '</strong></div><div class="detail-line"><span>工作流文件</span><strong class="mono wrap">' + safe(meta.workflow) + '</strong></div><div class="detail-line"><span>交付语义</span><strong>' + safe(meta.delivery) + '</strong></div>') + '</div></details>' + (meta.resource_request ? '<button class="context-link" data-action="resource-request">查看 Operator 资源请求 <span>→</span></button>' : '') + '<p class="detail-note">节点位置只服务于阅读，不参与业务执行。配置变更由 Foreman 根据项目事实处理。</p></aside>';
}

function renderAssets() {
  const items = state.bootstrap && state.bootstrap.assets || [];
  return '<section class="page list-page"><div class="page-heading"><div><span class="eyebrow">ASSET SURFACE</span><h1>资产</h1><p>输入素材、输出资产和知识资产的来源摘要。</p></div><span class="count-badge">' + items.length + ' 项</span></div><div class="panel list-panel"><div class="list-head"><span>名称</span><span>类型</span><span>状态</span><span>来源</span></div>' + (items.length ? items.map((item) => '<button class="asset-row" data-asset="' + escapeHtml(item.id) + '"><span class="asset-name"><strong>' + safe(item.meta && (item.meta.name || item.meta.title) || item.id.slice(0, 18)) + '</strong><small>' + safe(item.id) + '</small></span><span>' + safe(item.type) + '</span><span class="status-badge ' + (item.sealed ? "good" : "warn") + '">' + (item.sealed ? "已封存" : "处理中") + '</span><span>' + safe(item.source && item.source.run, "无来源运行") + ' <b>›</b></span></button>').join("") : '<div class="empty-state">还没有资产记录。</div>') + '</div>' + (state.activeAsset ? renderAssetContext(state.activeAsset) : '') + '</section>';
}

function renderSecretContext(secret) {
  const request = secret.request;
  return '<aside class="secret-context"><button class="icon-button" data-action="close-secret">×</button><span class="eyebrow">SECRET CABINET</span><h2>密码柜</h2><p>只显示资源状态与绑定范围，密钥值由本地控制面保管。</p>' + (secret ? '<div class="secret-key"><strong>' + safe(secret.key) + '</strong><span class="status-badge ' + (secret.configured ? "good" : "bad") + '">' + (secret.configured ? "已配置" : "缺少") + '</span></div><div class="context-line"><span>Value</span><strong class="mono">' + safe(secret.masked_value) + '</strong></div><div class="context-line"><span>使用范围</span><strong>' + safe(secret.usage_scope) + '</strong></div><div class="context-line"><span>绑定对象</span><strong>' + safe((secret.bound_objects || []).join(" · "), "暂无绑定") + '</strong></div><div class="context-line"><span>最近使用</span><strong>' + safe(secret.last_used, "暂无记录") + '</strong></div><div class="context-line"><span>过期状态</span><strong>' + (secret.expired ? "已过期" : "未发现过期") + '</strong></div><details open><summary>Agent 生成的检查</summary><ul class="check-list">' + (secret.agent_generated_checks || []).map((item) => '<li>' + safe(item) + '</li>').join("") + '</ul></details>' + (request ? '<div class="request-note"><span class="signal warn"></span><span>Operator 已主动提出请求，状态为 ' + safe(request.status) + '。</span></div>' : '') : '') + '</aside>';
}

function renderAssetContext(asset) {
  return '<div class="context-drawer"><button class="icon-button" data-action="close-context">×</button><span class="eyebrow">ASSET CONTEXT</span><h2>' + safe(asset.id) + '</h2><div class="context-line"><span>Validation Epoch</span><strong>由运行记录提供</strong></div><div class="context-line"><span>来源运行</span><strong>' + safe(asset.source && asset.source.run) + '</strong></div><div class="context-line"><span>来源节点</span><strong>' + safe(asset.source && asset.source.node) + '</strong></div><p>完整 Lineage 在上下文展开时读取，列表保持摘要。</p></div>';
}

function renderLogs() {
  const items = state.bootstrap && state.bootstrap.logs || [];
  return '<section class="page list-page"><div class="page-heading"><div><span class="eyebrow">AUTONOMY LOG</span><h1>日志</h1><p>运行、验证、修复和异常统一进入时间线。</p></div><span class="count-badge">' + items.length + ' 条</span></div><div class="log-layout"><div class="panel list-panel log-list">' + (items.length ? renderLogRows(items.slice().reverse()) : '<div class="empty-state">还没有事件记录。</div>') + '</div>' + (state.activeLog ? renderLogContext(state.activeLog) : '<div class="panel log-placeholder"><span class="eyebrow">CONTEXT</span><h2>选择一条日志</h2><p>查看运行、ChangeSet、Epoch、验证和异常的上下文。</p></div>') + '</div></section>';
}

function renderLogRows(items) {
  return items.map((item) => '<button class="log-row" data-log="' + escapeHtml(item.id) + '"><time>' + safe((item.at || "").slice(11, 19)) + '</time><span class="signal ' + statusTone(item.kind) + '"></span><span><strong>' + safe(item.kind) + '</strong><small>' + safe(item.id) + '</small></span><b>›</b></button>').join("") || '<div class="empty-state compact">暂无动态</div>';
}

function renderLogContext(log) {
  return '<div class="panel log-context"><button class="icon-button" data-action="close-context">×</button><span class="eyebrow">EVENT CONTEXT</span><h2>' + safe(log.kind) + '</h2><p>' + safe(log.id) + '</p><div class="context-line"><span>时间</span><strong>' + safe(log.at) + '</strong></div><div class="context-line"><span>运行</span><strong>' + safe(log.run_id || log.job_id, "无") + '</strong></div><details><summary>事件摘要</summary><pre>' + safe(JSON.stringify(log.data || {}, null, 2)) + '</pre></details></div>';
}

function renderAssetRows(items) {
  return items.map((item) => '<button class="mini-row" data-asset="' + escapeHtml(item.id) + '"><span class="mini-icon">A</span><span><strong>' + safe(item.type) + '</strong><small>' + safe(item.id.slice(0, 20)) + '</small></span><span class="status-badge ' + (item.sealed ? "good" : "warn") + '">' + (item.sealed ? "封存" : "处理中") + '</span></button>').join("") || '<div class="empty-state compact">暂无资产</div>';
}

function bindShellEvents() {
  app.querySelectorAll("[data-page]").forEach((element) => element.addEventListener("click", () => navigate(element.dataset.page)));
  app.querySelectorAll("[data-workarea]").forEach((element) => element.addEventListener("click", () => { navigate("compose"); openWorkarea(element.dataset.workarea); }));
  app.querySelectorAll("[data-asset]").forEach((element) => element.addEventListener("click", () => {
    const id = element.dataset.asset;
    state.activeAsset = (state.bootstrap.assets || []).find((item) => item.id === id) || null;
    if (state.page === "dashboard") navigate("assets"); else render();
  }));
  app.querySelectorAll("[data-log]").forEach((element) => element.addEventListener("click", () => {
    state.activeLog = (state.bootstrap.logs || []).find((item) => item.id === element.dataset.log) || null;
    render();
  }));
  app.querySelectorAll("[data-action]").forEach((element) => element.addEventListener("click", handleAction));
  app.querySelectorAll("[data-form='talk']").forEach((form) => form.addEventListener("submit", sendTalk));
  app.querySelectorAll("[data-action='constitution']").forEach((element) => element.addEventListener("click", () => {
    state.talkState = "当前宪法：" + ((state.bootstrap && state.bootstrap.constitution || {}).version || "未配置");
    render();
  }));
  app.querySelectorAll("[data-foreman-harness]").forEach((element) => element.addEventListener("change", () => {
    state.foremanHarnessChoice = element.value;
    state.talkState = "Foreman Harness 选择已交给本地自治配置处理。";
    renderComposeOnly();
  }));
  app.querySelectorAll("[data-harness]").forEach((element) => element.addEventListener("change", () => {
    state.harnessChoice[element.dataset.harness] = element.value;
    state.talkState = "Harness 选择已交给 Foreman 处理。";
    renderComposeOnly();
  }));
  bindMao();
}

function handleAction(event) {
  const action = event.currentTarget.dataset.action;
  if (action === "close-sidebar") state.sidebarOpen = false;
  if (action === "mao") {
    if (state.ignoreMaoClick) { state.ignoreMaoClick = false; return; }
    state.sidebarOpen = !state.sidebarOpen;
  }
  if (action === "retry") boot();
  if (action === "foreman") state.foremanOpen = !state.foremanOpen;
  if (action === "back-factory") returnToFactory();
  if (action === "close-detail") { state.detailOpen = false; state.selected = null; }
  if (action === "close-context") { state.activeLog = null; state.activeAsset = null; }
  if (action === "fit") fitGraph();
  if (action === "zoom-in") setZoom(state.zoom + 0.1);
  if (action === "zoom-out") setZoom(state.zoom - 0.1);
  if (action === "resource-request") {
    const secrets = state.bootstrap && state.bootstrap.secrets || [];
    state.activeSecret = secrets.find((item) => item.request) || secrets.find((item) => item.status === "missing") || null;
    state.talkState = state.activeSecret ? "Operator 请求已进入密码柜上下文，Foreman 会继续处理。" : "Operator 已主动提出资源请求，Foreman 会在运行上下文中处理。";
  }
  if (action === "secrets") {
    const secrets = state.bootstrap && state.bootstrap.secrets || [];
    state.activeSecret = secrets[0] || null;
  }
  if (action === "close-secret") state.activeSecret = null;
  render();
}

function bindMao() {
  const mao = app.querySelector(".mao-float");
  if (!mao) return;
  let drag = null;
  mao.addEventListener("pointerdown", (event) => {
    mao.setPointerCapture(event.pointerId);
    drag = { startX: event.clientX, startY: event.clientY, startTop: state.mao.top, moved: false };
  });
  mao.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const dy = event.clientY - drag.startY;
    if (Math.abs(dy) > 4 || Math.abs(event.clientX - drag.startX) > 4) drag.moved = true;
    state.mao.top = Math.max(42, Math.min(window.innerHeight - 42, drag.startTop + dy));
    mao.style.top = state.mao.top + "px";
  });
  mao.addEventListener("pointerup", (event) => {
    if (!drag) return;
    const moved = drag.moved;
    if (moved) state.sidebarDock = event.clientX < window.innerWidth / 2 ? "left" : "right";
    state.sidebarOpen = moved ? false : !state.sidebarOpen;
    if (moved) {
      state.ignoreMaoClick = true;
      window.setTimeout(() => { state.ignoreMaoClick = false; }, 0);
    }
    drag = null;
    saveLayout();
    render();
  });
}

function bindCanvasEvents() {
  const viewport = document.getElementById("canvas-viewport");
  if (!viewport) return;
  drawCanvas();
  viewport.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".node")) return;
    viewport.setPointerCapture(event.pointerId);
    state.panning = { x: event.clientX, y: event.clientY, panX: state.pan.x, panY: state.pan.y };
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!state.panning) return;
    state.pan.x = state.panning.panX + event.clientX - state.panning.x;
    state.pan.y = state.panning.panY + event.clientY - state.panning.y;
    drawCanvas();
  });
  viewport.addEventListener("pointerup", () => { if (state.panning) { state.panning = null; persistGraphViewport(); } });
  viewport.addEventListener("pointercancel", () => { state.panning = null; });
  viewport.addEventListener("wheel", (event) => { event.preventDefault(); setZoom(state.zoom + (event.deltaY > 0 ? -0.08 : 0.08), event.clientX, event.clientY); }, { passive: false });
}

function drawCanvas() {
  const world = document.getElementById("canvas-world");
  const layer = document.getElementById("node-layer");
  const svg = document.getElementById("edge-layer");
  const empty = document.getElementById("canvas-empty");
  if (!world || !layer || !svg) return;
  const graph = currentGraph();
  const nodes = graphNodes(graph);
  world.style.transform = "translate(" + state.pan.x + "px," + state.pan.y + "px) scale(" + state.zoom + ")";
  empty.style.display = nodes.length ? "none" : "grid";
  layer.innerHTML = nodes.map((node) => '<button class="node ' + node.type + ' ' + statusTone(node.status) + (state.selected === node.id ? " selected" : "") + '" data-node="' + escapeHtml(node.id) + '" style="left:' + node.position.x + 'px;top:' + node.position.y + 'px"><span class="node-top"><i class="node-icon">' + (node.type === "workarea" ? "W" : node.type === "operator" ? "O" : "M") + '</i><span class="status-dot"></span></span><strong>' + safe(node.label) + '</strong><small>' + safe(node.type === "workarea" ? node.metadata && node.metadata.brief : node.metadata && (node.metadata.project || node.metadata.machine)) + '</small></button>').join("");
  const width = Math.max(900, ...nodes.map((node) => node.position.x + 260));
  const height = Math.max(620, ...nodes.map((node) => node.position.y + 150));
  svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.innerHTML = '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z"></path></marker></defs>' + (graph.edges || []).map((edge) => renderEdge(edge, nodes)).join("");
  layer.querySelectorAll("[data-node]").forEach((nodeElement) => {
    nodeElement.addEventListener("click", () => selectNode(nodeElement.dataset.node));
    nodeElement.addEventListener("dblclick", () => { if (state.scope === "factory") openWorkarea(nodeElement.dataset.node); });
    nodeElement.addEventListener("pointerdown", (event) => startNodeDrag(event, nodeElement.dataset.node));
  });
}

function syncCanvasGeometry() {
  const world = document.getElementById("canvas-world");
  const layer = document.getElementById("node-layer");
  const svg = document.getElementById("edge-layer");
  if (!world || !layer || !svg) return;
  const nodes = graphNodes();
  world.style.transform = "translate(" + state.pan.x + "px," + state.pan.y + "px) scale(" + state.zoom + ")";
  nodes.forEach((node) => {
    const element = layer.querySelector('[data-node="' + CSS.escape(node.id) + '"]');
    if (element) { element.style.left = node.position.x + "px"; element.style.top = node.position.y + "px"; }
  });
  const width = Math.max(900, ...nodes.map((node) => node.position.x + 260));
  const height = Math.max(620, ...nodes.map((node) => node.position.y + 150));
  svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.innerHTML = '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z"></path></marker></defs>' + (currentGraph().edges || []).map((edge) => renderEdge(edge, nodes)).join("");
}

function renderEdge(edge, nodes) {
  const source = nodes.find((node) => node.id === edge.source);
  const target = nodes.find((node) => node.id === edge.target);
  if (!source || !target) return "";
  const x1 = source.position.x + 184;
  const y1 = source.position.y + 46;
  const x2 = target.position.x;
  const y2 = target.position.y + 46;
  const curve = Math.max(42, Math.abs(x2 - x1) * 0.42);
  const path = "M" + x1 + " " + y1 + " C" + (x1 + curve) + " " + y1 + "," + (x2 - curve) + " " + y2 + "," + x2 + " " + y2;
  return '<path class="edge ' + (edge.kind === "condition" ? "condition" : "") + '" d="' + path + '" marker-end="url(#arrow)"></path><text class="edge-label" x="' + ((x1 + x2) / 2) + '" y="' + ((y1 + y2) / 2 - 8) + '">' + safe(edge.condition || "") + '</text>';
}

function startNodeDrag(event, id) {
  if (event.button !== 0) return;
  event.stopPropagation();
  const node = graphNode(id);
  if (!node) return;
  state.dragging = { id, x: event.clientX, y: event.clientY, origin: { ...node.position } };
  event.currentTarget.setPointerCapture(event.pointerId);
  const move = (moveEvent) => {
    if (!state.dragging) return;
    const layout = graphLayout();
    layout.nodes[id] = {
      x: state.dragging.origin.x + (moveEvent.clientX - state.dragging.x) / state.zoom,
      y: state.dragging.origin.y + (moveEvent.clientY - state.dragging.y) / state.zoom,
    };
    syncCanvasGeometry();
  };
  const end = () => {
    if (!state.dragging) return;
    state.dragging = null;
    persistGraphViewport();
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", end);
  };
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", end, { once: true });
}

function selectNode(id) {
  state.selected = id;
  state.detailOpen = true;
  renderComposeOnly();
}

function renderComposeOnly() {
  const main = app.querySelector(".app-main");
  if (!main) return;
  main.innerHTML = renderCompose();
  bindShellEvents();
  bindCanvasEvents();
}

function setZoom(value, clientX, clientY) {
  const next = Math.max(0.55, Math.min(1.65, value));
  if (clientX != null) {
    const viewport = document.getElementById("canvas-viewport");
    if (viewport) {
      const rect = viewport.getBoundingClientRect();
      const factor = next / state.zoom;
      state.pan.x = clientX - rect.left - (clientX - rect.left - state.pan.x) * factor;
      state.pan.y = clientY - rect.top - (clientY - rect.top - state.pan.y) * factor;
    }
  }
  state.zoom = next;
  persistGraphViewport();
  const readout = app.querySelector(".zoom-readout");
  if (readout) readout.textContent = Math.round(state.zoom * 100) + "%";
  drawCanvas();
}

function fitGraph() {
  const nodes = graphNodes();
  if (!nodes.length) return;
  const minX = Math.min(...nodes.map((node) => node.position.x));
  const minY = Math.min(...nodes.map((node) => node.position.y));
  const maxX = Math.max(...nodes.map((node) => node.position.x + 260));
  const maxY = Math.max(...nodes.map((node) => node.position.y + 150));
  const viewport = document.getElementById("canvas-viewport");
  if (!viewport) return;
  state.zoom = Math.max(0.55, Math.min(1.15, Math.min(viewport.clientWidth / (maxX - minX + 160), viewport.clientHeight / (maxY - minY + 160))));
  state.pan = { x: (viewport.clientWidth - (maxX - minX) * state.zoom) / 2 - minX * state.zoom, y: (viewport.clientHeight - (maxY - minY) * state.zoom) / 2 - minY * state.zoom };
  persistGraphViewport();
  drawCanvas();
}

function persistGraphViewport() {
  const layout = graphLayout();
  layout.pan = { ...state.pan };
  layout.zoom = state.zoom;
  saveLayout();
}

async function sendTalk(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const text = String(new FormData(form).get("text") || "").trim();
  if (!text) return;
  state.talkText = "";
  state.talkState = "发送中";
  renderComposeOnly();
  try {
    const result = await api("/api/web/talk", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
    state.talkState = result.state || "queued";
    state.talk = [ ...(state.talk || []), result.message ];
    renderComposeOnly();
    window.setTimeout(refreshTalk, 900);
  } catch (error) {
    state.talkState = error.payload && error.payload.code ? error.payload.code + " · " + error.message : error.message;
    renderComposeOnly();
  }
}

window.addEventListener("resize", () => { state.mao.top = Math.max(42, Math.min(window.innerHeight - 42, state.mao.top)); if (state.page !== "compose") render(); });
boot();
