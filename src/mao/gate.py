"""来料检验与出厂检验。"""

from __future__ import annotations

import fnmatch
import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from mao.base import canonical_bytes, digest_bytes
from mao.errors import FactoryError
from mao.sandbox import Mount, SandboxSpec
from mao.values import accept_env, params_value, property_env
from mao.warehouse import Bean, BeanDraft
from mrl.errors import EvalError
from mrl.factory.sites import TypeEnv

_STAGES = ("structure", "validator", "property", "accept", "consistency")


@dataclass(frozen=True)
class Check:
    port: str
    index: int
    stage: str
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class GateReport:
    job_id: str
    station: str
    attempt: int
    verdict: str
    outputs: dict[str, list[str]]
    facts: dict[str, dict[str, Any]]
    checks: tuple[Check, ...]
    feedback: str

    def to_json(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "station": self.station,
            "attempt": self.attempt,
            "verdict": self.verdict,
            "outputs": {port: list(ids) for port, ids in self.outputs.items()},
            "facts": {bean_id: dict(facts) for bean_id, facts in self.facts.items()},
            "checks": [
                {
                    "port": item.port,
                    "index": item.index,
                    "stage": item.stage,
                    "name": item.name,
                    "ok": item.ok,
                    "detail": item.detail,
                }
                for item in self.checks
            ],
            "feedback": self.feedback,
        }

    def digest(self) -> str:
        return digest_bytes(canonical_bytes(self.to_json()))


class Gate:
    def __init__(
        self, snapshot: Any, warehouse: Any, provider: Any, clock: Any, ids: Any, *, workdir: Path
    ) -> None:
        self.snapshot = snapshot
        self.warehouse = warehouse
        self.provider = provider
        self.clock = clock
        self.ids = ids
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)

    def check_inputs(self, station_key: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
        station = self.snapshot.key("Station", station_key)
        declared = self.snapshot.spec(station).get("inputs") or {}
        problems: list[dict[str, Any]] = []
        result: dict[str, Any] = {}
        for port, decl in declared.items():
            type_expr, requires, optional = _port_decl(decl)
            if port not in inputs:
                if not optional:
                    problems.append(_problem(port, 0, "missing", "缺少必填端口"))
                continue
            kind, tid = _parse_type(type_expr, output=False)
            value = inputs[port]
            if kind == "list":
                if isinstance(value, str) or not isinstance(value, (list, tuple)):
                    problems.append(_problem(port, 0, "cardinality", "列表端口需要 id 序列"))
                    continue
                beans = []
                for index, bean_id in enumerate(value):
                    bean, issue = self._intake_one(port, index, str(bean_id), tid, requires)
                    if issue:
                        problems.append(issue)
                    elif bean is not None:
                        beans.append(bean)
                if not any(
                    item["port"] == port and item["reason"] != "missing" for item in problems if False
                ):
                    result[port] = beans
                if not any(item["port"] == port for item in problems):
                    result[port] = beans
            else:
                if isinstance(value, (list, tuple)):
                    problems.append(_problem(port, 0, "cardinality", "标量端口不能给列表"))
                    continue
                bean, issue = self._intake_one(port, 0, str(value), tid, requires)
                if issue:
                    problems.append(issue)
                elif bean is not None:
                    result[port] = bean
        for port in inputs:
            if port not in declared:
                problems.append(_problem(port, 0, "extra", "工位没有这个输入端口"))
        if problems:
            raise FactoryError("INTAKE_REJECTED", "来料检验未通过", problems=problems)
        return result

    def inspect(
        self,
        station_key: str,
        *,
        job_id: str,
        attempt: int,
        out_dir: Path,
        inputs: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> GateReport:
        station = self.snapshot.key("Station", station_key)
        normalized = params_value(self.snapshot, station, params)
        outputs = self.snapshot.spec(station).get("outputs") or {}
        if not outputs:
            return _report(job_id, station, attempt, "pass", {}, {}, (), "")
        checks: list[Check] = []
        produced: dict[str, list[str]] = {}
        facts: dict[str, dict[str, Any]] = {}
        registered: dict[str, list[dict[str, Any]]] = {}
        for port in sorted(outputs):
            self._inspect_port(
                station,
                port,
                outputs[port],
                Path(out_dir),
                job_id,
                attempt,
                inputs,
                normalized,
                checks,
                produced,
                facts,
                registered,
            )
        ordered = tuple(sorted(checks, key=_check_key))
        verdict = _verdict(ordered, produced, outputs)
        feedback = "" if verdict == "pass" else _feedback(ordered)
        return _report(
            job_id, station, attempt, verdict, produced, dict(sorted(facts.items())), ordered, feedback
        )

    def admit_external(
        self, type_id: str, files: Mapping[str, Path], meta: Mapping[str, Any]
    ) -> tuple[GateReport, Any]:
        tid = _type_id(type_id)
        type_name = self.snapshot.type_key(tid).split(":", 1)[1]
        job_id = self.ids.new("j")
        checks: list[Check] = []
        root = self.workdir / f"admit-{job_id}"
        root.mkdir(parents=True, exist_ok=True)
        for rel, src in files.items():
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        if meta:
            (root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        bean, ok = self._register_candidate("", 0, root, tid, type_name, checks)
        facts: dict[str, dict[str, Any]] = {}
        outputs: dict[str, list[str]] = {}
        if bean is not None and ok:
            outputs[""] = [bean.id]
            facts[bean.id] = {}
            self._validate_candidate("", 0, bean, tid, [], job_id, 0, checks, facts, {}, {}, set())
            self._consistency("", 0, bean, tid, meta, facts.get(bean.id, {}), checks)
        ordered = tuple(sorted(checks, key=_check_key))
        verdict = _verdict(ordered, outputs, {"": {}})
        feedback = "" if verdict == "pass" else _feedback(ordered)
        report = _report(job_id, "", 0, verdict, outputs, facts, ordered, feedback)
        if verdict == "pass" and bean is not None:
            return report, self.warehouse.get(bean.id)
        return report, None

    def _intake_one(
        self, port: str, index: int, bean_id: str, tid: str, requires: list[str]
    ) -> tuple[Bean | None, dict[str, Any] | None]:
        try:
            bean = self.warehouse.get(bean_id)
        except FactoryError as exc:
            if exc.code == "BEAN_NOT_FOUND":
                return None, _problem(port, index, "not_found", "Bean 不存在", bean_id)
            raise
        if not bean.sealed:
            return None, _problem(port, index, "unsealed", "Bean 未封存", bean.id)
        bean_tid = _type_id(bean.type)
        if tid != "any":
            try:
                self.snapshot.type_key(bean_tid)
            except FactoryError as exc:
                if exc.code == "TYPE_NOT_FOUND":
                    return None, _problem(port, index, "type", "类型不在快照里", bean.id)
                raise
            if not self.snapshot.is_subtype(bean_tid, tid):
                return None, _problem(port, index, "type", "类型不兼容", bean.id)
        props = {}
        try:
            props = self.snapshot.properties(bean_tid)
        except FactoryError as exc:
            if exc.code == "TYPE_NOT_FOUND":
                return None, _problem(port, index, "type", "类型不在快照里", bean.id)
            raise
        for name in requires:
            compiled = props.get(name)
            if compiled is None:
                return None, _problem(port, index, "requires", name, bean.id)
            try:
                ok = compiled.evaluate(property_env(bean)) is True
            except EvalError:
                ok = False
            if not ok:
                return None, _problem(port, index, "requires", name, bean.id)
        return bean, None

    def _inspect_port(
        self,
        station,
        port,
        decl,
        out_dir,
        job_id,
        attempt,
        inputs,
        params,
        checks,
        produced,
        facts,
        registered,
    ) -> None:
        type_expr, _requires, optional = _port_decl(decl)
        guarantees = list(decl.get("guarantees") or []) if isinstance(decl, dict) else []
        kind, tid = _parse_type(type_expr, output=True)
        type_name = self.snapshot.type_key(tid).split(":", 1)[1]
        port_dir = out_dir / port
        if not port_dir.exists():
            if optional:
                return
            checks.append(Check(port, 0, "structure", "payload", False, "缺少候选目录"))
            return
        if kind == "list":
            children = [path for path in port_dir.iterdir()]
            names = [path.name for path in children if path.is_dir()]
            if any(path.is_file() for path in children) or any(not _index_name(name) for name in names):
                checks.append(Check(port, 0, "structure", "index", False, "列表下标不合法"))
                return
            indexes = sorted(int(name) for name in names)
            if indexes != list(range(len(indexes))):
                checks.append(Check(port, 0, "structure", "index", False, "列表下标不连续"))
                return
            roots = [(index, port_dir / str(index)) for index in indexes]
        else:
            roots = [(0, port_dir)]
        port_items = []
        for index, root in roots:
            bean, ok = self._register_candidate(port, index, root, tid, type_name, checks)
            if bean is None or not ok:
                continue
            produced.setdefault(port, []).append(bean.id)
            facts[bean.id] = {}
            port_items.append({"index": index, "bean": bean, "stopped": False, "infra": False})
        registered[port] = port_items
        for item in port_items:
            stopped, infra = self._validate_candidate(
                port,
                item["index"],
                item["bean"],
                tid,
                guarantees,
                job_id,
                attempt,
                checks,
                facts,
                inputs,
                params,
                set(guarantees),
            )
            item["stopped"] = stopped
            item["infra"] = infra
        ready = port_items and all(not item["stopped"] and not item["infra"] for item in port_items)
        if ready:
            accepts = (decl.get("accept") or []) if isinstance(decl, dict) else []
            temps = [_with_facts(item["bean"], facts[item["bean"].id]) for item in port_items]
            out = temps if kind == "list" else temps[0]
            for i, _text in enumerate(accepts):
                subject = f"{station}/outputs.{port}.accept[{i}]"
                try:
                    ok = self.snapshot.compiled(subject).evaluate(accept_env(out, inputs, params)) is True
                    detail = "" if ok else f"accept[{i}]"
                except EvalError:
                    ok = False
                    detail = f"accept[{i}]"
                checks.append(Check(port, -1, "accept", f"accept[{i}]", ok, detail))
        for item in port_items:
            if item["infra"] or item["stopped"] and not item["infra"]:
                # consistency still runs when the candidate passed the first three stages.
                passed_early = not any(
                    check.port == port
                    and check.index == item["index"]
                    and check.stage in {"structure", "validator", "property"}
                    and not check.ok
                    for check in checks
                )
                if not passed_early:
                    continue
            self._consistency(
                port,
                item["index"],
                item["bean"],
                tid,
                item["bean"].meta,
                facts.get(item["bean"].id, {}),
                checks,
            )

    def _register_candidate(self, port, index, root, tid, type_name, checks) -> tuple[Bean | None, bool]:
        rules = self.snapshot.spec(self.snapshot.type_key(tid)).get("payload") or []
        meta: dict[str, Any] = {}
        meta_path = root / "meta.json"
        files: dict[str, Path] = {}
        if meta_path.exists():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                checks.append(Check(port, index, "structure", "meta", False, "meta.json 不是对象"))
                return None, False
            if not isinstance(loaded, dict):
                checks.append(Check(port, index, "structure", "meta", False, "meta.json 不是对象"))
                return None, False
            meta = loaded
        actual = [
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path.name != "meta.json"
        ]
        matched = {rel: [] for rel in actual}
        for rule in rules:
            rule_path = str(rule.get("path", ""))
            hits = [rel for rel in actual if _path_match(rel, rule_path)]
            for rel in hits:
                matched[rel].append(rule)
            if rule.get("required", True) and not hits:
                checks.append(Check(port, index, "structure", "payload", False, f"缺少 {rule_path}"))
                return None, False
        for rel, rules_hit in matched.items():
            if len(rules_hit) != 1:
                checks.append(Check(port, index, "structure", "payload", False, f"文件不匹配规则 {rel}"))
                return None, False
            rule = rules_hit[0]
            recorded = self.warehouse.media_type_of(rel) if hasattr(self.warehouse, "media_type_of") else None
            from mao.warehouse import media_type_of

            recorded = media_type_of(rel)
            expected = rule.get("media_type")
            if expected and not fnmatch.fnmatchcase(recorded, expected):
                checks.append(Check(port, index, "structure", "payload", False, f"media_type 不匹配 {rel}"))
                return None, False
            files[rel] = root / rel
        bean = self.warehouse.add(BeanDraft(type=type_name, meta=meta, files=files))
        checks.append(Check(port, index, "structure", "payload", True, ""))
        return bean, True

    def _validate_candidate(
        self, port, index, bean, tid, guarantees, job_id, attempt, checks, facts, inputs, params, _g
    ) -> tuple[bool, bool]:
        merged: dict[str, Any] = {}
        for validator in self.snapshot.validators(tid):
            ok, detail, emitted, infra = self._run_validator(
                port, index, bean, tid, validator, job_id, attempt
            )
            checks.append(Check(port, index, "validator", str(validator.get("name")), ok, detail))
            if not ok:
                return True, infra
            for key, value in emitted.items():
                if key in merged and merged[key] != value:
                    checks.append(
                        Check(
                            port,
                            index,
                            "validator",
                            str(validator.get("name")),
                            False,
                            "设施故障：emits 冲突",
                        )
                    )
                    return True, True
                merged[key] = value
        facts[bean.id] = merged
        temp = _with_facts(bean, merged)
        names = sorted(set(self.snapshot.always(tid)) | set(guarantees))
        props = self.snapshot.properties(tid)
        for name in names:
            compiled = props.get(name)
            try:
                ok = compiled is not None and compiled.evaluate(property_env(temp)) is True
            except EvalError:
                ok = False
            detail = "" if ok else name
            checks.append(Check(port, index, "property", name, ok, detail))
            if not ok:
                return True, False
        return False, False

    def _run_validator(
        self, port, index, bean, tid, validator, job_id, attempt
    ) -> tuple[bool, str, dict[str, Any], bool]:
        name = str(validator.get("name"))
        raw = f"{job_id}-{attempt}-{port}-{index}-{name}"
        suffix = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in raw)
        sandbox_name = "mao-gate-" + suffix
        host = self.workdir / sandbox_name
        if host.exists():
            shutil.rmtree(host)
        payload = host / "payload"
        for item in bean.payload:
            dest = payload / item.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.warehouse.blob_path(item.digest), dest)
        body = canonical_bytes(
            {
                "id": bean.id,
                "type": bean.type,
                "facts": {},
                "meta": bean.meta,
                "payload": [
                    {
                        "path": item.path,
                        "digest": item.digest,
                        "size": item.size,
                        "media_type": item.media_type,
                    }
                    for item in bean.payload
                ],
            }
        )
        (host / "bean.json").write_bytes(body)
        spec = SandboxSpec(
            name=sandbox_name,
            image=str(validator.get("image")),
            mounts=(Mount(source=str(host.resolve()), target="/bean", readonly=True),),
            network="none",
        )
        stdout = self.workdir / f"{sandbox_name}.out"
        stderr = self.workdir / f"{sandbox_name}.err"
        sandbox = None
        try:
            sandbox = self.provider.open(spec)
            result = sandbox.exec(
                list(validator.get("run") or []), timeout_s=60, stdout=stdout, stderr=stderr
            )
        except Exception as exc:
            return False, f"设施故障：{type(exc).__name__}", {}, True
        finally:
            if sandbox is not None:
                sandbox.close()
        if result.timed_out or result.exit_code is None:
            return False, "设施故障：超时", {}, True
        text = stdout.read_text(encoding="utf-8", errors="replace").strip() if stdout.exists() else ""
        if result.exit_code not in (0, 1):
            return False, f"设施故障：退出码 {result.exit_code}", {}, True
        if result.exit_code == 1:
            reason = text[:500]
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and isinstance(parsed.get("reason"), str):
                    reason = parsed["reason"]
            except json.JSONDecodeError:
                pass
            return False, "拒绝：" + reason, {}, False
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return False, "设施故障：stdout 不是 JSON", {}, True
        if not isinstance(parsed, dict):
            return False, "设施故障：stdout 不是 JSON 对象", {}, True
        emits = list(validator.get("emits") or [])
        if set(parsed) != set(emits):
            return False, "设施故障：facts 形状不对", {}, True
        declared = self.snapshot.facts_decl(tid)
        for key in emits:
            if not _fact_type_ok(declared.get(key, ""), parsed[key]):
                return False, "设施故障：facts 类型不对", {}, True
        return True, "", parsed, False

    def _consistency(self, port, index, bean, tid, meta, observed, checks) -> None:
        schema = _meta_schema(self.snapshot, tid)
        detail = ""
        ok = True
        if schema:
            errors = list(Draft202012Validator(schema).iter_errors(meta))
            if errors:
                ok = False
                detail = errors[0].message
        if ok:
            for key, value in meta.items():
                if key in observed and not _same_value(value, observed[key]):
                    ok = False
                    detail = key
                    break
        checks.append(Check(port, index, "consistency", "meta_schema", ok, detail))


def _port_decl(decl: Any) -> tuple[str, list[str], bool]:
    if isinstance(decl, str):
        return decl, [], False
    return str(decl.get("type")), list(decl.get("requires") or []), bool(decl.get("optional"))


def _parse_type(expr: str, *, output: bool) -> tuple[str, str]:
    if expr == "any":
        if output:
            raise FactoryError("GATE_UNSUPPORTED", "M1 不检验这种类型表达式", type=expr)
        return "scalar", "any"
    if expr.startswith("ref<") or "bundle" in expr:
        raise FactoryError("GATE_UNSUPPORTED", "M1 不检验这种类型表达式", type=expr)
    if expr.startswith("list<") and expr.endswith(">"):
        inner = expr[5:-1]
        if "<" in inner:
            raise FactoryError("GATE_UNSUPPORTED", "M1 不检验这种类型表达式", type=expr)
        return "list", _type_id(inner)
    return "scalar", _type_id(expr)


def _type_id(type_name: str) -> str:
    if "@" not in type_name:
        return type_name
    name, version = type_name.rsplit("@", 1)
    return f"{name}@{version.split('.', 1)[0]}"


def _problem(port: str, index: int, reason: str, detail: str, bean_id: str = "") -> dict[str, Any]:
    return {"port": port, "index": index, "reason": reason, "detail": detail, "bean_id": bean_id}


def _index_name(name: str) -> bool:
    return name == "0" or (name.isdigit() and not name.startswith("0"))


def _path_match(path: str, rule: str) -> bool:
    parts = path.split("/")
    rules = rule.split("/")
    if len(parts) != len(rules):
        return False
    return all(fnmatch.fnmatchcase(part, pat) for part, pat in zip(parts, rules, strict=False))


def _fact_type_ok(declared: str, value: Any) -> bool:
    if isinstance(value, bool):
        return declared == "bool"
    if declared == "int":
        return isinstance(value, int)
    if declared == "float":
        return isinstance(value, float)
    if declared == "string":
        return isinstance(value, str)
    if declared == "bool":
        return False
    if declared == "num":
        return isinstance(value, (int, float))
    return True


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return left == right


def _with_facts(bean: Bean, facts: dict[str, Any]) -> Bean:
    return Bean(bean.id, bean.type, dict(bean.meta), dict(facts), list(bean.payload), False, bean.created_at)


def _meta_schema(snapshot: Any, tid: str) -> dict[str, Any] | None:
    key = snapshot.type_key(tid)
    spec = snapshot.spec(key)
    if "meta_schema" in spec:
        return spec["meta_schema"]
    env = TypeEnv(snapshot.result.extraction, snapshot.result.model)
    seen: set[str] = set()
    current = env.key_parent.get(key)
    while current and current not in seen:
        seen.add(current)
        parent_key = env.type_key.get(current)
        if parent_key and parent_key in env.x.meta_schemas:
            return env.x.meta_schemas[parent_key]
        current = env.parent.get(current)
    return None


def _check_key(item: Check) -> tuple:
    return (item.port, _STAGES.index(item.stage), item.index, item.name)


def _verdict(checks: tuple[Check, ...], produced: dict[str, list[str]], outputs: Mapping[str, Any]) -> str:
    if any(
        (not item.ok and item.detail.startswith("设施故障："))
        or (not item.ok and "设施故障：" in item.detail)
        for item in checks
    ):
        return "error"
    if any(not item.ok for item in checks):
        return "reject"
    for port, decl in outputs.items():
        _expr, _req, optional = _port_decl(decl)
        if not optional and port not in produced and port != "":
            return "reject"
    return "pass"


def _feedback(checks: tuple[Check, ...]) -> str:
    lines = []
    for item in checks:
        if item.ok:
            continue
        lines.append(f"{item.port} {item.stage} {item.name} {item.detail}")
    return "\n".join(lines)


def _report(job_id, station, attempt, verdict, outputs, facts, checks, feedback) -> GateReport:
    ordered_outputs = {port: outputs[port] for port in sorted(outputs)}
    ordered_facts = {bean_id: facts[bean_id] for bean_id in sorted(facts)}
    return GateReport(job_id, station, attempt, verdict, ordered_outputs, ordered_facts, checks, feedback)
