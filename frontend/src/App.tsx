import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,

  type Edge,
  type Node,
  type NodeProps,

  type OnNodesChange,
  type ReactFlowInstance,
  type Viewport,
} from "@xyflow/react";
import { ApiError, cancelBusinessRun, cancelForemanTurn, configureHarness, createBusinessRun, dispatchBusinessRun, ensureRuntime, getAssetsPage, getBootstrap, getBusinessRuns, getGraph, getLogs, getTalk, markProjectResourceReady, putProjectSecret, retryBusinessRun, sendTalk, submitProjectIntake, subscribeToEvents, verifyHarness, type WebScope } from "./api";
import { OperatorBindingForm } from "./OperatorBindingForm";
import { WorkareaConversions } from "./WorkareaConversions";
import type { AssetPageData, AssetRecord, Bootstrap, BusinessRun, DependencyStatus, EventRecord, ForemanTurn, Graph, GraphNode, HarnessProfile, LayoutGraph, LayoutState, OperatorBindingView, Page, Scope, SecretRecord, StorageProviderStatus, TalkMessage } from "./types";

type FlowNodeData = { source: GraphNode };
type FlowNode = Node<FlowNodeData>;

const LAYOUT_KEY = "mao-m18-web-layout";
const NAV: Array<{ id: Page; label: string; hint: string; glyph: string }> = [
  { id: "dashboard", label: "看板", hint: "工厂概况", glyph: "◉" },
  { id: "compose", label: "编排", hint: "工厂与厂区", glyph: "⌘" },
  { id: "assets", label: "资产", hint: "输入与输出", glyph: "◇" },
  { id: "logs", label: "日志", hint: "运行与自治", glyph: "≡" },
  { id: "settings", label: "设置", hint: "项目依赖配置", glyph: "⚙" },
];
const STATUS_LABELS: Record<string, string> = { connected: "在线", connecting: "连接中", degraded: "降级", exception: "异常", running: "运行中", repairing: "自动修复", validating: "验证中", ready: "就绪", done: "已完成", failed: "失败", unknown: "待观测" };

function readLayout(): LayoutState {
  try { const parsed = JSON.parse(localStorage.getItem(LAYOUT_KEY) ?? "{}"); return parsed && typeof parsed === "object" ? parsed : {}; } catch { return {}; }
}
function statusLabel(status: unknown): string { return STATUS_LABELS[String(status ?? "")] ?? String(status || "未知"); }
function statusTone(status: unknown): "good" | "warn" | "bad" | "quiet" { const value = String(status ?? "").toLowerCase(); if (["connected", "running", "ready", "done", "sealed"].includes(value)) return "good"; if (["repairing", "validating", "degraded", "waiting", "pending"].includes(value)) return "warn"; if (["exception", "failed", "missing", "crashed"].includes(value)) return "bad"; return "quiet"; }
function display(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  if (Array.isArray(value)) return value.map((item) => display(item, "")).filter(Boolean).join(" · ") || fallback;
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([key, item]) => item !== null && item !== undefined && item !== "" && !/secret|token|password|api[_-]?key/i.test(key))
      .slice(0, 5)
      .map(([key, item]) => `${key} ${display(item, "")}`);
    return entries.join(" · ") || fallback;
  }
  return String(value);
}

function semanticEntries(value: Record<string, unknown> | undefined): Array<[string, string]> {
  if (!value) return [];
  return Object.entries(value)
    .filter(([key, item]) => item !== null && item !== undefined && item !== "" && !/secret|token|password|api[_-]?key/i.test(key))
    .slice(0, 12)
    .map(([key, item]) => [key.replaceAll("_", " "), display(item)]);
}
function graphKey(graph: Graph | null): string { return graph ? `${graph.scope}:${graph.id}` : "factory:factory"; }
function graphLayout(layout: LayoutState, graph: Graph): LayoutGraph { return layout.graphs?.[graphKey(graph)] ?? { nodes: {}, viewport: { x: 0, y: 0, zoom: 1 } }; }
function makeFlowNodes(graph: Graph, layout: LayoutState): FlowNode[] { const saved = graphLayout(layout, graph); return graph.nodes.map((source) => ({ id: source.id, type: source.type, position: saved.nodes[source.id] ?? source.position, data: { source }, draggable: true, selectable: true })); }

function edgeColor(kind: string): string { if (kind === "condition") return "#8a71e7"; if (kind === "default") return "#6e8795"; if (kind === "aggregate") return "#a7b6be"; return "#6390c8"; }
function makeFlowEdges(graph: Graph): Edge[] { return graph.edges.map((edge) => { const color = edgeColor(edge.kind); return { id: edge.id, source: edge.source, target: edge.target, type: "smoothstep", animated: edge.status === "running", label: edge.condition || undefined, labelStyle: { fill: "#6c7a84", fontSize: 10, fontWeight: 600 }, labelBgStyle: { fill: "#f8fafb", fillOpacity: 0.96, color: "#dce4e8" }, labelBgPadding: [6, 3], labelBgBorderRadius: 5, style: { stroke: color, strokeWidth: edge.kind === "condition" ? 1.8 : 1.45 }, markerEnd: { type: MarkerType.ArrowClosed, color } }; }); }

function GraphNodeCard({ data, selected }: NodeProps<FlowNode>): ReactElement {
  const node = data.source;
  const metadata = node.metadata ?? {};
  const kindLabel = node.type === "workarea" ? "厂区" : node.type === "operator" ? "Operator" : "Machine";
  const secondary = node.type === "operator" ? display(metadata.project, "职责由 Operator 管理") : node.type === "machine" ? display(metadata.project, "开源项目待识别") : display(metadata.brief, "由现有执行图聚合");
  return <div className={`graph-node graph-node-${node.type} ${selected ? "is-selected" : ""}`}>
    <Handle type="target" position={Position.Left} className="flow-handle" />
    <div className="node-cap"><span className={`node-icon ${node.type}`}>{node.type === "workarea" ? "W" : node.type === "operator" ? "O" : "M"}</span><span className="node-type">{kindLabel}</span><span className={`status-dot ${statusTone(node.status)}`} /></div>
    <strong className="node-title">{node.label}</strong><span className="node-secondary">{secondary}</span>
    <div className="node-meta">{node.type === "operator" && <span>{display(metadata.harness, "Harness 待选择")}</span>}{node.type === "machine" && <span>{display(metadata.repo, "项目来源待识别")}</span>}{node.type === "workarea" && <span>{display(metadata.active, "0")} 活跃节点</span>}</div>
    <Handle type="source" position={Position.Right} className="flow-handle" />
  </div>;
}
const nodeTypes = { workarea: GraphNodeCard, operator: GraphNodeCard, machine: GraphNodeCard };

function ConnectionState({ state, message }: { state: string; message?: string }): ReactElement { return <span className={`connection-pill ${statusTone(state)}`} title={message}><span className="status-dot" />{statusLabel(state)}</span>; }

function Sidebar({ open, dock, page, connection, onClose, onNavigate }: { open: boolean; dock: "left" | "right"; page: Page; connection: string; onClose: () => void; onNavigate: (page: Page) => void }): ReactElement {
  return <aside className={`sidebar sidebar-${dock} ${open ? "is-open" : ""}`} aria-label="主导航"><div className="sidebar-brand"><div className="brand-mark">M</div><div><strong>MAO</strong><small>machine agent org</small></div><button className="icon-button" onClick={onClose} aria-label="关闭侧边栏">×</button></div>
    <nav className="nav-list">{NAV.map((item) => <button className={`nav-item ${page === item.id ? "active" : ""}`} key={item.id} onClick={() => onNavigate(item.id)}><span className="nav-glyph">{item.glyph}</span><span><strong>{item.label}</strong><small>{item.hint}</small></span></button>)}</nav>
    <div className="sidebar-foot"><span className={`status-dot ${statusTone(connection)}`} /><span>{statusLabel(connection)}</span><span className="mono">LOCAL</span></div></aside>;
}

function MaoButton({ dock, top, onToggle, onDock }: { dock: "left" | "right"; top: number; onToggle: () => void; onDock: (dock: "left" | "right", top: number) => void }): ReactElement {
  const pointer = useRef<{ startX: number; startY: number; top: number; moved: boolean } | null>(null);
  const handlePointerDown = (event: React.PointerEvent<HTMLButtonElement>) => { pointer.current = { startX: event.clientX, startY: event.clientY, top, moved: false }; event.currentTarget.setPointerCapture(event.pointerId); };
  const handlePointerMove = (event: React.PointerEvent<HTMLButtonElement>) => { const current = pointer.current; if (!current) return; const distance = Math.abs(event.clientX - current.startX) + Math.abs(event.clientY - current.startY); current.moved ||= distance > 4; if (current.moved) onDock(event.clientX < window.innerWidth / 2 ? "left" : "right", Math.max(44, Math.min(window.innerHeight - 44, current.top + event.clientY - current.startY))); };
  const handlePointerUp = () => { const current = pointer.current; pointer.current = null; if (current && !current.moved) onToggle(); };
  return <button className={`mao-float mao-${dock}`} style={{ top }} aria-label="打开 MAO 侧边栏" title="拖动 MAO 调整停靠侧，点击打开侧边栏" onPointerDown={handlePointerDown} onPointerMove={handlePointerMove} onPointerUp={handlePointerUp} onPointerCancel={() => { pointer.current = null; }}><span>MAO</span><i /></button>;
}
function ErrorBanner({ message, onRetry }: { message: string; onRetry: () => void }): ReactElement { return <div className="error-banner"><span className="signal bad" /><strong>本地服务异常</strong><span>{message}</span><button className="text-button" onClick={onRetry}>重试</button></div>; }
function MetricCard({ label, value, note, tone }: { label: string; value: string | number; note: string; tone: string }): ReactElement { return <div className="metric-card"><span className="eyebrow">{label}</span><strong className={`metric-value ${tone}`}>{value}</strong><small>{note}</small></div>; }
function SummaryItem({ label, value }: { label: string; value: string }): ReactElement { return <div><span>{label}</span><strong>{value}</strong></div>; }
function EmptyState({ text }: { text: string }): ReactElement { return <div className="empty-state">{text}</div>; }
function Dashboard({ bootstrap, onCompose, onIntake, onSecrets, onConstitution, onWorkarea, onForemanConfig }: { bootstrap: Bootstrap; onCompose: () => void; onIntake: () => void; onSecrets: () => void; onConstitution: () => void; onWorkarea: (id: string) => void; onForemanConfig: () => void }): ReactElement {
  const status = bootstrap.status ?? {}; const runs = status.runs; const active = runs ? Number(runs.RUNNING ?? 0) + Number(runs.PENDING ?? 0) : undefined; const alerts = status.alerts; const exceptions = alerts ? alerts.length : undefined; const secrets = bootstrap.secrets ?? []; const configuredSecrets = secrets.filter((item) => item.configured).length; const factory = bootstrap.graph.factory;
  return <section className="page dashboard-page"><div className="page-heading"><div><span className="eyebrow">FACTORY OVERVIEW</span><h1>看板</h1><p>工厂由 Foreman 驱动，人管理边界和方向。</p></div><div className="compose-actions"><button className="secondary-button" onClick={onIntake}>导入项目</button><ConnectionState state={bootstrap.connection.state} message={bootstrap.connection.message} /></div></div>
    {!bootstrap.foreman.configured && <div className="setup-banner"><div><strong>先配置厂长 Harness</strong><span>Foreman 是本项目的自治入口，缺少配置时会主动提出后续依赖。</span></div><button className="primary-button" onClick={onForemanConfig}>进入编排配置</button></div>}
    <div className="metric-grid"><MetricCard label="当前运行" value={active === undefined ? "待观测" : active} note={active === undefined ? "后端未提供运行汇总" : active ? "正在由员工处理" : "当前无运行"} tone={active === undefined ? "blue" : "running"} /><MetricCard label="自治完成" value="待观测" note="等待运行事实汇总" tone="blue" /><MetricCard label="厂区" value={factory.nodes.length} note="从现有执行图聚合" tone="violet" /><MetricCard label="异常" value={exceptions === undefined ? "待观测" : exceptions} note={exceptions === undefined ? "后端未提供异常汇总" : exceptions ? "进入日志上下文" : "Foreman 当前可继续"} tone={exceptions === undefined ? "blue" : exceptions ? "bad" : "good"} /><button className="metric-card metric-action" onClick={onSecrets}><span className="eyebrow">项目资源</span><strong className="metric-value violet">{configuredSecrets}/{secrets.length}</strong><small>查看项目资源请求</small></button></div>
    <div className="dashboard-columns"><section className="panel overview-panel"><div className="panel-head"><div><span className="eyebrow">WORKAREAS</span><h2>厂区</h2></div><button className="text-button" onClick={onCompose}>打开编排 <span>→</span></button></div>{factory.nodes.length ? <div className="workarea-list">{factory.nodes.map((node) => <button className="workarea-row" key={node.id} onClick={() => onWorkarea(node.id)}><span className={`status-ring ${statusTone(node.status)}`} /><span className="row-main"><strong>{node.label}</strong><small>{display(node.metadata?.brief, "现有执行图聚合")}</small></span><span className={`row-state ${statusTone(node.status)}`}>{statusLabel(node.status)}</span><span className="chevron">›</span></button>)}</div> : <EmptyState text="当前 Registry 没有可展示的厂区。" />}</section>
      <section className="panel boundary-panel"><div className="panel-head"><div><span className="eyebrow">HUMAN BOUNDARY</span><h2>厂长边界</h2></div><span className="signal good" /></div><p>人决定工厂方向。厂长管理工位和业务流程，项目只申请运行所需的密钥。</p><div className="boundary-chain"><span>人</span><b>→</b><span>Foreman</span><b>→</b><span>Operator</span><b>→</b><span>Machine</span></div><button className="boundary-action" onClick={onConstitution}><span><strong>宪法</strong><small>{display(bootstrap.constitution.version, "未配置")} · {display(bootstrap.constitution.status, "边界状态未知")}</small></span><b>›</b></button></section></div>
    <section className="panel recent-panel"><div className="panel-head"><div><span className="eyebrow">RECENT ACTIVITY</span><h2>最近运行</h2></div><button className="text-button" onClick={onCompose}>进入编排</button></div><div className="recent-strip"><SummaryItem label="最近日志" value={`${bootstrap.logs.length} 条`} /><SummaryItem label="资产" value={`${bootstrap.assetTotal ?? bootstrap.assets.length} 项`} /><SummaryItem label="Foreman" value={bootstrap.foreman.configured ? "可用" : "待配置"} /><SummaryItem label="宪法" value={display(bootstrap.constitution.version, "未配置")} /></div></section></section>;
}

function CanvasView({ graph, layout, onLayout, onSelect, onOpenWorkarea }: { graph: Graph; layout: LayoutState; onLayout: (graph: Graph, nodes: FlowNode[], viewport: Viewport) => void; onSelect: (node: GraphNode | null) => void; onOpenWorkarea: (id: string) => void }): ReactElement {
  const saved = graphLayout(layout, graph); const [nodes, setNodes] = useState<FlowNode[]>(() => makeFlowNodes(graph, layout)); const [flow, setFlow] = useState<ReactFlowInstance<FlowNode, Edge> | null>(null);
  useEffect(() => { setNodes(makeFlowNodes(graph, layout)); }, [graph, layout]);
  const edges = useMemo(() => makeFlowEdges(graph), [graph]);
  const handleNodesChange: OnNodesChange<FlowNode> = useCallback((changes) => { setNodes((current) => changes.applyTo(current)); }, []);
  const handleNodeDragStop = useCallback(() => { if (flow) onLayout(graph, nodes, flow.getViewport()); }, [flow, graph, nodes, onLayout]);
  const handleMoveEnd = useCallback((_: unknown, viewport: Viewport) => { onLayout(graph, nodes, viewport); }, [graph, nodes, onLayout]);
  const handleNodeClick = useCallback((_: React.MouseEvent, node: FlowNode) => onSelect(node.data.source), [onSelect]);
  const handleNodeDoubleClick = useCallback((_: React.MouseEvent, node: FlowNode) => { if (graph.scope === "factory" && node.data.source.type === "workarea") onOpenWorkarea(node.id); }, [graph.scope, onOpenWorkarea]);
  return <div className="canvas-panel"><div className="canvas-tools"><span className="canvas-hint">{graph.scope === "factory" ? "双击厂区进入内部" : "Operator 管理 Machine，Foreman 处理细节"}</span><span className="zoom-readout">{Math.round((saved.viewport?.zoom ?? 1) * 100)}%</span><button onClick={() => flow?.zoomOut()} aria-label="缩小">−</button><button onClick={() => flow?.zoomIn()} aria-label="放大">＋</button><button onClick={() => flow?.fitView({ padding: 0.2 })} aria-label="适应画布">适应</button></div>
    <div className="flow-shell">{graph.nodes.length ? <ReactFlow<FlowNode, Edge> key={graph.id} nodes={nodes} edges={edges} nodeTypes={nodeTypes} onInit={setFlow} onNodesChange={handleNodesChange} onNodeDragStop={handleNodeDragStop} onMoveEnd={handleMoveEnd} onNodeClick={handleNodeClick} onNodeDoubleClick={handleNodeDoubleClick} defaultViewport={saved.viewport} minZoom={0.35} maxZoom={1.8} fitView={false} nodesConnectable={false} edgesReconnectable={false} panOnDrag selectionOnDrag={false} zoomOnScroll zoomOnPinch zoomOnDoubleClick={false} proOptions={{ hideAttribution: true }}><Background color="#d8e0e4" gap={28} size={1} /><Controls showInteractive={false} position="bottom-right" /></ReactFlow> : <EmptyState text="没有可展示的拓扑" />}</div><div className="canvas-legend"><span><i className="legend-dot good" />运行状态</span><span><i className="legend-line" />物料流转</span><span><i className="legend-line condition" />条件表达</span></div></div>;
}

type HarnessForm = {
  runtime: string;
  base_url: string;
  model: string;
  api_key: string;
  thinking_effort: string;
  context: string;
  image: string;
  network: string;
  session_timeout_s: string;
  session_budget_usd: string;
  daily_budget_usd: string;
  max_turns: string;
  max_attempts: string;
  backoff_s: string;
  mode: string;
  execution_mode: string;
};

function emptyHarnessForm(role: string, runtime: string, profile?: HarnessProfile | null, sharedProfile?: HarnessProfile | null): HarnessForm {
  const shared = role === "operator" ? sharedProfile : undefined;
  return {
    runtime: profile?.runtime ?? runtime,
    base_url: profile?.base_url ?? "",
    model: profile?.model ?? "",
    api_key: "",
    thinking_effort: profile?.thinking_effort ?? (role === "foreman" ? "max" : "medium"),
    context: String(profile?.context ?? (role === "foreman" ? 500000 : 256000)),
    image: profile?.image ?? "",
    network: profile?.network ?? (role === "foreman" ? "bridge" : "egress"),
    session_timeout_s: String(profile?.session_timeout_s ?? shared?.session_timeout_s ?? 600),
    session_budget_usd: String(profile?.session_budget_usd ?? shared?.session_budget_usd ?? 1),
    daily_budget_usd: String(profile?.daily_budget_usd ?? shared?.daily_budget_usd ?? 10),
    max_turns: String(profile?.max_turns ?? shared?.max_turns ?? 20),
    max_attempts: String(profile?.max_attempts ?? shared?.max_attempts ?? 2),
    backoff_s: String(profile?.backoff_s ?? shared?.backoff_s ?? 60),
    mode: profile?.mode ?? (role === "foreman" ? "session" : "session"),
    execution_mode: profile?.execution_mode ?? (role === "foreman" ? "trusted_container" : "native_host"),
  };
}

function HarnessConfigPanel({ open, role, bootstrap, initialRuntime, onClose, onSaved }: { open: boolean; role: string; bootstrap: Bootstrap; initialRuntime?: string; onClose: () => void; onSaved: (profile: HarnessProfile, foreman: Bootstrap["foreman"]) => void }): ReactElement | null {
  const profiles = (bootstrap.harness_profiles ?? []).filter((item) => item.role === role);
  const runtimes = bootstrap.harnesses.filter((item) => item.kind === "agent-cli" || item.roles?.includes(role));
  const [selectedProfile, setSelectedProfile] = useState("");
  const sharedProfile = role === "operator" ? bootstrap.foreman.profile : undefined;
  const [form, setForm] = useState<HarnessForm>(() => emptyHarnessForm(role, initialRuntime ?? runtimes[0]?.id ?? "", undefined, sharedProfile));
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);
  const [message, setMessage] = useState("");
  const [savedProfile, setSavedProfile] = useState<HarnessProfile | null>(null);
  const [apiKeySource, setApiKeySource] = useState("");
  const persistedProfile = profiles.find((item) => item.id === savedProfile?.id);
  const verification = (persistedProfile?.verification?.checked_at ?? "") >= (savedProfile?.verification?.checked_at ?? "")
    ? persistedProfile?.verification ?? savedProfile?.verification : savedProfile?.verification;

  useEffect(() => {
    if (!open) return;
    const current = profiles.find((item) => item.runtime === initialRuntime) ?? profiles[0];
    setSelectedProfile(current?.id ?? "");
    setSavedProfile(current ?? null);
    setForm(emptyHarnessForm(role, initialRuntime ?? runtimes[0]?.id ?? "", current, sharedProfile));
    setApiKeySource(current?.api_key_configured ? current.id : "");
    setMessage("");
  }, [open, role, initialRuntime, sharedProfile]);

  if (!open) return null;
  const update = (key: keyof HarnessForm, value: string) => setForm((current) => ({ ...current, [key]: value }));
  const loadProfile = (id: string) => {
    const profile = profiles.find((item) => item.id === id);
    setSelectedProfile(id);
    setSavedProfile(profile ?? null);
    setApiKeySource(profile?.api_key_configured ? profile.id : "");
    if (profile) setForm(emptyHarnessForm(role, profile.runtime, profile, sharedProfile));
  };
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setMessage("");
    try {
      const { api_key: _apiKey, ...safeForm } = form;
      const result = await configureHarness({ ...safeForm, role, api_key_profile_id: apiKeySource || undefined, context: Number(form.context), session_timeout_s: Number(form.session_timeout_s), session_budget_usd: Number(form.session_budget_usd), daily_budget_usd: Number(form.daily_budget_usd), max_turns: Number(form.max_turns), max_attempts: Number(form.max_attempts), backoff_s: Number(form.backoff_s) });
      setSavedProfile(result.profile);
      setSelectedProfile(result.profile.id);
      setMessage("配置已保存。可以验证连通性 harness 和模型连通性。");
      onSaved(result.profile, result.foreman);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "配置保存失败");
    } finally {
      setSaving(false);
    }
  };
  const verify = async () => {
    if (!savedProfile) {
      setMessage("请先保存配置");
      return;
    }
    setChecking(true);
    try {
      const result = await verifyHarness(savedProfile.id);
      setSavedProfile(result.profile);
      setMessage(result.profile.verification?.reason ?? "验证请求已提交");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "验证请求失败");
    } finally {
      setChecking(false);
    }
  };
  const baseUrls = Array.from(new Set(profiles.map((item) => item.base_url).filter(Boolean)));
  const models = Array.from(new Set(profiles.map((item) => item.model).filter(Boolean)));
  return <aside className="detail-panel harness-config-panel"><button className="icon-button" onClick={onClose} aria-label="关闭 Harness 配置">×</button><span className="eyebrow">HARNESS CONFIGURATION</span><h2>{role === "foreman" ? "配置厂长 Harness" : "配置 Operator Harness"}</h2><p className="config-intro">先选择 Harness、Base URL、API key 来源和模型。Thinking effort 与 Context 按当前 Harness profile 独立设置，其他运行参数按共享策略管理。</p>{profiles.length > 0 && <label className="config-field"><span>已保存配置</span><select value={selectedProfile} onChange={(event) => loadProfile(event.target.value)}><option value="">新建配置</option>{profiles.map((item) => <option key={item.id} value={item.id}>{item.runtime} · {item.model}</option>)}</select></label>}<form onSubmit={save}><label className="config-field"><span>Harness Runtime</span><select value={form.runtime} onChange={(event) => update("runtime", event.target.value)} required><option value="">选择 agent-cli runtime</option>{runtimes.map((item) => <option key={item.id} value={item.id}>{item.name}{item.version ? ` · ${item.version}` : ""}</option>)}</select></label><label className="config-field"><span>Base URL</span><input list="harness-base-urls" value={form.base_url} onChange={(event) => update("base_url", event.target.value)} placeholder="留空使用官方 Harness 默认地址" /><datalist id="harness-base-urls">{baseUrls.map((item) => <option key={item} value={item} />)}</datalist></label><label className="config-field"><span>API key</span><select value={apiKeySource} onChange={(event) => setApiKeySource(event.target.value)}><option value="">由项目资源请求提供</option>{profiles.filter((item) => item.api_key_configured).map((item) => <option key={item.id} value={item.id}>已配置 · {item.runtime} · {item.model}</option>)}</select><small className="field-hint">密钥值保存在项目资源保险箱，不会进入 Harness 配置表单。</small></label><label className="config-field"><span>Model</span><input list="harness-models" value={form.model} onChange={(event) => update("model", event.target.value)} placeholder="例如 claude-sonnet-5-5" required /><datalist id="harness-models">{models.map((item) => <option key={item} value={item} />)}</datalist></label><div className="context-note">项目缺少密钥时，Foreman 会创建项目资源请求并在此处显示状态。</div><details className="config-advanced"><summary>高级运行参数 <span>当前 profile 可独立调整 effort 与 context</span></summary>{role === "foreman" && <label className="config-field"><span>Foreman 执行环境</span><select value={form.execution_mode} onChange={(event) => update("execution_mode", event.target.value)}><option value="trusted_container">可信容器（root、bridge、可安装项目依赖）</option><option value="native_host">本机官方 Harness（需通过能力探针）</option></select></label>}<div className="config-grid"><label className="config-field"><span>Thinking effort</span><select value={form.thinking_effort} onChange={(event) => update("thinking_effort", event.target.value)}>{["minimal", "low", "medium", "high", "max"].map((item) => <option key={item}>{item}</option>)}</select></label><label className="config-field"><span>Context</span><input type="number" min="1" value={form.context} onChange={(event) => update("context", event.target.value)} /></label></div><label className="config-field"><span>Harness image</span><input value={form.image} onChange={(event) => update("image", event.target.value)} placeholder="可信容器需要 digest 固定镜像" /></label><div className="config-grid"><label className="config-field"><span>Network</span><select value={form.network} onChange={(event) => update("network", event.target.value)}>{role === "foreman" && <option value="bridge">bridge（可信厂长，可下载项目依赖）</option>}<option value="egress">egress</option><option value="none">none</option></select></label><label className="config-field"><span>Mode</span><select value={form.mode} onChange={(event) => update("mode", event.target.value)}><option value="session">session</option><option value="resident">resident</option></select></label></div><div className="config-grid"><label className="config-field"><span>Max turns</span><input type="number" min="1" value={form.max_turns} onChange={(event) => update("max_turns", event.target.value)} /></label><label className="config-field"><span>Timeout seconds</span><input type="number" min="1" value={form.session_timeout_s} onChange={(event) => update("session_timeout_s", event.target.value)} /></label></div><div className="config-grid"><label className="config-field"><span>Session budget</span><input type="number" min="0" step="0.01" value={form.session_budget_usd} onChange={(event) => update("session_budget_usd", event.target.value)} /></label><label className="config-field"><span>Daily budget</span><input type="number" min="0" step="0.01" value={form.daily_budget_usd} onChange={(event) => update("daily_budget_usd", event.target.value)} /></label></div><div className="config-grid"><label className="config-field"><span>Max attempts</span><input type="number" min="1" value={form.max_attempts} onChange={(event) => update("max_attempts", event.target.value)} /></label><label className="config-field"><span>Backoff seconds</span><input type="number" min="0" value={form.backoff_s} onChange={(event) => update("backoff_s", event.target.value)} /></label></div></details><div className="config-actions"><button className="primary-button" type="submit" disabled={saving}>{saving ? "保存中…" : "保存配置"}</button><button className="secondary-button" type="button" onClick={() => void verify()} disabled={checking || !savedProfile}>{checking ? "请求中…" : "验证连通性"}</button></div></form>{message && <div className="context-note">{message}</div>}{verification && <div className="context-line"><span>验证状态</span><strong>{display(verification.reason ?? verification.state, "待验证")}</strong></div>}</aside>;
}

const foremanTurnStatus = {
  queued: { rank: 0, label: "排队中" },
  claimed: { rank: 1, label: "处理中" },
  done: { rank: 2, label: "已完成" },
  failed: { rank: 2, label: "会话失败" },
  cancelled: { rank: 2, label: "已取消" },
  blocked: { rank: 2, label: "环境阻塞" },
  waiting_foreman_runtime: { rank: 0, label: "等待 Foreman 运行环境" },
} satisfies Record<ForemanTurn["status"], { rank: number; label: string }>;

const businessOutcomeLabels: Record<string, string> = {
  not_started: "业务尚未执行",
  in_progress: "业务执行中",
  applied: "业务已落库",
  no_change: "对话完成，未产生业务变更",
  blocked_environment: "环境阻塞，业务未执行",
  blocked_input: "等待业务输入",
  failed: "业务执行失败",
};

function businessOutcomeLabel(status?: string): string {
  return status ? businessOutcomeLabels[status] ?? status : "";
}

function readForemanTurn(input: unknown): ForemanTurn | undefined {
  if (!input || typeof input !== "object") return;
  const value = input as Record<string, unknown>;
  const { id, messageId, status, cancellable } = value;
  if (typeof id !== "string" || typeof messageId !== "string" || typeof status !== "string" || !Object.hasOwn(foremanTurnStatus, status)) return;
  const timestamp = (camel: string, snake: string) => {
    const candidate = value[camel] ?? value[snake];
    return typeof candidate === "string" ? candidate : undefined;
  };
  const elapsed = value.elapsedMs ?? value.elapsed_ms;
  return {
    id,
    messageId,
    status: status as ForemanTurn["status"],
    cancellable: cancellable === true || (cancellable === undefined && status === "queued"),
    queuedAt: timestamp("queuedAt", "queued_at"),
    startedAt: timestamp("startedAt", "started_at"),
    lastEventAt: timestamp("lastEventAt", "last_event_at"),
    completedAt: timestamp("completedAt", "completed_at"),
    elapsedMs: typeof elapsed === "number" && Number.isFinite(elapsed) ? elapsed : undefined,
    error: typeof value.error === "string" ? value.error : undefined,
    businessStatus: typeof value.businessStatus === "string" ? value.businessStatus : typeof value.business_status === "string" ? value.business_status : undefined,
    businessReason: typeof value.businessReason === "string" ? value.businessReason : typeof value.business_reason === "string" ? value.business_reason : undefined,
  };
}

function talkInboxId(message: TalkMessage, turns: Record<string, ForemanTurn>): string | undefined {
  const metadata = message.metadata;
  if (metadata && typeof metadata.inbox_id === "string") return metadata.inbox_id;
  if ((message.role === "user" || message.role === "human") && message.id) return turns[message.id]?.id;
  return undefined;
}

function orderTalkMessages(messages: TalkMessage[], turns: Record<string, ForemanTurn>): TalkMessage[] {
  const groups = new Map<string, { first: number; messages: TalkMessage[] }>();
  const unbound: Array<{ index: number; message: TalkMessage }> = [];
  messages.forEach((message, index) => {
    const inboxId = talkInboxId(message, turns);
    if (!inboxId) {
      unbound.push({ index, message });
      return;
    }
    const group = groups.get(inboxId);
    if (group) group.messages.push(message);
    else groups.set(inboxId, { first: index, messages: [message] });
  });
  const ordered = [...groups.values()].sort((left, right) => {
    const leftHuman = left.messages.find((message) => message.role === "user" || message.role === "human");
    const rightHuman = right.messages.find((message) => message.role === "user" || message.role === "human");
    const leftTime = Date.parse(leftHuman?.createdAt ?? "");
    const rightTime = Date.parse(rightHuman?.createdAt ?? "");
    if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) return leftTime - rightTime;
    return left.first - right.first;
  });
  return ordered.flatMap(({ messages: group }) => [
    ...group.filter((message) => message.role === "user" || message.role === "human"),
    ...group.filter((message) => message.role !== "user" && message.role !== "human"),
  ]).concat(unbound.sort((left, right) => left.index - right.index).map(({ message }) => message));
}

function ForemanPanel({ open, bootstrap, talk, turns, activity, onConfigureForeman, onOpenSecret, onClose, onSend, onCancel }: {
  open: boolean;
  bootstrap: Bootstrap;
  talk: TalkMessage[];
  turns: Record<string, ForemanTurn>;
  activity: string;
  onConfigureForeman: () => void;
  onOpenSecret: (request: SecretRecord) => void;
  onClose: () => void;
  onSend: (text: string) => Promise<void>;
  onCancel: (id: string) => Promise<void>;
}): ReactElement | null {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [cancelling, setCancelling] = useState<string>();
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    if (!open) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [open]);
  if (!open) return null;
  const foreman = bootstrap.foreman;
  const profile = foreman.profile;
  const runtimeState = foreman.runtime_state ?? profile?.runtime_state;
  const runtimeReason = foreman.runtime_reason ?? profile?.runtime_reason;
  const chatReady = Boolean(foreman.configured && profile?.api_key_configured && runtimeState !== "waiting_configuration" && runtimeState !== "blocked_environment" && runtimeState !== "native_unverified");
  const chatStatus = runtimeState === "waiting_configuration" ? "等待可信运行环境"
    : runtimeState === "blocked_environment" ? "环境阻塞"
      : runtimeState === "native_unverified" ? "等待能力验证"
        : profile && !profile.api_key_configured ? "等待项目资源"
          : chatReady ? "可对话" : "待配置";
  const pending = bootstrap.secrets.filter((item) => item.request?.id && !item.configured);
  const firstRequest = pending[0];
  const cancel = async (id: string) => {
    setCancelling(id);
    try { await onCancel(id); }
    finally { setCancelling(undefined); }
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!text.trim() || sending) return;
    setSending(true);
    try {
      await onSend(text.trim());
      setText("");
    } finally {
      setSending(false);
    }
  };
  const elapsed = (at?: string) => {
    if (!at) return "";
    const started = Date.parse(at);
    if (!Number.isFinite(started)) return "";
    const seconds = Math.max(0, Math.floor((clock - started) / 1_000));
    const minutes = Math.floor(seconds / 60);
    return `${String(minutes).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  };
  const turnElapsed = (turn: ForemanTurn | undefined, message: TalkMessage) => {
    if (!turn) return "";
    if (typeof turn.elapsedMs === "number") return elapsed(new Date(Date.now() - turn.elapsedMs).toISOString());
    const start = turn.startedAt ?? turn.queuedAt ?? message.at ?? message.createdAt;
    if (turn.status === "queued" || turn.status === "claimed") return elapsed(start);
    if (turn.completedAt && start) {
      const duration = Date.parse(turn.completedAt) - Date.parse(start);
      if (Number.isFinite(duration) && duration >= 0) return elapsed(new Date(Date.now() - duration).toISOString());
    }
    return elapsed(start);
  };
  const orderedTalk = useMemo(() => orderTalkMessages(talk, turns), [talk, turns]);
  return <aside className="foreman-panel">
    <div className="panel-head">
      <div><span className="eyebrow">FOREMAN CONSOLE</span><h2>厂长</h2></div>
      <button className="icon-button" onClick={onClose} aria-label="关闭厂长对话">×</button>
    </div>
    <div className="foreman-harness">
      <div>
        <span>Harness CLI</span>
        <strong>{foreman.configured ? `${display(foreman.runtime)} · ${display(foreman.model)}` : "未配置"}</strong>
        {profile && <small>{runtimeState === "waiting_configuration" ? "需要 digest 固定的可信 Harness 镜像" : profile.image ? (profile.api_key_configured ? "可信容器凭证已配置" : "可信容器凭证未配置") : (profile.api_key_configured ? "本机官方 Harness 待能力验证" : "本机官方 Harness 未发现")}</small>}
      </div>
      <div className="foreman-harness-actions"><span className={`connection-pill ${chatReady ? "good" : "warn"}`}><span className="status-dot" />{chatStatus}</span><button className="text-button" onClick={onConfigureForeman}>{foreman.configured ? "编辑配置" : "配置厂长"}</button></div>
    </div>
    {runtimeState && runtimeState !== "ready" && <div className="context-note foreman-runtime-state" role="status"><strong>{runtimeState === "blocked_environment" ? "Foreman 环境阻塞" : runtimeState === "waiting_configuration" ? "等待配置可信 Foreman" : `Foreman 环境 ${runtimeState}`}</strong>{runtimeReason && <small>{runtimeReason}</small>}</div>}
    {foreman.setup_required && <div className="context-note">项目已启动，但厂长 Harness 尚未配置。先完成配置，Foreman 才能接管缺失依赖和运行验证。</div>}
    {firstRequest && <div className="foreman-resource">
      <div aria-live="polite">
        <strong>项目资源待配置</strong>
        <small>{firstRequest.request?.project_name ?? firstRequest.request?.project_id} · {firstRequest.key}</small>
      </div>
      <button className="text-button" onClick={() => onOpenSecret(firstRequest)}>查看 {pending.length} 项</button>
    </div>}
    {activity && <div className="context-note" role="status">{activity}</div>}
    <div className="talk-list">
      {orderedTalk.length ? orderedTalk.map((item, index) => {
        const human = item.role === "human" || item.role === "user";
        const historicalOrphan = item.metadata?.historical_orphan === true || item.metadata?.historicalOrphan === true;
        const inboxId = talkInboxId(item, turns);
        const turn = human && item.id ? turns[item.id] : inboxId ? Object.values(turns).find((candidate) => candidate.id === inboxId) : undefined;
        return <div className={`talk-message ${human ? "human" : "agent"}${historicalOrphan ? " historical" : ""}`} key={item.id ?? `${item.at}-${index}`}>
          <span className="talk-role">{human ? "你" : "Foreman"}</span>
          <p>{display(item.body)}</p>
          <small>{historicalOrphan ? "历史孤立记录 · 不参与当前业务" : turn ? `${foremanTurnStatus[turn.status].label} · 已用时 ${turnElapsed(turn, item)}${turn.businessStatus ? ` · ${businessOutcomeLabel(turn.businessStatus)}` : ""}${turn.businessReason ? ` · ${turn.businessReason}` : ""}${turn.status === "failed" && turn.error ? ` · ${turn.error}` : ""}` : display(item.state, "已接收")}</small>
          {turn?.cancellable && foremanTurnStatus[turn.status].rank < 2 && <button type="button" className="text-button"
            disabled={Boolean(cancelling)} onClick={() => void cancel(turn.id)}>
            {cancelling === turn.id ? "取消中…" : "取消本轮"}
          </button>}
        </div>;
      }) : <div className="talk-empty"><span className="signal good" /><p>描述目标，Foreman 会创建厂区、管理员工并继续运行。</p></div>}
    </div>
    <form className="talk-form" onSubmit={submit}>
      <textarea value={text} onChange={(event) => setText(event.target.value)} placeholder="告诉 Foreman 你想让工厂完成什么…" rows={3} />
      <button className="primary-button" type="submit" disabled={!text.trim() || sending}>{sending ? "发送中…" : "发送给 Foreman"}</button>
    </form>
  </aside>;
}

const businessRunLabels: Record<string, string> = {
  queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消",
};

function BusinessRunsPanel({ businessSystemId, revision }: { businessSystemId: string; revision: number }): ReactElement {
  const [runs, setRuns] = useState<BusinessRun[]>([]);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");

  const refresh = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const next = await getBusinessRuns(businessSystemId);
      if (!signal?.aborted) { setRuns(next); setMessage(""); }
    } catch (error) {
      if (!signal?.aborted) setMessage(error instanceof Error ? error.message : "运行状态读取失败");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [businessSystemId]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    const timer = window.setInterval(() => { void refresh(controller.signal); }, 5_000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [refresh, revision]);

  const runAction = async (action: "dispatch" | "cancel" | "retry", run: BusinessRun) => {
    setMessage("");
    try {
      if (action === "dispatch") await dispatchBusinessRun(run.id);
      else if (action === "cancel") await cancelBusinessRun(run.id);
      else await retryBusinessRun(run.id);
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "运行操作失败");
    }
  };

  const create = async () => {
    setMessage("");
    try {
      await createBusinessRun(businessSystemId);
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法创建运行");
    }
  };

  return <section className="panel business-runs-panel">
    <div className="panel-head"><div><span className="eyebrow">BUSINESS RUNS</span><h2>业务运行</h2></div><button type="button" className="secondary-button" onClick={() => void create()} disabled={loading}>开始运行</button></div>
    {message && <div className="context-note" role="status">{message}</div>}
    {runs.length === 0 && !loading ? <EmptyState text="当前厂区还没有运行记录。" /> : <div className="business-run-list">{runs.slice(-8).reverse().map((run) => {
      const active = run.status === "queued" || run.status === "running";
      const label = businessRunLabels[run.status] ?? "待观测";
      return <div className="business-run-row" key={run.id}>
        <div><strong>{label}</strong><small>{run.id}</small></div>
        <span className={`status-badge ${run.status === "completed" ? "good" : run.status === "failed" ? "bad" : "warn"}`}>{run.units.length} 个单元</span>
        <div className="business-run-actions">
          {run.status === "queued" && <button type="button" className="text-button" onClick={() => void runAction("dispatch", run)}>派发</button>}
          {active && <button type="button" className="text-button" onClick={() => void runAction("cancel", run)}>取消</button>}
          {run.status === "failed" && <button type="button" className="text-button" onClick={() => void runAction("retry", run)}>重试</button>}
        </div>
      </div>;
    })}</div>}
  </section>;
}

function NodeDetail({ node, bootstrap, onConfigureOperator, onBindingSaved, onClose }: { node: GraphNode; bootstrap: Bootstrap; onConfigureOperator: (runtime?: string) => void; onBindingSaved: (unitId: string, binding: OperatorBindingView) => void; onClose: () => void }): ReactElement {
  const metadata = node.metadata ?? {};
  const operator = node.type === "operator";
  const machine = node.type === "machine";
  const harness = display(metadata.harness, "");
  const profile = (bootstrap.harness_profiles ?? []).find((item) => item.role === "operator" && item.runtime === harness);
  const internalSteps = Array.isArray(metadata.internal_steps)
    ? metadata.internal_steps.filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null)
    : [];
  return <aside className="detail-panel"><button className="icon-button" onClick={onClose} aria-label="关闭节点详情">×</button><span className="eyebrow">{operator ? "OPERATOR" : machine ? "MACHINE" : "WORKAREA"}</span><h2>{node.label}</h2><div className="detail-status"><span className={`status-dot ${statusTone(node.status)}`} /><strong>{statusLabel(node.status)}</strong><small>{display(metadata.recent_status, "最近状态待观测")}</small></div><div className="context-line"><span>输入</span><strong>{display(metadata.inputs, "由 Operator 管理")}</strong></div><div className="context-line"><span>输出</span><strong>{display(metadata.outputs, "由项目配置决定")}</strong></div>{(operator || machine) && <><div className="context-line"><span>项目摘要</span><strong>{display(metadata.project_summary ?? metadata.project, "由项目配置决定")}</strong></div><div className="context-line"><span>内部步骤</span><strong>{display(metadata.step_count, "0")}</strong></div></>}{operator && <>{node.operatorBinding ? <OperatorBindingForm key={node.id} unitId={node.id} binding={node.operatorBinding} harnesses={bootstrap.harnesses} profiles={bootstrap.harness_profiles ?? []} onConfigure={onConfigureOperator} onSaved={onBindingSaved} /> : <div className="detail-section"><span className="eyebrow">HARNESS CLI</span><div className="config-summary"><strong>{profile ? `${profile.runtime} · ${profile.model}` : "未配置"}</strong><small>{profile?.api_key_configured ? "API key 已配置" : "API key 待配置"} · {display(profile?.verification?.state, "未验证")}</small></div><button className="secondary-button" onClick={() => onConfigureOperator(harness || undefined)}>打开完整配置</button></div>}<div className="context-line"><span>职责</span><strong>{display(metadata.project, "由 Operator 与 Foreman 管理")}</strong></div><div className="context-line"><span>交付判断</span><strong>{display(metadata.delivery, "由 Foreman、Operator 和项目配置判断")}</strong></div></>}{machine && <><div className="context-line"><span>真实 Machine</span><strong>{display(metadata.machine_count, "0")}</strong></div><div className="context-line"><span>项目工具</span><strong>{display(metadata.project, "待识别")}</strong></div><div className="context-line"><span>Git 地址</span><strong className="mono breakable">{display(metadata.repos ?? metadata.repo, "未提供")}</strong></div><div className="context-line"><span>工作流文件</span><strong>{display(metadata.workflows ?? metadata.workflow, "由项目配置决定")}</strong></div><div className="context-note">{display(metadata.delivery, "Machine 是否承担交付由 Foreman、Operator 和项目配置判断")}</div></>}{node.type === "workarea" && <><div className="context-line"><span>活动节点</span><strong>{display(metadata.active, "0")}</strong></div><div className="context-line"><span>最近输出</span><strong>{display(metadata.latest, "暂无")}</strong></div></>}{internalSteps.length > 0 && <details className="detail-collapse" open><summary>内部步骤上下文</summary><ul className="check-list">{internalSteps.map((step, index) => <li key={`${display(step.name, "step")}-${index}`}><strong>{display(step.name, "未命名步骤")}</strong><small>{display(step.station, "未声明工位")} · {display(step.status, "待观测")}</small></li>)}</ul></details>}<details className="detail-collapse"><summary>节点标识与运行上下文</summary><div className="semantic-list"><div className="context-line"><span>节点标识</span><strong className="mono breakable">{node.id}</strong></div><div className="context-line"><span>所属图层</span><strong className="mono breakable">{node.group}</strong></div>{semanticEntries(metadata).map(([key, value]) => <div className="context-line" key={key}><span>{key}</span><strong>{value}</strong></div>)}</div></details></aside>;
}

function ComposePage({ bootstrap, graph, scope, layout, foremanOpen, selected, talk, foremanTurns, foremanActivity, runRevision, onCancelTalk, harnessConfigRole, harnessConfigRuntime, onToggleForeman, onBack, onLayout, onSelect, onOpenWorkarea, onConfigureForeman, onConfigureOperator, onBindingSaved, onCloseHarnessConfig, onHarnessProfileSaved, onSendTalk, onOpenSecret, onAddProject, materialConversions }: { bootstrap: Bootstrap; graph: Graph; scope: Scope; layout: LayoutState; foremanOpen: boolean; selected: GraphNode | null; talk: TalkMessage[]; foremanTurns: Record<string, ForemanTurn>; foremanActivity: Record<string, string>; runRevision: number; onCancelTalk: (id: string) => Promise<void>; harnessConfigRole: "operator" | "foreman" | null; harnessConfigRuntime: string; onToggleForeman: () => void; onBack: () => void; onLayout: (graph: Graph, nodes: FlowNode[], viewport: Viewport) => void; onSelect: (node: GraphNode | null) => void; onOpenWorkarea: (id: string) => void; onConfigureForeman: () => void; onConfigureOperator: (runtime?: string) => void; onBindingSaved: (unitId: string, binding: OperatorBindingView) => void; onCloseHarnessConfig: () => void; onHarnessProfileSaved: (profile: HarnessProfile, foreman: Bootstrap["foreman"]) => void; onSendTalk: (text: string) => Promise<void>; onOpenSecret: (request: SecretRecord) => void; onAddProject: () => void; materialConversions: ReactNode }): ReactElement {
  return <section className="compose-page"><div className="compose-head"><div><span className="eyebrow">{scope === "factory" ? "FACTORY TOPOLOGY" : "WORKAREA TOPOLOGY"}</span><h1>{scope === "factory" ? "工厂" : graph.title}</h1><p>{scope === "factory" ? "厂区是现有执行图的聚合节点。双击进入内部。" : "每个项目有自己的 Operator 和 Machine。"}</p></div><div className="compose-actions">{scope === "workarea" && <><button className="secondary-button" onClick={onBack}>← 返回工厂</button><button className="secondary-button" onClick={onAddProject}>添加项目</button>{materialConversions}</>}<button className={`foreman-toggle ${foremanOpen ? "active" : ""}`} onClick={onToggleForeman}><span className={`status-dot ${bootstrap.foreman.configured ? "good" : "warn"}`} />{foremanOpen ? "隐藏厂长" : "打开厂长"}</button></div></div><div className="compose-body"><CanvasView graph={graph} layout={layout} onLayout={onLayout} onSelect={onSelect} onOpenWorkarea={onOpenWorkarea} />{scope === "workarea" && <BusinessRunsPanel businessSystemId={graph.id} revision={runRevision} />}{foremanOpen && <ForemanPanel open={foremanOpen} bootstrap={bootstrap} talk={talk} turns={foremanTurns} activity={Object.values(foremanActivity).at(-1) ?? ""} onCancel={onCancelTalk} onConfigureForeman={onConfigureForeman} onClose={onToggleForeman} onSend={onSendTalk} onOpenSecret={onOpenSecret} />}{selected && !harnessConfigRole && <NodeDetail node={selected} bootstrap={bootstrap} onConfigureOperator={onConfigureOperator} onBindingSaved={onBindingSaved} onClose={() => onSelect(null)} />}{harnessConfigRole && <HarnessConfigPanel open role={harnessConfigRole} bootstrap={bootstrap} initialRuntime={harnessConfigRuntime} onClose={onCloseHarnessConfig} onSaved={onHarnessProfileSaved} />}</div></section>;
}

function IntakePanel({ system, onClose, onSaved }: { system?: { id: string; title: string }; onClose: () => void; onSaved: () => Promise<void> }): ReactElement {
  const [name, setName] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [localFolder, setLocalFolder] = useState("");
  const [fileRef, setFileRef] = useState("");
  const [projectFiles, setProjectFiles] = useState<File[]>([]);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving || (!repoUrl.trim() && !localFolder.trim() && !fileRef.trim() && !projectFiles.length)) {
      setMessage("提供仓库链接、项目文件或本地文件夹中的一项。");
      return;
    }
    setSaving(true);
    setMessage("");
    try {
      await submitProjectIntake({ business_system_id: system?.id, name: name.trim() || undefined, repo_url: repoUrl.trim() || undefined, local_folder: localFolder.trim() || undefined, files: fileRef.trim() ? [{ name: fileRef.trim(), ref: fileRef.trim() }] : undefined }, projectFiles);
      setMessage("项目已进入 Foreman Intake 队列。");
      await onSaved();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "项目导入失败");
    } finally {
      setSaving(false);
    }
  };
  return <DetailDrawer title={system ? "向厂区添加项目" : "导入项目"} eyebrow="PROJECT INTAKE" onClose={onClose}>{system && <div className="context-line"><span>目标厂区</span><strong>{system.title}</strong></div>}<p>提供一个来源，Foreman 会继续识别项目、创建 Machine、绑定 Operator 并搭建业务 DAG。</p><form className="intake-form" onSubmit={submit}><label className="config-field"><span>项目名称</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 视频生产系统" /></label><label className="config-field"><span>仓库链接</span><input value={repoUrl} onChange={(event) => setRepoUrl(event.target.value)} placeholder="https://..." /></label><label className="config-field"><span>本地文件夹</span><input value={localFolder} onChange={(event) => setLocalFolder(event.target.value)} placeholder="本地目录引用" /></label><label className="config-field"><span>项目文件</span><input type="file" multiple onChange={(event) => setProjectFiles(Array.from(event.currentTarget.files ?? []))} /></label><label className="config-field"><span>已有文件引用</span><input value={fileRef} onChange={(event) => setFileRef(event.target.value)} placeholder="CAS 或上传引用" disabled={projectFiles.length > 0} /></label><button className="primary-button" type="submit" disabled={saving}>{saving ? "提交中…" : "交给 Foreman 搭建"}</button></form>{message && <div className="context-note">{message}</div>}</DetailDrawer>;
}

function DependencyState({ item }: { item: DependencyStatus }): ReactElement {
  return <span className={`status-badge ${statusTone(item.state)}`}>{statusLabel(item.state)}</span>;
}

function StorageState({ label, item }: { label: string; item?: StorageProviderStatus }): ReactElement {
  const state = item?.available ? "online" : "offline";
  return <div className="dependency-row"><span><strong>{label}</strong><small>{display(item?.kind, "未选择")}</small></span><span className={`status-badge ${statusTone(state)}`}>{item?.available ? "可用" : display(item?.reason, "待配置")}</span></div>;
}

function SettingsPage({ bootstrap, onRuntimeReady, onConfigureForeman }: { bootstrap: Bootstrap; onRuntimeReady: () => Promise<void>; onConfigureForeman: () => void }): ReactElement {
  const dependencies = bootstrap.dependencies ?? { harnesses: [], podman: { state: "unknown", message: "尚未读取" } };
  const runtime = [dependencies.kiri, dependencies.podman, dependencies.docker].find((item) => item && item.state !== "inactive") ?? { state: "unknown", message: "尚未读取" };
  const runtimeLabel = runtime.provider === "kiri" ? "Kiri" : "Podman";
  const [checking, setChecking] = useState(false);
  const [message, setMessage] = useState("");
  const startRuntime = async () => {
    if (checking) return;
    setChecking(true);
    setMessage("");
    try {
      await ensureRuntime();
      await onRuntimeReady();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : `${runtimeLabel} 尚未就绪`);
    } finally {
      setChecking(false);
    }
  };
  const foremanProfile = bootstrap.foreman.profile;
  const operatorProfiles = (bootstrap.harness_profiles ?? []).filter((item) => item.role === "operator");
  const sharedDefaults = [
    ["Foreman thinking effort", foremanProfile?.thinking_effort ?? "max"],
    ["Foreman context", foremanProfile?.context ?? 500000],
    ...(operatorProfiles.length
      ? operatorProfiles.flatMap((profile) => [
        [`Operator ${profile.runtime} thinking effort`, profile.thinking_effort ?? "medium"],
        [`Operator ${profile.runtime} context`, profile.context ?? 256000],
      ])
      : [["Operator thinking effort", "medium"], ["Operator context", 256000]]),
    ["Harness image", foremanProfile?.image || "本机官方 Harness"],
    ["Network", foremanProfile?.network || "bridge"],
    ["Mode", foremanProfile?.mode || "session"],
    ["Max turns", foremanProfile?.max_turns ?? 20],
    ["Timeout seconds", foremanProfile?.session_timeout_s ?? 600],
    ["Session budget", foremanProfile?.session_budget_usd ?? 1],
    ["Daily budget", foremanProfile?.daily_budget_usd ?? 10],
    ["Max attempts", foremanProfile?.max_attempts ?? 2],
    ["Backoff seconds", foremanProfile?.backoff_s ?? 60],
  ];
  const storage = dependencies.storage;
  return <section className="page settings-page"><div className="page-heading"><div><span className="eyebrow">PROJECT DEPENDENCIES</span><h1>设置</h1><p>项目依赖、Harness 运行默认值和共享资源都集中在这里。</p></div><ConnectionState state={bootstrap.connection.state} message={bootstrap.connection.message} /></div><div className="dependency-grid"><section className="panel dependency-card"><div className="panel-head"><div><span className="eyebrow">CONTAINER RUNTIME</span><h2>{runtimeLabel}</h2></div><DependencyState item={runtime} /></div><div className="context-line"><span>状态</span><strong>{display(runtime.message, "待检查")}</strong></div><div className="context-line"><span>版本</span><strong className="mono">{display(runtime.version, "—")}</strong></div><button className="secondary-button" onClick={() => void startRuntime()} disabled={checking}>{checking ? "检查中…" : `尝试启动 ${runtimeLabel}`}</button>{message && <div className="context-note">{message}</div>}</section><section className="panel dependency-card"><div className="panel-head"><div><span className="eyebrow">AGENT HARNESSES</span><h2>Harness</h2></div><span className="count-badge">{dependencies.harnesses.length} 个</span></div>{dependencies.harnesses.length ? <div className="dependency-list">{dependencies.harnesses.map((item) => <div className="dependency-row" key={item.id}><span><strong>{item.name}</strong><small>{display(item.version, "未声明版本")}</small></span><DependencyState item={item} /></div>)}</div> : <EmptyState text="Registry 中没有发现 agent harness。" />}</section>{storage && <section className="panel dependency-card"><div className="panel-head"><div><span className="eyebrow">STORAGE PROVIDERS</span><h2>存储与密钥</h2></div><span className="count-badge">{display(storage.mode, "local")}</span></div><div className="dependency-list"><StorageState label="状态库" item={storage.state} /><StorageState label="对象存储" item={storage.blob} /><StorageState label="密钥" item={storage.secret} /><StorageState label="KMS" item={storage.kms} /></div></section>}</div><section className="panel shared-settings-card"><div className="panel-head"><div><span className="eyebrow">RUNTIME SETTINGS</span><h2>运行参数</h2></div><div className="compose-actions"><span className="count-badge">厂长 {bootstrap.foreman.configured ? "已配置" : "待配置"} · Operator {operatorProfiles.length} 个</span><button className="secondary-button" onClick={onConfigureForeman}>编辑共享参数</button></div></div><p className="settings-help">除 Thinking effort 和 Context 外，其余运行参数由厂长与 Operator 共用。Thinking effort 和 Context 按角色分别配置。Base URL、API key 和模型仍在 Harness 配置中按角色选择。</p><div className="shared-settings-grid">{sharedDefaults.map(([label, value]) => <div className="shared-setting" key={label}><span>{label}</span><strong>{String(value)}</strong></div>)}</div></section></section>;
}

function assetTitle(asset: AssetRecord): string {
  return asset.title?.trim() || asset.summary?.trim() || asset.type;
}

function assetRecordLabel(asset: AssetRecord): string {
  return asset.record_type === "material" ? "物料" : "产物";
}

function AssetsPage({ bootstrap, onOpen, scope }: { bootstrap: Bootstrap; onOpen: (asset: AssetRecord) => void; scope?: WebScope }): ReactElement {
  const [olderPage, setOlderPage] = useState<AssetPageData>();
  const [pendingCursor, setPendingCursor] = useState<string>();
  const [failed, setFailed] = useState(false);
  const assets = olderPage?.items ?? bootstrap.assets;
  const total = bootstrap.assetTotal ?? bootstrap.assets.length;
  const olderCursor = olderPage ? olderPage.olderCursor : bootstrap.assetOlderCursor;

  useEffect(() => {
    if (!pendingCursor) return;
    const controller = new AbortController();
    void getAssetsPage(pendingCursor, controller.signal, scope).then((page) => {
      if (!controller.signal.aborted) setOlderPage(page);
    }).catch(() => {
      if (!controller.signal.aborted) setFailed(true);
    }).finally(() => {
      if (!controller.signal.aborted) setPendingCursor(undefined);
    });
    return () => controller.abort();
  }, [pendingCursor, scope]);

  return <section className="page list-page">
    <div className="page-heading">
      <div><span className="eyebrow">ASSET REGISTRY</span><h1>资产</h1><p>输入素材、输出资产与封存状态。完整 Lineage 在上下文中展开。</p></div>
      <span className="count-badge">{total} 项</span>
    </div>
    {(olderPage || olderCursor) && <nav className="asset-page-actions" aria-label="资产分页">
      {olderPage && <button type="button" className="secondary-button" onClick={() => {
        setPendingCursor(undefined);
        setOlderPage(undefined);
        setFailed(false);
      }}>返回最新</button>}
      {olderCursor && <button type="button" className="secondary-button" disabled={Boolean(pendingCursor)} onClick={() => {
        setFailed(false);
        setPendingCursor(olderCursor);
      }}>{pendingCursor ? "读取中…" : "加载更早"}</button>}
      {failed && <span role="status">读取失败，请重试。</span>}
    </nav>}
    <div className="panel list-panel" aria-busy={Boolean(pendingCursor)}>
      <div className="list-head"><span>资产</span><span>来源</span><span>状态</span><span /></div>
      {assets.length ? assets.map((asset) => (
        <button className="asset-row" key={asset.id} onClick={() => onOpen(asset)}>
          <span className="breakable">
            <strong>{assetTitle(asset)}</strong>
            <small>{assetRecordLabel(asset)}{assetTitle(asset) !== asset.type && ` · ${asset.type}`}</small>
          </span>
          <span>{display(asset.source?.node, "暂无来源")}</span>
          <span className={`status-badge ${asset.sealed ? "good" : "warn"}`}>{asset.sealed ? "封存" : "处理中"}</span><b>›</b>
        </button>
      )) : <EmptyState text="暂无资产。运行完成后，资产会从真实仓库进入这里。" />}
    </div>
  </section>;
}

function LogsPage({ logs, active, onOpen }: { logs: EventRecord[]; active: EventRecord | null; onOpen: (item: EventRecord) => void }): ReactElement { return <section className="page list-page"><div className="page-heading"><div><span className="eyebrow">AUTONOMY LOG</span><h1>日志</h1><p>运行、验证、修复和异常统一进入时间线，不作为审批队列。</p></div><span className="count-badge">{logs.length} 条</span></div><div className="log-layout"><div className="panel list-panel log-list">{logs.length ? logs.slice().reverse().map((item, index) => <button className="log-row" key={item.id ?? index} onClick={() => onOpen(item)}><time>{display(item.at, "").slice(11, 19)}</time><span className={`signal ${statusTone(item.kind)}`} /><span><strong>{item.kind}</strong><small>{item.id}</small></span><b>›</b></button>) : <EmptyState text="还没有事件记录。" />}</div>{active ? <LogContext log={active} /> : <div className="panel log-placeholder"><span className="eyebrow">CONTEXT</span><h2>选择一条日志</h2><p>查看运行、ChangeSet、Epoch、验证和异常上下文。</p></div>}</div></section>; }
function LogContext({ log }: { log: EventRecord }): ReactElement { return <div className="panel log-context"><span className="eyebrow">EVENT CONTEXT</span><h2>{log.kind}</h2><p>{log.id}</p><div className="context-line"><span>时间</span><strong>{display(log.at)}</strong></div><div className="context-line"><span>运行</span><strong>{display(log.run_id ?? log.job_id, "无")}</strong></div><details open><summary>事件摘要</summary><div className="semantic-list">{semanticEntries(log.data).map(([key, value]) => <div className="context-line" key={key}><span>{key}</span><strong>{value}</strong></div>)}</div></details></div>; }
function DetailDrawer({ title, eyebrow, onClose, children }: { title: string; eyebrow: string; onClose: () => void; children: ReactNode }): ReactElement { return <aside className="context-drawer"><button className="icon-button" onClick={onClose} aria-label="关闭详情">×</button><span className="eyebrow">{eyebrow}</span><h2>{title}</h2>{children}</aside>; }
function SecretDrawer({ secret, requests, onSelect, onClose, onSaved }: { secret: SecretRecord; requests: SecretRecord[]; onSelect: (request: SecretRecord) => void; onClose: () => void; onSaved: () => Promise<void> }): ReactElement {
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const requestId = secret.request?.id;
  const sourceTool = secret.kind === "source-tool";
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!requestId || !value || saving) return;
    setSaving(true);
    try {
      await putProjectSecret(requestId, value);
      setValue("");
      setMessage("已写入项目保险箱。");
      await onSaved();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "密钥写入失败");
    } finally {
      setSaving(false);
    }
  };
  const markReady = async () => {
    if (!requestId || saving) return;
    setSaving(true);
    try {
      await markProjectResourceReady(requestId);
      setMessage("项目来源工具已标记为可用，运行将自动恢复。");
      await onSaved();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "资源状态更新失败");
    } finally {
      setSaving(false);
    }
  };
  return <DetailDrawer title="项目资源请求" eyebrow="PROJECT RESOURCE" onClose={onClose}>
    {requests.length > 1 && <label className="config-field"><span>项目请求</span><select value={secret.id} onChange={(event) => {
      const selection = requests.find((item) => item.id === event.target.value);
      if (selection) onSelect(selection);
    }}>{requests.map((item) => <option key={item.id} value={item.id}>{item.request?.project_name ? `${item.request.project_name} · ` : ""}{item.key} · {item.configured ? "已配置" : "待补充"}</option>)}</select></label>}
    {secret.request?.project_name && <div className="context-line"><span>请求项目</span><strong>{secret.request.project_name}</strong></div>}
    <div className="secret-key"><strong>{secret.key}</strong><span className={`status-badge ${secret.configured ? "good" : "bad"}`}>{secret.configured ? (sourceTool ? "已准备" : "已配置") : (sourceTool ? "待准备" : "待补充")}</span></div>
    {sourceTool ? <p>项目需要在当前宿主准备 {secret.source_tool ?? "来源工具"}。安装或确认工具可用后标记资源，等待中的 Operator 会自动恢复。</p> : secret.request?.purpose && <p>{display(secret.request.purpose)}</p>}
    <div className="context-line"><span>使用范围</span><strong>{display(secret.usage_scope ?? secret.request?.scope, "未声明")}</strong></div>
    {secret.agent_generated_checks?.length ? <details className="detail-collapse" open><summary>Agent 生成的检查项</summary><ul className="check-list">{secret.agent_generated_checks.map((check, index) => <li key={`${check}-${index}`}><strong>{check}</strong></li>)}</ul></details> : null}
    {requestId && !secret.configured && sourceTool && <button className="primary-button" type="button" onClick={() => void markReady()} disabled={saving}>{saving ? "更新中…" : "标记已准备"}</button>}
    {requestId && !secret.configured && !sourceTool && <form className="secret-form" onSubmit={submit}>
      <label className="config-field"><span>填写这项项目密钥</span><input type="password" value={value} onChange={(event) => setValue(event.target.value)} autoComplete="off" /></label>
      <button className="primary-button" type="submit" disabled={!value || saving}>{saving ? "写入中…" : "写入保险箱"}</button>
    </form>}
    {message && <div className="context-note">{message}</div>}
  </DetailDrawer>;
}
const artifactFileDownload = /^\/api\/v1\/projects\/[a-z0-9][a-z0-9_.-]*\/artifacts\/[a-z0-9][a-z0-9_.-]*\/files\/sha256(?::|%3a)[a-f0-9]{64}$/i;

function AssetFiles({ files }: { files: NonNullable<AssetRecord["files"]> }) {
  return <details className="detail-collapse" open>
    <summary>文件附件</summary>
    <ul className="check-list">
      {files.map((file, index) => {
        const basename = file.path && !file.path.includes("://") ? file.path.split(/[\\/]/).at(-1)?.trim() : undefined;
        const name = basename && !basename.includes(":") ? basename : "文件附件";
        const download = file.download && artifactFileDownload.test(file.download) ? file.download : undefined;
        const mediaType = file.mediaType && /^[\w.+-]+\/[\w.+-]+$/.test(file.mediaType) ? file.mediaType : undefined;
        return <li key={`${file.ref}-${index}`} className="breakable">
          {download ? <a href={download} download aria-label={`下载 ${name}`}>{name}</a> : <strong>{name}</strong>}
          {mediaType && <>{" "}<small>{mediaType}</small></>}
        </li>;
      })}
    </ul>
  </details>;
}

function AssetDrawer({ asset, onClose }: { asset: AssetRecord; onClose: () => void }): ReactElement {
  const lineage = [...new Set([...(asset.lineage ?? []), ...(asset.inputs ?? [])])];
  const outputs = [...new Set(asset.outputs ?? [])];
  return <DetailDrawer title={assetTitle(asset)} eyebrow="ASSET CONTEXT" onClose={onClose}>
    <div className="context-line"><span>类型</span><strong>{assetRecordLabel(asset)} · {asset.type}</strong></div>
    <div className="context-line"><span>标识</span><strong className="breakable">{asset.id}</strong></div>
    <div className="context-line"><span>说明</span><strong>{display(asset.description || asset.summary, "暂无说明")}</strong></div>
    <div className="context-line"><span>封存</span><strong>{asset.sealed ? "已封存" : "处理中"}</strong></div>
    <div className="context-line"><span>Validation Epoch</span><strong className="breakable">{display(asset.validation_epoch, "未声明")}</strong></div>
    <div className="context-line"><span>来源运行</span><strong>{display(asset.source?.run, "暂无")}</strong></div>
    <div className="context-line"><span>来源节点</span><strong>{display(asset.source?.node, "暂无")}</strong></div>
    <section className="asset-lineage" aria-label="产物物料流转图"><span className="eyebrow">MATERIAL FLOW</span><div className="asset-flow-track">
      {lineage.length ? <>{lineage.map((ref, index) => <span className="asset-flow-node" key={`input-${ref}-${index}`}>{ref}</span>)}<b>→</b></> : <span className="asset-flow-node muted">起点由项目定义</span>}
      <span className="asset-flow-node current">{assetTitle(asset)}</span>
      {outputs.map((ref, index) => <Fragment key={`output-${ref}-${index}`}><b>→</b><span className="asset-flow-node">{ref}</span></Fragment>)}
    </div></section>
    {asset.files?.length ? <AssetFiles files={asset.files} /> : null}
  </DetailDrawer>;
}
function ConstitutionDrawer({ bootstrap, onClose }: { bootstrap: Bootstrap; onClose: () => void }): ReactElement { const constitution = bootstrap.constitution; return <DetailDrawer title="宪法" eyebrow="CONSTITUTION" onClose={onClose}><div className="context-line"><span>当前版本</span><strong>{display(constitution.version, "未配置")}</strong></div><div className="context-line"><span>生效状态</span><strong>{display(constitution.status, "未知")}</strong></div><div className="context-line"><span>最近变更</span><strong>{display(constitution.recent_change, "暂无")}</strong></div><div className="context-note">宪法冲突会进入日志异常上下文，由人处理边界决策，普通运行由 Foreman 自治。</div></DetailDrawer>; }

export function App(): ReactElement {
  const initialLayout = useRef(readLayout());
  const [page, setPage] = useState<Page>("dashboard"); const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null); const [connection, setConnection] = useState("connecting"); const [error, setError] = useState(""); const [scope, setScope] = useState<Scope>("factory"); const [workarea, setWorkarea] = useState<string | null>(null); const [workareaGraph, setWorkareaGraph] = useState<Graph | null>(null); const [selected, setSelected] = useState<GraphNode | null>(null); const [layout, setLayout] = useState<LayoutState>(initialLayout.current); const [sidebarOpen, setSidebarOpen] = useState(false); const [sidebarDock, setSidebarDock] = useState<"left" | "right">(initialLayout.current.sidebarDock ?? "left"); const [maoTop, setMaoTop] = useState(initialLayout.current.maoTop ?? Math.round(window.innerHeight * 0.5)); const [foremanOpen, setForemanOpen] = useState(false); const [talk, setTalk] = useState<TalkMessage[]>([]); const [harnessConfigRole, setHarnessConfigRole] = useState<"operator" | "foreman" | null>(null); const [harnessConfigRuntime, setHarnessConfigRuntime] = useState(""); const [activeAsset, setActiveAsset] = useState<AssetRecord | null>(null); const [activeSecret, setActiveSecret] = useState<SecretRecord | null>(null); const [activeLog, setActiveLog] = useState<EventRecord | null>(null); const [constitutionOpen, setConstitutionOpen] = useState(false);
  const [intake, setIntake] = useState<{ system?: { id: string; title: string } } | null>(null);
  const persistLayout = useCallback((next: LayoutState) => { setLayout(next); localStorage.setItem(LAYOUT_KEY, JSON.stringify(next)); }, []);
  const [liveReplies, setLiveReplies] = useState<Record<string, TalkMessage>>({});
  const [foremanActivity, setForemanActivity] = useState<Record<string, string>>({});
  const [foremanTurns, setForemanTurns] = useState<Record<string, ForemanTurn>>({});
  const cancelledTurns = useRef(new Set<string>());
  const [eventCursor, setEventCursor] = useState<string>();
  const [workareaRevision, setWorkareaRevision] = useState(0);
  const [runRevision, setRunRevision] = useState(0);
  const [conversionRevision, setConversionRevision] = useState(0);
  const [assetRevision, setAssetRevision] = useState(0);
  const activeWorkarea = useRef<string | null>(null);
  const startedEvents = useRef(false);
  const activeWebScope = useCallback((): WebScope | undefined => scope === "workarea" && workarea ? { business_system_id: workarea } : undefined, [scope, workarea]);
  const rememberTurn = useCallback((turn: ForemanTurn) => {
    if (turn.status === "cancelled") {
      cancelledTurns.current.add(turn.id);
      setLiveReplies((current) => Object.fromEntries(Object.entries(current).filter(([id]) => id !== turn.id)));
    }
    setForemanTurns((current) => {
      const previous = current[turn.messageId];
      if (previous && foremanTurnStatus[previous.status].rank > foremanTurnStatus[turn.status].rank) return current;
      return { ...current, [turn.messageId]: turn };
    });
  }, []);
  const retry = useCallback(async () => {
    setError("");
    setConnection("connecting");
    try {
      const next = await getBootstrap(activeWebScope());
      setBootstrap(next);
      setTalk(next.talk ?? []);
      for (const turn of next.foremanTurns ?? []) rememberTurn(turn);
      if (!startedEvents.current) {
        setLiveReplies(Object.fromEntries(Object.entries(next.pendingForemanReplies ?? {}).filter(([id]) => !cancelledTurns.current.has(id)).map(([id, body]) => [id, {
          id: `live-${id}`, role: "foreman", body, state: "回复中", metadata: { inbox_id: id },
        }])));
        setEventCursor(String(next.eventCursor ?? 0));
        startedEvents.current = true;
      }
      setConnection(next.connection.state);
    } catch (cause) {
      setConnection("exception");
      setError(cause instanceof Error ? cause.message : "无法连接本地服务");
    }
  }, [activeWebScope, rememberTurn]);
  const updateOperatorNode = useCallback((unitId: string, binding: OperatorBindingView) => {
    setWorkareaGraph((current) => current && current.id === binding.businessSystemId ? {
      ...current,
      nodes: current.nodes.map((node) => node.id === unitId ? { ...node, operatorBinding: binding } : node),
    } : current);
  }, []);
  const refreshLive = useCallback(async (eventType: string, eventData?: Record<string, unknown>) => {
    setConnection("connected");
    const type = typeof eventData?.type === "string" ? eventData.type : eventType;
    const inboxId = eventData?.inboxId;
    if (type.startsWith("foreman.message.")) {
      const turn = readForemanTurn(eventData);
      if (turn) rememberTurn(turn);
      return;
    }
    if (type === "foreman.runtime.blocked" && typeof inboxId === "string") {
      const reason = typeof eventData?.reason === "string" ? eventData.reason : "可信 Foreman 运行环境不可用";
      setForemanActivity((current) => ({ ...current, [inboxId]: reason }));
      setForemanTurns((current) => {
        const previous = current[inboxId];
        return previous ? { ...current, [inboxId]: { ...previous, businessStatus: "blocked_environment", businessReason: reason } } : current;
      });
      return;
    }
    if (type === "foreman.session.failed" && typeof inboxId === "string") {
      const turn = readForemanTurn(eventData);
      if (turn) rememberTurn(turn);
      if (!cancelledTurns.current.has(inboxId)) {
        const reason = typeof eventData?.reason === "string" ? eventData.reason : "厂长会话中断";
        setLiveReplies((current) => ({ ...current, [inboxId]: {
          id: `live-${inboxId}`, role: "foreman", body: current[inboxId]?.body || reason, state: "会话失败", metadata: { inbox_id: inboxId },
        } }));
      }
      setForemanActivity((current) => ({ ...current, [inboxId]: "厂长本轮处理失败" }));
      return;
    }
    if (type.startsWith("foreman.session.") && typeof inboxId === "string") {
      const activity = type.endsWith("tool.started") ? "厂长正在执行工具操作…"
        : type.endsWith("tool.finished") ? "厂长已完成工具操作，正在整理结果…"
          : type.endsWith("started") ? "厂长已开始处理本轮…"
            : type.endsWith("finished") ? "厂长已完成本轮处理"
              : type.endsWith("failed") ? "厂长本轮处理失败"
                : "厂长正在处理…";
      setForemanActivity((current) => ({ ...current, [inboxId]: activity }));
      return;
    }
    if (type === "business-unit.binding.changed" && typeof eventData?.unitId === "string"
      && eventData.binding && typeof eventData.binding === "object") {
      updateOperatorNode(eventData.unitId, eventData.binding as OperatorBindingView);
      return;
    }
    if (type === "business-conversion.created") {
      if (eventData?.businessSystemId === activeWorkarea.current) setConversionRevision((revision) => revision + 1);
      return;
    }
    if (type === "business-system.graph.updated") {
      if (eventData?.id === activeWorkarea.current) setWorkareaRevision((revision) => revision + 1);
      return;
    }
    if (type === "harness.profile.verification.completed" && typeof eventData?.id === "string"
      && eventData.verification && typeof eventData.verification === "object") {
      const verification = eventData.verification as HarnessProfile["verification"];
      const id = eventData.id;
      setBootstrap((current) => current ? {
        ...current,
        harness_profiles: current.harness_profiles?.map((item) => item.id === id ? { ...item, verification } : item),
      } : current);
      return;
    }
    if ((type === "talk.foreman.replied" || type === "talk.message.created") && typeof eventData?.id === "string" && typeof eventData.body === "string") {
      const messageMetadata = eventData.metadata && typeof eventData.metadata === "object" ? eventData.metadata as Record<string, unknown> : {};
      const message = { id: eventData.id, body: eventData.body, role: typeof eventData.role === "string" ? eventData.role : "foreman", metadata: typeof eventData.inboxId === "string" ? { ...messageMetadata, inbox_id: eventData.inboxId } : messageMetadata };
      setTalk((current) => [...current.filter((item) => item.id !== message.id), message]);
      if (typeof inboxId === "string") setLiveReplies((current) => Object.fromEntries(Object.entries(current).filter(([id]) => id !== inboxId)));
      return;
    }
    if (type === "foreman.reply.delta" && typeof inboxId === "string" && typeof eventData?.text === "string") {
      if (cancelledTurns.current.has(inboxId)) return;
      const text = eventData.text;
      setLiveReplies((current) => ({ ...current, [inboxId]: {
        id: `live-${inboxId}`, role: "foreman", body: (current[inboxId]?.body ?? "") + text, state: "回复中", metadata: { inbox_id: inboxId },
      } }));
      return;
    }
    if (type === "project.intake.created" && activeWorkarea.current && eventData?.businessSystemId === activeWorkarea.current) {
      setWorkareaRevision((revision) => revision + 1);
    }
    const normalized = `${type} ${String(eventData?.kind ?? "")}`.toLowerCase();
    try {
      if (type === "artifact.created") {
        setAssetRevision((revision) => revision + 1);
        return;
      }
      if (normalized.includes("talk")) {
        const messages = await getTalk(activeWebScope());
        setTalk(messages);
        setBootstrap((current) => current ? { ...current, talk: messages } : current);
        return;
      }
      if (normalized.includes("event") || normalized.includes("log") || normalized.includes("run") || normalized.includes("job")) {
        if (normalized.includes("run") || normalized.includes("job")) setRunRevision((revision) => revision + 1);
        const logs = await getLogs(activeWebScope());
        setBootstrap((current) => current ? { ...current, logs } : current);
        return;
      }
      if (normalized.includes("graph") || normalized.includes("asset") || normalized.includes("secret") || normalized.includes("bootstrap") || normalized.includes("project") || normalized.includes("intake") || normalized.includes("business-system") || normalized.includes("conversion")) {
        const next = await getBootstrap(activeWebScope());
        setBootstrap(next);
        setTalk(next.talk ?? []);
        for (const turn of next.foremanTurns ?? []) rememberTurn(turn);
        setConnection(next.connection.state);
      }
    } catch {
      // SSE is an enhancement. The initial page remains usable while a later event is retried.
    }
  }, [activeWebScope, rememberTurn, updateOperatorNode]);
  useEffect(() => { void retry(); }, [retry]);
  useEffect(() => eventCursor === undefined ? undefined : subscribeToEvents((event) => { void refreshLive(event.type, event.data); }, () => {
    setConnection((current) => current === "exception" ? current : "degraded");
  }, eventCursor), [eventCursor, refreshLive]);
  useEffect(() => {
    if (!assetRevision) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void getAssetsPage(undefined, controller.signal, activeWebScope()).then(({ items, total, olderCursor }) => {
        if (controller.signal.aborted) return;
        setBootstrap((current) => current ? {
          ...current, assets: items, assetTotal: total, assetOlderCursor: olderCursor,
        } : current);
      }).catch(() => {
        // Keep the previous page while a later event or retry refreshes the registry.
      });
    }, 75);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [activeWebScope, assetRevision]);
  useEffect(() => {
    activeWorkarea.current = page === "compose" && scope === "workarea" ? workarea : null;
    if (!activeWorkarea.current) return;
    let current = true;
    void getGraph("workarea", activeWorkarea.current).then((graph) => {
      if (current) setWorkareaGraph(graph);
    }).catch((cause) => {
      if (current) setError(cause instanceof Error ? cause.message : "厂区读取失败");
    });
    return () => { current = false; };
  }, [page, scope, workarea, workareaRevision]);
  useEffect(() => { const handleResize = () => setMaoTop((value) => Math.max(44, Math.min(window.innerHeight - 44, value))); window.addEventListener("resize", handleResize); return () => window.removeEventListener("resize", handleResize); }, []);
  useEffect(() => { localStorage.setItem(LAYOUT_KEY, JSON.stringify({ ...layout, sidebarDock, maoTop })); }, [layout, sidebarDock, maoTop]);
  const factoryGraph = bootstrap?.graph.factory ?? null; const activeGraph = scope === "factory" ? factoryGraph : workareaGraph;
  const selectedNode = selected ? activeGraph?.nodes.find((node) => node.id === selected.id) ?? null : null;
  const navigate = (next: Page) => { setPage(next); setSidebarOpen(false); setHarnessConfigRole(null); if (next !== "compose") { setSelected(null); setForemanOpen(false); } };
  const openWorkarea = (id: string) => { setPage("compose"); setScope("workarea"); setWorkarea(id); setSelected(null); setError(""); };
  const returnFactory = () => { setScope("factory"); setWorkarea(null); setWorkareaGraph(null); setSelected(null); };
  const openForemanConfig = () => { setPage("compose"); setScope("factory"); setForemanOpen(true); setSelected(null); setHarnessConfigRuntime(bootstrap?.foreman.runtime ?? ""); setHarnessConfigRole("foreman"); };
  const openOperatorConfig = (runtime?: string) => { setHarnessConfigRuntime(runtime ?? ""); setHarnessConfigRole("operator"); };
  const closeHarnessConfig = () => setHarnessConfigRole(null);
  const updateHarnessProfile = (profile: HarnessProfile, foreman: Bootstrap["foreman"]) => { setBootstrap((current) => { if (!current) return current; const profiles = [...(current.harness_profiles ?? []).filter((item) => item.id !== profile.id), profile]; return { ...current, harness_profiles: profiles, foreman }; }); };
  const updateGraphLayout = useCallback((graph: Graph, nodes: FlowNode[], viewport: Viewport) => { const current = { ...layout, graphs: { ...(layout.graphs ?? {}) } }; current.graphs![graphKey(graph)] = { nodes: Object.fromEntries(nodes.map((node) => [node.id, { x: node.position.x, y: node.position.y }])), viewport: { x: viewport.x, y: viewport.y, zoom: viewport.zoom } }; persistLayout(current); }, [layout, persistLayout]);
  const sendToForeman = async (text: string) => {
    try {
      const response = await sendTalk(text, activeWebScope());
      if (response.foremanTurn) rememberTurn(response.foremanTurn);
      setTalk((items) => items.some((item) => item.id === response.message.id) ? items : [...items, response.message]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Foreman 对话不可用");
      throw cause;
    }
  };
  const cancelTalk = async (id: string) => {
    try {
      const result = await cancelForemanTurn(id);
      rememberTurn(result.foremanTurn);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409 && cause.payload && typeof cause.payload === "object") {
        const turn = readForemanTurn((cause.payload as { foremanTurn?: unknown }).foremanTurn);
        if (turn) rememberTurn(turn);
      }
      setError(cause instanceof Error ? cause.message : "无法取消本轮");
    }
  };
  if (!bootstrap && connection === "connecting") return <div className="boot-screen"><div className="boot-mark">M</div><strong>正在连接本地工厂</strong><span>读取真实执行图与运行上下文…</span></div>;
  return <div className={`app-shell dock-${sidebarDock}`}><Sidebar open={sidebarOpen} dock={sidebarDock} page={page} connection={connection} onClose={() => setSidebarOpen(false)} onNavigate={navigate} /><MaoButton dock={sidebarDock} top={maoTop} onToggle={() => setSidebarOpen((value) => !value)} onDock={(dock, top) => { setSidebarDock(dock); setMaoTop(top); }} /><main className="app-main">{error && <ErrorBanner message={error} onRetry={retry} />}{page === "dashboard" && bootstrap && <Dashboard bootstrap={bootstrap} onCompose={() => navigate("compose")} onIntake={() => setIntake({})} onSecrets={() => setActiveSecret(bootstrap.secrets.find((item) => !item.configured) ?? bootstrap.secrets[0] ?? null)} onConstitution={() => setConstitutionOpen(true)} onWorkarea={openWorkarea} onForemanConfig={openForemanConfig} />}{page === "compose" && bootstrap && activeGraph && <ComposePage bootstrap={bootstrap} graph={activeGraph} scope={scope} layout={layout} foremanOpen={foremanOpen} selected={selectedNode} talk={[...talk, ...Object.values(liveReplies)]} foremanTurns={foremanTurns} foremanActivity={foremanActivity} runRevision={runRevision} onCancelTalk={cancelTalk} harnessConfigRole={harnessConfigRole} harnessConfigRuntime={harnessConfigRuntime} onToggleForeman={() => setForemanOpen((value) => !value)} onBack={returnFactory} onLayout={updateGraphLayout} onSelect={setSelected} onOpenWorkarea={openWorkarea} onConfigureForeman={openForemanConfig} onConfigureOperator={openOperatorConfig} onBindingSaved={updateOperatorNode} onCloseHarnessConfig={closeHarnessConfig} onHarnessProfileSaved={updateHarnessProfile} onSendTalk={sendToForeman} onOpenSecret={setActiveSecret} materialConversions={workarea === activeGraph.id && <WorkareaConversions key={activeGraph.id} businessSystemId={activeGraph.id} revision={conversionRevision} />} onAddProject={() => { if (workarea === activeGraph.id) setIntake({ system: { id: activeGraph.id, title: activeGraph.title } }); }} />}{page === "settings" && bootstrap && <SettingsPage bootstrap={bootstrap} onRuntimeReady={retry} onConfigureForeman={openForemanConfig} />}{page === "assets" && bootstrap && <AssetsPage bootstrap={bootstrap} scope={activeWebScope()} onOpen={setActiveAsset} />}{page === "logs" && bootstrap && <LogsPage logs={bootstrap.logs} active={activeLog} onOpen={setActiveLog} />}</main>{intake && <IntakePanel system={intake.system} onClose={() => setIntake(null)} onSaved={retry} />}{activeSecret && <SecretDrawer key={activeSecret.id} secret={bootstrap?.secrets.find((item) => item.id === activeSecret.id) ?? activeSecret} requests={bootstrap?.secrets ?? []} onSelect={setActiveSecret} onClose={() => setActiveSecret(null)} onSaved={retry} />}{activeAsset && <AssetDrawer asset={activeAsset} onClose={() => setActiveAsset(null)} />}{constitutionOpen && bootstrap && <ConstitutionDrawer bootstrap={bootstrap} onClose={() => setConstitutionOpen(false)} />}</div>;
}
