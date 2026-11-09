"""嵌入表达式的静态检查：E-EXPR / W-EXPR / W-PROPERTY-META。

Spec 中的每一处 MRL 表达式或模板（属性定义、验收标准、equivalence、审批条件、幂等键、
步骤参数模板、switch 条件、until 条件）都在规定的类型环境中编译。类型环境见
spec/mrl/factory-profile.md 第 4 章；这里是它的参考实现。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .. import types as T
from ..errors import Diagnostic, StaticError
from ..expr import compile_expr, compile_template
from ..rules import Model
from ..stdlib import make_table
from .extract import Extraction, Site
from .registry import HostDiag

PAYLOAD = T.ListT(T.record({"path": T.STRING, "digest": T.STRING, "size": T.INT, "media_type": T.STRING}))
ANY_BEAN = T.record(
    {"id": T.STRING, "type": T.STRING, "facts": T.MapT(T.DYN), "meta": T.MapT(T.DYN), "payload": PAYLOAD}
)
RUN = T.record(
    {
        "id": T.STRING,
        "epoch": T.STRING,
        "line": T.STRING,
        "budget": T.record({"cap_usd": T.FLOAT, "spent_usd": T.FLOAT, "remaining_usd": T.FLOAT}),
    }
)
PROPOSAL = T.record({"summary": T.STRING, "cost_estimate_usd": T.NUM, "time_estimate_s": T.NUM})
REQUEST = T.record({"kind": T.STRING, "summary": T.STRING, "cost_usd": T.NUM})
PARAM_TYPES = {"string": T.STRING, "int": T.INT, "float": T.FLOAT, "num": T.NUM, "bool": T.BOOL}
# OperatorRuntime 的 argv / env 模板可用的名字（由控制面在启动操作工时提供）
RUNTIME_ENV = {
    "brief": T.record({"task": T.STRING, "factory_contract": T.STRING}),
    "limits": T.record({"max_turns": T.INT, "timeout_s": T.INT, "budget_usd": T.FLOAT}),
    "operator": T.record({"model": T.STRING, "runtime": T.STRING}),
}


def gate_type(has_judge: bool) -> T.Type:
    """until 条件中的 gate：attempt 总是存在；judge_score 只在工位配置了 judge 时保证存在。"""
    return T.record(
        {"attempt": T.INT, "judge_score": T.FLOAT}, {"attempt", "judge_score"} if has_judge else {"attempt"}
    )


def schema_type(s: Any) -> T.Type:
    """JSON Schema（meta_schema）→ MRL 类型。只识别常用关键字，其余一律为 dyn。"""
    if not isinstance(s, dict):
        return T.DYN
    if "const" in s:
        return _literal_type(s["const"])
    if "enum" in s and isinstance(s["enum"], list) and s["enum"]:
        t: T.Type | None = None
        for v in s["enum"]:
            vt = _literal_type(v)
            t = vt if t is None else T.lub(t, vt)
        return t or T.DYN
    ty = s.get("type")
    if ty is None and "properties" in s:
        ty = "object"
    if ty == "string":
        return T.STRING
    if ty == "integer":
        return T.INT
    if ty == "number":
        return T.NUM
    if ty == "boolean":
        return T.BOOL
    if ty == "null":
        return T.NULL
    if ty == "array":
        return T.ListT(schema_type(s.get("items")) if isinstance(s.get("items"), dict) else T.DYN)
    if ty == "object":
        props = s.get("properties")
        if isinstance(props, dict):
            fields = {k: schema_type(v) for k, v in props.items()}
            required = {k for k in s.get("required") or [] if isinstance(k, str)}
            for k in required - set(fields):
                fields[k] = T.DYN
            return T.record(fields, required, s.get("additionalProperties", True) is not False)
        extra = s.get("additionalProperties")
        return T.MapT(schema_type(extra) if isinstance(extra, dict) else T.DYN)
    return T.DYN


def _literal_type(v: Any) -> T.Type:
    if v is None:
        return T.NULL
    if isinstance(v, bool):
        return T.BOOL
    if isinstance(v, int):
        return T.INT
    if isinstance(v, float):
        return T.FLOAT
    if isinstance(v, str):
        return T.STRING
    return T.DYN


class TypeEnv:
    """由抽取结果与第一遍求值的模型构造 Bean 的类型。"""

    def __init__(self, x: Extraction, model: Model):
        self.x = x
        self.type_key: dict[str, str] = {tid: key for tid, key in model.rows("type_key")}
        self.facts: dict[str, dict[str, str]] = defaultdict(dict)
        for t, f, ft in model.rows("type_fact"):
            self.facts[t].setdefault(f, ft)
        self.parent: dict[str, str] = {t: p for t, p in model.rows("extends")}
        self.resolved: dict[tuple[str, str], str] = {(k, r): key for k, r, key in model.rows("resolved")}
        self.step_out: dict[tuple[str, str, str], str] = {
            (line, st, port): u for line, st, port, u, _g in model.rows("step_out_port")
        }
        self.own_facts: dict[str, dict[str, str]] = defaultdict(dict)
        for key, f, ft in self.x.facts.get("type_fact_decl", []):
            self.own_facts[key].setdefault(f, ft)
        self.key_id: dict[str, str] = {
            key: tid for key, tid, _minor, _lc in self.x.facts.get("type_version", [])
        }
        self.key_parent: dict[str, str] = {key: p for key, p in self.x.facts.get("type_extends_decl", [])}

    def meta_type(self, tid: str | None, key: str | None = None) -> T.Type:
        seen: set[str] = set()
        if key is not None and key in self.x.meta_schemas:
            return schema_type(self.x.meta_schemas[key])
        t = self.key_parent.get(key) if key is not None else tid
        while t and t not in seen:
            seen.add(t)
            k = self.type_key.get(t)
            if k is not None and k in self.x.meta_schemas:
                return schema_type(self.x.meta_schemas[k])
            t = self.parent.get(t)
        return T.RecordT((), True)

    def _bean(self, facts: dict[str, str], meta: T.Type) -> T.Type:
        ft: dict[str, T.Type] = {}
        for f, s in sorted(facts.items()):
            try:
                ft[f] = T.parse_type(s)
            except ValueError:
                ft[f] = T.DYN
        return T.record(
            {"id": T.STRING, "type": T.STRING, "facts": T.record(ft), "meta": meta, "payload": PAYLOAD}
        )

    def bean_of_key(self, key: str) -> T.Type:
        """某个 Type 文档（具体到 minor）的 Bean 类型：自身声明的 facts + 父类型（最新 minor）的 facts。"""
        facts = dict(self.facts.get(self.key_parent.get(key, ""), {}))
        facts.update(self.own_facts.get(key, {}))
        return self._bean(facts, self.meta_type(None, key))

    def bean(self, texpr_id: str) -> T.Type:
        te = self.x.texprs.get(texpr_id)
        if te is None or te.form == "any":
            return ANY_BEAN
        if te.form == "list":
            assert te.arg is not None
            return T.ListT(self.bean(te.arg.id))
        if te.form == "ref":
            assert te.arg is not None
            b = self.bean(te.arg.id)
            if isinstance(b, T.RecordT):
                return T.RecordT(tuple(sorted(b.fields + (("uri", T.STRING, True),))), b.open)
            return b
        tid = te.type_id
        if tid not in self.type_key:
            return ANY_BEAN  # 引用无法解析：E-REF 已经报告，这里不再连带报错
        return self._bean(self.facts.get(tid, {}), self.meta_type(tid))

    def ports(self, station: str, direction: str) -> T.Type:
        rows = self.x.facts.get("in_port" if direction == "inputs" else "out_port", [])
        fields: dict[str, T.Type] = {}
        required: set[str] = set()
        for key, port, texpr, optional, _props in rows:
            if key != station:
                continue
            fields[port] = self.bean(texpr)
            if not optional:
                required.add(port)
        return T.record(fields, required)

    def recipe_ports(self, machine: str, recipe: str) -> T.Type:
        fields: dict[str, T.Type] = {}
        required: set[str] = set()
        for m, r, port, texpr, optional in self.x.facts.get("recipe_in", []):
            if (m, r) == (machine, recipe):
                fields[port] = self.bean(texpr)
                if not optional:
                    required.add(port)
        return T.record(fields, required)

    def recipe_params(self, machine: str, recipe: str) -> T.Type:
        return T.record(
            {
                p: PARAM_TYPES.get(pt, T.DYN)
                for m, r, p, pt in self.x.facts.get("recipe_param", [])
                if (m, r) == (machine, recipe)
            }
        )

    def params(self, station: str) -> T.Type:
        fields = {
            p: PARAM_TYPES.get(pt, T.DYN)
            for key, p, pt, _d in self.x.facts.get("station_param", [])
            if key == station
        }
        return T.record(fields)

    def line_inputs(self, line: str) -> T.Type:
        return T.record({p: self.bean(t) for p, t in self.x.line_inputs.get(line, {}).items()})

    def source_type(self, line: str, src: tuple[str, str, str]) -> str | None:
        kind, step, port = src
        if kind == "input":
            return self.x.line_inputs.get(line, {}).get(port)
        if kind == "step":
            return self.step_out.get((line, step, port))
        return None

    def step_station(self, line: str, step: str) -> str | None:
        st = self.x.line_steps.get(line, {}).get(step) or {}
        ref = st.get("station")
        return self.resolved.get(("Station", ref)) if isinstance(ref, str) else None


def site_env(site: Site, env: TypeEnv) -> tuple[dict[str, T.Type], T.Type | None, str | None]:
    """返回（类型环境、期望类型、环境本身的问题）。期望类型为 None 表示模板。"""
    c = site.ctx
    if site.kind == "property":
        return {"bean": env.bean_of_key(c["type_key"])}, T.BOOL, None
    if site.kind == "accept":
        s = c["station"]
        out_t = env.x.station_ports.get(s, {}).get("outputs", {}).get(c["port"], "any")
        return (
            {"out": env.bean(out_t), "inputs": env.ports(s, "inputs"), "params": env.params(s)},
            T.BOOL,
            None,
        )
    if site.kind == "equivalence":
        s = c["station"]
        return {"src": env.bean(c["from"]), "dst": env.bean(c["to"]), "params": env.params(s)}, T.BOOL, None
    if site.kind == "approval":
        s = c["station"]
        root = {"propose": ("proposal", PROPOSAL), "in_run": ("request", REQUEST)}[c["phase"]]
        return {root[0]: root[1], "inputs": env.ports(s, "inputs"), "params": env.params(s)}, T.BOOL, None
    if site.kind == "idempotency":
        s = c["station"]
        return {"inputs": env.ports(s, "inputs"), "params": env.params(s), "run": RUN}, None, None
    if site.kind == "step-param":
        line, step = c["line"], c["step"]
        types: dict[str, T.Type] = {"run": RUN, "inputs": env.line_inputs(line)}
        m = (env.x.line_steps.get(line, {}).get(step) or {}).get("map")
        problem = None
        if isinstance(m, dict) and isinstance(m.get("as"), str):
            name = m["as"]
            over = m.get("over")
            if isinstance(over, list):
                t: T.Type | None = None
                for v in over:
                    vt = _literal_type(v)
                    t = vt if t is None else T.lub(t, vt)
                item_t = t or T.DYN
            else:
                from .extract import _source

                u = env.source_type(line, _source(over)) if isinstance(over, str) else None
                te = env.x.texprs.get(u) if u else None
                item_t = (
                    env.bean(te.arg.id)
                    if te is not None and te.form == "list" and te.arg is not None
                    else T.DYN
                )
            if name in types:
                problem = f"map 的 as 名字 {name!r} 与模板环境中的名字冲突"
            else:
                types[name] = item_t
        return types, None, problem
    if site.kind == "recipe-template":
        m, r = c["machine"], c["recipe"]
        return {"inputs": env.recipe_ports(m, r), "params": env.recipe_params(m, r)}, None, None
    if site.kind == "runtime-template":
        return dict(RUNTIME_ENV), None, None
    if site.kind == "switch":
        u = env.source_type(c["line"], tuple(c["on"]))
        return {"bean": env.bean(u) if u else ANY_BEAN}, T.BOOL, None
    if site.kind == "until":
        s = env.step_station(c["line"], c["step"])
        if s is None:
            return {"gate": gate_type(False), "out": T.MapT(ANY_BEAN)}, T.BOOL, None
        return {"gate": gate_type(s in env.x.station_judges), "out": env.ports(s, "outputs")}, T.BOOL, None
    raise ValueError(f"未知的表达式位置 {site.kind}")


def _fmt(d: Diagnostic) -> str:
    return f"{d.code}（第 {d.span.line} 行第 {d.span.col} 列）：{d.message}"


def check_sites(x: Extraction, model: Model) -> list[HostDiag]:
    env = TypeEnv(x, model)
    functions = make_table()
    out: list[HostDiag] = []
    seen: set[tuple[str, str, str]] = set()

    def emit(code: str, subject: str, message: str) -> None:
        if (code, subject, message) in seen:
            return
        seen.add((code, subject, message))
        file, doc, path = x.locs.get(subject, ("", -1, ""))
        out.append(HostDiag(code, subject, message, file, doc, path))

    for site in x.sites:
        types, expect, problem = site_env(site, env)
        if problem:
            emit("E-EXPR", site.subject, problem)
            continue
        try:
            if site.template:
                compiled: Any = compile_template(site.text, types, functions)
                paths: set[str] = set()
            else:
                compiled = compile_expr(site.text, types, functions, expect=expect)
                paths = compiled.paths
        except StaticError as e:
            for d in e.diagnostics:
                if d.severity == "error":
                    emit("E-EXPR", site.subject, _fmt(d))
            continue
        for w in compiled.warnings:
            emit("W-EXPR", site.subject, _fmt(w))
        if site.kind == "property" and any(p == "bean.meta" or p.startswith("bean.meta.") for p in paths):
            emit(
                "W-PROPERTY-META",
                site.subject,
                "命名属性引用了生产者自报的 bean.meta；属性应当建立在 validator 实测的 facts 上",
            )
    return out
