"""抽取：Spec 文档 → 关系事实（规范性映射，见 spec/mrl/factory-profile.md 第 3 章）。

只处理已经通过 schema 校验的文档。每个函数对应一种 kind；主体（subject）的写法必须与内核规则中
拼接主体的写法一致，例如 "Line:explainer-multilang@5/steps.dub_en.in.subtitle"。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..parser import is_template
from .registry import HostDiag, SpecDoc
from .texpr import TExpr, parse_texpr


@dataclass
class Site:
    """一处嵌入的 MRL 表达式或模板。"""

    kind: str  # property | accept | equivalence | approval | idempotency | step-param | switch | until
    #          | recipe-template | runtime-template
    subject: str
    text: str
    template: bool
    ctx: dict[str, Any] = field(default_factory=dict)


@dataclass
class Extraction:
    facts: dict[str, list[tuple]] = field(default_factory=lambda: defaultdict(list))
    diags: list[HostDiag] = field(default_factory=list)
    locs: dict[str, tuple[str, int, str]] = field(default_factory=dict)
    sites: list[Site] = field(default_factory=list)
    meta_schemas: dict[str, Any] = field(default_factory=dict)
    texprs: dict[str, TExpr] = field(default_factory=dict)
    station_params: dict[str, dict[str, str]] = field(default_factory=dict)
    station_ports: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    line_inputs: dict[str, dict[str, str]] = field(default_factory=dict)
    line_steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    station_judges: set[str] = field(default_factory=set)

    def add(self, rel: str, *row: Any) -> None:
        self.facts[rel].append(tuple(row))

    def loc(self, subject: str, doc: SpecDoc, path: str) -> None:
        self.locs.setdefault(subject, (doc.file, doc.index, path))

    def ref(self, subject: str, kind: str, target: str, doc: SpecDoc, path: str) -> None:
        self.add("ref", subject, kind, target)
        self.loc(subject, doc, path)

    def texpr(self, text: str, subject: str, doc: SpecDoc, path: str) -> str | None:
        """解析并登记类型表达式（含子表达式），为其中的命名类型登记引用；失败时报 E-SCHEMA。"""
        try:
            te = parse_texpr(text)
        except ValueError as e:
            self.diags.append(HostDiag("E-SCHEMA", subject, str(e), doc.file, doc.index, path))
            return None
        self.register(te)
        for sub in te.walk():
            if sub.form == "named":
                self.ref(subject, "Type", sub.raw_ref, doc, path)
        return te.id

    def register(self, te: TExpr) -> None:
        for sub in te.walk():
            if sub.id in self.texprs:
                continue
            self.texprs[sub.id] = sub
            if sub.form == "named":
                self.add("texpr", sub.id, "named", sub.type_id)
            elif sub.form == "any":
                self.add("texpr", "any", "any", "")
            else:
                assert sub.arg is not None
                self.add("texpr", sub.id, sub.form, sub.arg.id)


def _str_version(v: Any) -> str:
    return str(v)


def extract(docs: list[SpecDoc], extra_texprs: list[str] | None = None) -> Extraction:
    x = Extraction()
    x.register(parse_texpr("any"))
    for te in extra_texprs or []:
        try:
            x.register(parse_texpr(te))
        except ValueError as e:
            x.diags.append(HostDiag("E-SCHEMA", f"Arg:{te}", str(e)))
    for doc in docs:
        d = doc.data
        kind = d["kind"]
        meta = d["metadata"]
        key = doc.key
        assert key is not None
        x.add("spec", key, kind, meta["name"], _str_version(meta["version"]), meta.get("lifecycle", "active"))
        x.add("spec_doc", key, doc.file, doc.index)
        x.loc(key, doc, "")
        spec = d.get("spec") or {}
        handler = HANDLERS.get(kind)
        if handler:
            handler(x, doc, key, meta, spec)
    return x


# ---------- Type ----------


def _type(x: Extraction, doc: SpecDoc, key: str, meta: dict, spec: dict) -> None:
    major_s, minor_s = str(meta["version"]).split(".")
    tid = f"{meta['name']}@{int(major_s)}"
    x.add("type_version", key, tid, int(minor_s), meta.get("lifecycle", "active"))
    x.add("type_kind", key, spec.get("kind", "atomic"))
    if "extends" in spec:
        subject = key + "/extends"
        parent = x.texpr(spec["extends"], subject, doc, "spec.extends")
        if parent is not None:
            pte = x.texprs[parent]
            if pte.form == "named":
                x.add("type_extends_decl", key, pte.type_id)
    for f, te in (spec.get("fields") or {}).items():
        subject = f"{key}/fields.{f}"
        x.loc(subject, doc, f"spec.fields.{f}")
        tid_f = x.texpr(te, subject, doc, f"spec.fields.{f}")
        if tid_f is not None:
            x.add("type_field", key, f, tid_f)
    for field_name, rel in (("item", "type_item"), ("target", "type_target")):
        if field_name in spec:
            t = x.texpr(spec[field_name], f"{key}/{field_name}", doc, f"spec.{field_name}")
            if t is not None:
                x.add(rel, key, t)
    for f, ftype in (spec.get("facts") or {}).items():
        x.add("type_fact_decl", key, f, ftype)
        x.loc(f"{key}/facts.{f}", doc, f"spec.facts.{f}")
    for i, v in enumerate(spec.get("validators") or []):
        name = v["name"]
        x.add("type_validator", key, name, v["image"])
        x.loc(f"{key}/validators.{name}", doc, f"spec.validators[{i}]")
        for fact in v.get("emits") or []:
            x.add("validator_emits", key, name, fact)
        fixtures = v.get("fixtures") or {}
        for polarity in ("accept", "reject"):
            for path in fixtures.get(polarity) or []:
                x.add("validator_fixture", key, name, polarity, path)
    for p, expr in (spec.get("properties") or {}).items():
        subject = f"{key}/properties.{p}"
        x.add("type_property", key, p)
        x.loc(subject, doc, f"spec.properties.{p}")
        x.sites.append(Site("property", subject, expr, False, {"type_id": tid, "type_key": key}))
    for p in spec.get("always") or []:
        x.add("type_always", key, p)
    x.loc(f"{key}/always", doc, "spec.always")
    if "meta_schema" in spec:
        x.meta_schemas[key] = spec["meta_schema"]


# ---------- Machine ----------


def _machine(x: Extraction, doc: SpecDoc, key: str, meta: dict, spec: dict) -> None:
    x.add("machine", key, spec["form"])
    rt = spec.get("runtime") or {}
    if "image" in rt:
        x.add("machine_image", key, rt["image"])
        x.loc(key, doc, "spec.runtime.image")
    src = spec.get("source")
    if src:
        x.add("machine_source", key, src["repo"], src["commit"])
        x.loc(f"{key}/source", doc, "spec.source")
    for rname, r in (spec.get("recipes") or {}).items():
        x.add("recipe", key, rname)
        base = f"spec.recipes.{rname}"
        for port, p in (r.get("inputs") or {}).items():
            t = x.texpr(p["type"], f"{key}/recipes.{rname}.inputs.{port}", doc, f"{base}.inputs.{port}.type")
            if t is not None:
                x.add("recipe_in", key, rname, port, t, bool(p.get("optional", False)))
        for port, p in (r.get("outputs") or {}).items():
            t = x.texpr(
                p["type"], f"{key}/recipes.{rname}.outputs.{port}", doc, f"{base}.outputs.{port}.type"
            )
            if t is not None:
                x.add("recipe_out", key, rname, port, t, bool(p.get("optional", False)))
        for pname, pdecl in (r.get("params") or {}).items():
            x.add("recipe_param", key, rname, pname, pdecl["type"])
        ctx = {"machine": key, "recipe": rname}
        for path, text in _templates(r.get("task_template"), "task_template"):
            _template_site(x, doc, "recipe-template", key, f"recipes.{rname}.{path}", text, ctx)
        for path, text in _templates(r.get("call"), "call"):
            _template_site(x, doc, "recipe-template", key, f"recipes.{rname}.{path}", text, ctx)
        for path, text in _templates(r.get("run"), "run"):
            _template_site(x, doc, "recipe-template", key, f"recipes.{rname}.{path}", text, ctx)
        for port, p in (r.get("outputs") or {}).items():
            for path, text in _templates(p.get("harvest"), f"outputs.{port}.harvest"):
                _template_site(x, doc, "recipe-template", key, f"recipes.{rname}.{path}", text, ctx)


# ---------- OperatorRuntime ----------


def _runtime(x: Extraction, doc: SpecDoc, key: str, meta: dict, spec: dict) -> None:
    x.add("runtime", key, spec["kind"])
    for m in spec.get("models_allowed") or []:
        x.add("runtime_model", key, m)
    vol = (spec.get("inject") or {}).get("volume")
    if vol:
        x.add("runtime_volume", key, vol)
    for path, text in _templates((spec.get("invoke") or {}).get("argv"), "invoke.argv"):
        _template_site(x, doc, "runtime-template", key, path, text, {"runtime": key})
    for path, text in _templates(spec.get("env"), "env"):
        _template_site(x, doc, "runtime-template", key, path, text, {"runtime": key})


def _templates(value: Any, path: str) -> list[tuple[str, str]]:
    """value 中所有含 {{ }} 的字符串，连同它们的路径（递归进入 list 与 map）。"""
    if isinstance(value, str):
        return [(path, value)] if is_template(value) else []
    if isinstance(value, list):
        return [t for i, v in enumerate(value) for t in _templates(v, f"{path}[{i}]")]
    if isinstance(value, dict):
        return [t for k, v in value.items() for t in _templates(v, f"{path}.{k}")]
    return []


def _template_site(x: Extraction, doc: SpecDoc, kind: str, key: str, path: str, text: str, ctx: dict) -> None:
    subject = f"{key}/{path}"
    x.loc(subject, doc, f"spec.{path}")
    x.sites.append(Site(kind, subject, text, True, ctx))


# ---------- Station ----------


def _approval_mode(rule: Any) -> str:
    return "conditional" if isinstance(rule, dict) else str(rule)


def _station(x: Extraction, doc: SpecDoc, key: str, meta: dict, spec: dict) -> None:
    role = spec["role"]
    x.add("station", key, role)
    x.add("station_machine", key, spec["machine"], spec["recipe"])
    x.ref(f"{key}/machine", "Machine", spec["machine"], doc, "spec.machine")
    x.loc(f"{key}/recipe", doc, "spec.recipe")
    op = spec["operator"]
    x.add("station_operator", key, op["runtime"], op.get("model", ""))
    x.ref(f"{key}/operator", "OperatorRuntime", op["runtime"], doc, "spec.operator")
    x.add("station_policy", key, spec["policy"])
    x.ref(f"{key}/policy", "Policy", spec["policy"], doc, "spec.policy")
    x.loc(f"{key}/limits", doc, "spec.limits")

    params: dict[str, str] = {}
    for pname, pdecl in (spec.get("params") or {}).items():
        x.add("station_param", key, pname, pdecl["type"], "default" in pdecl)
        x.loc(f"{key}/params.{pname}", doc, f"spec.params.{pname}")
        params[pname] = pdecl["type"]
    x.station_params[key] = params

    ports: dict[str, dict[str, str]] = {"inputs": {}, "outputs": {}}
    x.loc(f"{key}/inputs", doc, "spec.inputs")
    x.loc(f"{key}/outputs", doc, "spec.outputs")
    for port, p in (spec.get("inputs") or {}).items():
        subject = f"{key}/inputs.{port}"
        x.loc(subject, doc, f"spec.inputs.{port}")
        t = x.texpr(p["type"], subject, doc, f"spec.inputs.{port}.type")
        if t is None:
            continue
        reqs = tuple(p.get("requires") or [])
        x.add("in_port", key, port, t, bool(p.get("optional", False)), reqs)
        for r in reqs:
            x.add("in_require", key, port, r)
        ports["inputs"][port] = t
    for port, p in (spec.get("outputs") or {}).items():
        subject = f"{key}/outputs.{port}"
        x.loc(subject, doc, f"spec.outputs.{port}")
        t = x.texpr(p["type"], subject, doc, f"spec.outputs.{port}.type")
        if t is None:
            continue
        gs = tuple(p.get("guarantees") or [])
        x.add("out_port", key, port, t, bool(p.get("optional", False)), gs)
        for g in gs:
            x.add("out_guarantee", key, port, g)
        ports["outputs"][port] = t
        for i, expr in enumerate(p.get("accept") or []):
            s = f"{subject}.accept[{i}]"
            x.loc(s, doc, f"spec.outputs.{port}.accept[{i}]")
            x.sites.append(Site("accept", s, expr, False, {"station": key, "port": port}))
    x.station_ports[key] = ports
    for t in ports["outputs"].values():
        x.register(parse_texpr(f"list<{t}>"))  # 带 map 的步骤把输出聚合为 list<t>（见 kernel/lines.mrl）

    limits = spec.get("limits") or {}
    if "budget_usd" in limits:
        x.add("station_budget", key, float(limits["budget_usd"]))
    x.add("station_phases", key, tuple(spec.get("phases") or ["execute"]))

    conv = spec.get("converts")
    if conv is not None:
        subject = f"{key}/converts"
        x.loc(subject, doc, "spec.converts")
        f = x.texpr(conv["from"], subject, doc, "spec.converts.from")
        t = x.texpr(conv["to"], subject, doc, "spec.converts.to")
        if f is not None and t is not None:
            pres = tuple(conv.get("preserves") or [])
            x.add("converts", key, f, t, bool(conv["lossless"]), pres)
            for p in pres:
                x.add("conv_preserve", key, p)
            cost = spec.get("cost") or {}
            x.add("conv_cost", key, float(cost.get("usd_est", 0.0)), float(cost.get("p50_s", 0.0)))
            for i, expr in enumerate(spec.get("equivalence") or []):
                s = f"{key}/equivalence[{i}]"
                x.loc(s, doc, f"spec.equivalence[{i}]")
                x.sites.append(Site("equivalence", s, expr, False, {"station": key, "from": f, "to": t}))
    x.add("equivalence_count", key, len(spec.get("equivalence") or []))
    x.loc(f"{key}/equivalence", doc, "spec.equivalence")
    x.loc(f"{key}/fixtures", doc, "spec.fixtures")
    for polarity in ("accept", "reject"):
        for path in (spec.get("fixtures") or {}).get(polarity) or []:
            x.add("station_fixture", key, polarity, path)

    if "idempotency_key" in spec:
        x.add("sink_idempotency", key, spec["idempotency_key"])
        s = f"{key}/idempotency_key"
        x.loc(s, doc, "spec.idempotency_key")
        x.sites.append(Site("idempotency", s, spec["idempotency_key"], True, {"station": key}))
    if "verify_receipt" in spec:
        x.add("sink_verify", key, spec["verify_receipt"])

    for phase in ("propose", "in_run"):
        rule = (spec.get("approval") or {}).get(phase)
        if rule is None:
            continue
        x.add("approval", key, phase, _approval_mode(rule))
        if isinstance(rule, dict):
            s = f"{key}/approval.{phase}"
            x.loc(s, doc, f"spec.approval.{phase}.auto_if")
            x.sites.append(Site("approval", s, rule["auto_if"], False, {"phase": phase, "station": key}))

    judge = (spec.get("gate") or {}).get("judge")
    if judge:
        x.station_judges.add(key)
        x.ref(f"{key}/gate.judge", "OperatorRuntime", judge["runtime"], doc, "spec.gate.judge.runtime")
    bounds = spec.get("plan_bounds") or {}
    for field_name in ("allowed_stations", "allowed_sinks"):
        for i, r in enumerate(bounds.get(field_name) or []):
            x.ref(
                f"{key}/plan_bounds.{field_name}[{i}]",
                "Station",
                r,
                doc,
                f"spec.plan_bounds.{field_name}[{i}]",
            )


# ---------- Line ----------


def _value_kind(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    return "string"


def _source(s: str) -> tuple[str, str, str]:
    if s == "$item":
        return ("item", "", "")
    if s.startswith("$inputs."):
        return ("input", "", s[len("$inputs.") :])
    step, port = s.split(".", 1)
    return ("step", step, port)


def _line(x: Extraction, doc: SpecDoc, key: str, meta: dict, spec: dict) -> None:
    x.add("line", key)
    iface = spec.get("interface") or {}
    inputs: dict[str, str] = {}
    for port, p in (iface.get("inputs") or {}).items():
        subject = f"{key}/interface.inputs.{port}"
        x.loc(subject, doc, f"spec.interface.inputs.{port}")
        t = x.texpr(p["type"], subject, doc, f"spec.interface.inputs.{port}.type")
        if t is not None:
            reqs = tuple(p.get("requires") or [])
            x.add("line_input", key, port, t, reqs)
            for r in reqs:
                x.add("line_input_require", key, port, r)
            inputs[port] = t
    x.line_inputs[key] = inputs
    for port, p in (iface.get("outputs") or {}).items():
        subject = f"{key}/interface.outputs.{port}"
        x.loc(subject, doc, f"spec.interface.outputs.{port}")
        step, src_port = p["from"].split(".", 1)
        x.add("line_output", key, port, step, src_port)
        if "type" in p:
            t = x.texpr(p["type"], subject, doc, f"spec.interface.outputs.{port}.type")
            if t is not None:
                x.add("line_output_type", key, port, t)
    if "budget" in spec:
        x.add("line_budget", key, float(spec["budget"]["cap_usd"]))
    x.loc(f"{key}/budget", doc, "spec.budget")
    for i, trig in enumerate(spec.get("trigger") or []):
        if "type" in trig:
            x.texpr(trig["type"], f"{key}/trigger[{i}]", doc, f"spec.trigger[{i}].type")

    steps = spec.get("steps") or {}
    x.line_steps[key] = steps
    for sid, st in steps.items():
        base = f"spec.steps.{sid}"
        subject = f"{key}/steps.{sid}"
        x.loc(subject, doc, base)
        if "station" in st:
            kind, target = "station", st["station"]
            x.ref(subject, "Station", target, doc, f"{base}.station")
        elif "subline" in st:
            kind, target = "subline", st["subline"]
            x.ref(subject, "Line", target, doc, f"{base}.subline")
        else:
            kind, target = "switch", ""
        x.add("step", key, sid, kind, target)
        x.loc(f"{subject}.in", doc, f"{base}.in")
        for port, b in (st.get("in") or {}).items():
            bsubject = f"{subject}.in.{port}"
            x.loc(bsubject, doc, f"{base}.in.{port}")
            src = b if isinstance(b, str) else b["from"]
            src_kind, src_step, src_port = _source(src)
            x.add("bind", key, sid, port, src_kind, src_step, src_port)
            via = None if isinstance(b, str) else b.get("via")
            if via is None:
                x.add("bind_via", key, sid, port, "auto", ())
            elif via == "none":
                x.add("bind_via", key, sid, port, "none", ())
            else:
                x.add("bind_via", key, sid, port, "explicit", tuple(via))
                for i, c in enumerate(via):
                    x.ref(f"{bsubject}.via[{i}]", "Station", c, doc, f"{base}.in.{port}.via[{i}]")
        x.loc(f"{subject}.params", doc, f"{base}.params")
        for pname, value in (st.get("params") or {}).items():
            psubject = f"{subject}.params.{pname}"
            x.loc(psubject, doc, f"{base}.params.{pname}")
            if isinstance(value, str) and is_template(value):
                x.add("step_param", key, sid, pname, "template")
                x.sites.append(
                    Site("step-param", psubject, value, True, {"line": key, "step": sid, "param": pname})
                )
            else:
                x.add("step_param", key, sid, pname, _value_kind(value))
        if "map" in st:
            m = st["map"]
            x.loc(f"{subject}.map", doc, f"{base}.map")
            over = m["over"]
            if isinstance(over, list):
                items = len(over)
            else:
                items = int(m.get("max_items", -1))
                x.add("map_over", key, sid, *_source(over))
            x.add("step_map", key, sid, items, int(m.get("max_parallel", 1)))
        if "until" in st:
            u = st["until"]
            x.add("step_until", key, sid, int(u["max_iter"]))
            s = f"{subject}.until"
            x.loc(s, doc, f"{base}.until.cond")
            x.sites.append(Site("until", s, u["cond"], False, {"line": key, "step": sid}))
        if "switch" in st:
            sw = st["switch"]
            x.loc(f"{subject}.switch", doc, f"{base}.switch")
            on = _source(sw["on"])
            x.add("switch_on", key, sid, *on)
            for i, case in enumerate(sw["cases"]):
                x.add("switch_target", key, sid, case["goto"])
                s = f"{subject}.switch.cases[{i}]"
                x.loc(s, doc, f"{base}.switch.cases[{i}].when")
                x.sites.append(Site("switch", s, case["when"], False, {"line": key, "on": on}))
            x.add("switch_target", key, sid, sw["default"])
    x.loc(f"{key}/keep", doc, "spec.keep")
    for item in spec.get("keep") or []:
        step, port = item.split(".", 1)
        x.add("keep", key, step, port)


# ---------- Policy ----------


def _policy(x: Extraction, doc: SpecDoc, key: str, meta: dict, spec: dict) -> None:
    x.add("policy", key)
    for r in spec["applies_to"]:
        x.add("policy_applies", key, r)
    for tier in spec["egress"]:
        x.add("policy_egress", key, tier)
    secrets = spec.get("secrets")
    if isinstance(secrets, list):
        for s in secrets:
            x.add("policy_secret", key, s)


# ---------- GoldenSet / Baseline ----------


def _station_ref_only(x: Extraction, doc: SpecDoc, key: str, meta: dict, spec: dict) -> None:
    x.ref(f"{key}/station", "Station", spec["station"], doc, "spec.station")


HANDLERS = {
    "Type": _type,
    "Machine": _machine,
    "OperatorRuntime": _runtime,
    "Station": _station,
    "Line": _line,
    "Policy": _policy,
    "GoldenSet": _station_ref_only,
    "Baseline": _station_ref_only,
}
