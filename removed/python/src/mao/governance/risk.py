"""变更事实与风险分级。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mao.errors import FactoryError
from mrl.factory.paths import spec_root
from mrl.rules import Program


def diff_facts(
    base: Any,
    candidate: Any,
    *,
    base_resolutions: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[list[Any]]], list[dict[str, Any]]]:
    changes = _changes(base, candidate)
    facts: dict[str, list[list[Any]]] = {
        "change": [[item["id"], item["kind"], item["op"]] for item in changes],
        "spec_count": [[len(changes)]],
    }
    fields: list[list[Any]] = []
    roles: list[list[Any]] = []
    weakened: list[list[Any]] = []
    for item in changes:
        fields.extend([item["id"], field] for field in item.get("fields") or [])
        for role in item.get("roles") or []:
            roles.append([item["id"], role])
        for what in item.get("weakened") or []:
            weakened.append([item["id"], what])
    if fields:
        facts["field_changed"] = fields
    if roles:
        facts["station_role"] = roles
    if weakened:
        facts["weakened"] = weakened
    moved = _resolution_changed(base_resolutions, getattr(candidate.result, "resolutions", []))
    if moved:
        facts["resolution_changed"] = moved
    return facts, changes


def score_risk(facts: Mapping[str, list[list[Any]]]) -> Any:
    from dataclasses import dataclass

    program = _risk_program()
    model = program.evaluate({name: [list(row) for row in rows] for name, rows in facts.items()})
    unscored = [[str(row[0]), str(row[1])] for row in model.rows("unscored")]
    if unscored:
        raise FactoryError("RISK_UNSCORED", "风险规则给不出等级", changes=unscored)
    risk_rows = [int(row[0]) for row in model.rows("risk")]
    if not risk_rows:
        raise FactoryError("RISK_UNSCORED", "风险规则没有给出等级", changes=[])
    tier_n = max(risk_rows)
    reviews = {int(row[0]): int(row[1]) for row in model.rows("reviews")}
    stages = tuple(sorted(str(row[1]) for row in model.rows("needs_stage") if int(row[0]) == tier_n))
    multipliers = {int(row[0]): int(row[1]) for row in model.rows("stage_multiplier")}
    if tier_n not in reviews or tier_n not in multipliers or (tier_n > 0 and not stages and tier_n != 0):
        if tier_n not in reviews or tier_n not in multipliers:
            raise FactoryError("RISK_UNSCORED", "风险规则不完整", changes=[])
    if tier_n not in reviews or tier_n not in multipliers:
        raise FactoryError("RISK_UNSCORED", "风险规则不完整", changes=[])

    @dataclass(frozen=True)
    class RiskResult:
        tier: str
        reviews_required: int
        stages: tuple[str, ...]
        multiplier: int
        tiers: dict[str, int]

    tiers: dict[str, int] = {}
    for row in model.rows("tier_of"):
        key = str(row[0])
        tiers[key] = max(tiers.get(key, 0), int(row[1]))
    return RiskResult(
        tier="T" + str(tier_n),
        reviews_required=reviews[tier_n],
        stages=stages,
        multiplier=multipliers[tier_n],
        tiers=tiers,
    )


def governance_limits() -> dict[str, int]:
    model = _risk_program().evaluate({})
    return {str(row[0]): int(row[1]) for row in model.rows("limit")}


_RISK: Program | None = None
_RISK_TEXT: str | None = None


def _risk_program() -> Program:
    global _RISK, _RISK_TEXT
    path = spec_root() / "mrl" / "governance" / "risk.mrl"
    text = path.read_text(encoding="utf-8")
    if _RISK is not None and _RISK_TEXT == text:
        return _RISK
    program = Program.from_sources([(str(path), text)])
    _RISK = program
    _RISK_TEXT = text
    return program


def _changes(base: Any, candidate: Any) -> list[dict[str, Any]]:
    base_docs = {key: base.doc(key) for key in base.keys()}
    cand_docs = {key: candidate.doc(key) for key in candidate.keys()}
    grouped_base: dict[tuple[str, str], list[str]] = {}
    for key in base_docs:
        kind, name, _version = _split(key)
        grouped_base.setdefault((kind, name), []).append(key)
    found: list[dict[str, Any]] = []
    for key, doc in cand_docs.items():
        kind, name, version = _split(key)
        if kind not in _GOVERNED:
            continue
        if key in base_docs:
            if _same(base_docs[key], doc):
                continue
            life = _lifecycle_op(base_docs[key], doc)
            if life:
                found.append({"kind": kind, "name": name, "op": life, "key": key, "version": version})
            continue
        siblings = grouped_base.get((kind, name), [])
        if not siblings:
            found.append(
                {"kind": kind, "name": name, "op": "add", "key": key, "version": version, "doc": doc}
            )
            continue
        old_key = max(siblings, key=lambda item: _version_key(kind, _split(item)[2]))
        op = "modify"
        if kind == "Type":
            old_major = _split(old_key)[2].split(".", 1)[0]
            new_major = version.split(".", 1)[0]
            op = "minor" if old_major == new_major else "major"
        item = {
            "kind": kind,
            "name": name,
            "op": op,
            "key": key,
            "version": version,
            "old_key": old_key,
            "doc": doc,
            "old": base_docs[old_key],
        }
        if op == "modify":
            item["fields"] = _fields(kind, base_docs[old_key], doc)
        if kind == "Station":
            roles = []
            for source in (base_docs.get(old_key), doc):
                role = ((source or {}).get("spec") or {}).get("role")
                if role and role not in roles:
                    roles.append(str(role))
            item["roles"] = roles
        found.append(item)
    found.sort(key=lambda item: (item["kind"], item["name"], item["key"]))
    for index, item in enumerate(found):
        item["id"] = f"c{index}"
    return found


_GOVERNED = {
    "Type",
    "Machine",
    "OperatorRuntime",
    "Station",
    "Line",
    "Policy",
    "GoldenSet",
    "Baseline",
    "Agent",
}


def _split(key: str) -> tuple[str, str, str]:
    kind, rest = key.split(":", 1)
    name, version = rest.rsplit("@", 1)
    return kind, name, version


def _version_key(kind: str, version: str) -> tuple[int, ...]:
    parts = version.split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return (0,)


def _same(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return _norm(left) == _norm(right)


def _norm(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _norm(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(item) for item in value]
    return value


def _lifecycle_op(old: Mapping[str, Any], new: Mapping[str, Any]) -> str | None:
    old_copy = _norm(old)
    new_copy = _norm(new)
    old_life = (old_copy.get("metadata") or {}).get("lifecycle", "active")
    new_life = (new_copy.get("metadata") or {}).get("lifecycle", "active")
    old_copy.setdefault("metadata", {})["lifecycle"] = old_life
    new_copy.setdefault("metadata", {})["lifecycle"] = old_life
    if old_copy != new_copy:
        return None
    if old_life == "active" and new_life == "deprecated":
        return "deprecate"
    if new_life == "retired":
        return "retire"
    return None


def _fields(kind: str, old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    if kind == "Agent":
        return _agent_fields(old, new)
    old_spec = old.get("spec") or {}
    new_spec = new.get("spec") or {}
    found: list[str] = []
    for field in sorted(set(old_spec) | set(new_spec)):
        if _norm(old_spec.get(field)) == _norm(new_spec.get(field)):
            continue
        if kind == "Machine" and field == "runtime":
            found.extend(
                _narrow(old_spec.get("runtime") or {}, new_spec.get("runtime") or {}, "runtime", "image")
            )
        elif kind == "Machine" and field == "recipes":
            found.extend(_recipe_fields(old_spec.get("recipes") or {}, new_spec.get("recipes") or {}))
        elif kind == "Station" and field == "operator":
            found.extend(
                _narrow(old_spec.get("operator") or {}, new_spec.get("operator") or {}, "operator", "model")
            )
        else:
            found.append(field)
    old_meta = {
        key: value
        for key, value in (old.get("metadata") or {}).items()
        if key not in ("name", "version", "lifecycle")
    }
    new_meta = {
        key: value
        for key, value in (new.get("metadata") or {}).items()
        if key not in ("name", "version", "lifecycle")
    }
    if _norm(old_meta) != _norm(new_meta):
        found.append("metadata")
    return found


def _agent_fields(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    old_spec = old.get("spec") or {}
    new_spec = new.get("spec") or {}
    structural = []
    for field in ("runtime", "model", "charter", "context", "policy"):
        if _norm(old_spec.get(field)) != _norm(new_spec.get(field)):
            structural.append(field)
    for field in sorted(set(old_spec) | set(new_spec)):
        if field in ("runtime", "model", "charter", "context", "policy", "lines", "subscribe", "budget"):
            continue
        if _norm(old_spec.get(field)) != _norm(new_spec.get(field)):
            structural.append(field)
    reduced = _agent_scope_reduced(old_spec, new_spec)
    expanded = _agent_scope_expanded(old_spec, new_spec)
    if reduced and not expanded and not structural:
        return ["scope.reduced"]
    found = list(structural)
    if expanded:
        found.append("scope.expanded")
    elif _norm(old_spec.get("lines")) != _norm(new_spec.get("lines")):
        found.append("lines")
    elif _norm(old_spec.get("subscribe")) != _norm(new_spec.get("subscribe")):
        found.append("subscribe")
    elif _norm(old_spec.get("budget")) != _norm(new_spec.get("budget")):
        found.append("budget")
    return found


def _agent_scope_reduced(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    old_lines = set(old.get("lines") or [])
    new_lines = set(new.get("lines") or [])
    if not new_lines <= old_lines:
        return False
    old_sub = old.get("subscribe") or {}
    new_sub = new.get("subscribe") or {}
    if set(new_sub.get("ingress") or {}) - set(old_sub.get("ingress") or {}):
        return False
    if set(new_sub.get("beans") or []) - set(_as_tuples(old_sub.get("beans"))):
        return False
    old_every = _schedule_every(old_sub)
    new_every = _schedule_every(new_sub)
    if new_every and old_every and new_every < old_every:
        return False
    return _budget_not_raised(old.get("budget") or {}, new.get("budget") or {})


def _agent_scope_expanded(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    old_lines = set(old.get("lines") or [])
    new_lines = set(new.get("lines") or [])
    if new_lines - old_lines:
        return True
    old_sub = old.get("subscribe") or {}
    new_sub = new.get("subscribe") or {}
    if set(new_sub.get("ingress") or {}) - set(old_sub.get("ingress") or {}):
        return True
    old_every = _schedule_every(old_sub)
    new_every = _schedule_every(new_sub)
    if old_every and new_every and new_every < old_every:
        return True
    old_budget = old.get("budget") or {}
    new_budget = new.get("budget") or {}
    return not _budget_not_raised(old_budget, new_budget) and _norm(old_budget) != _norm(new_budget)


def _schedule_every(subscribe: Mapping[str, Any]) -> float | None:
    from mao.base import parse_duration

    items = subscribe.get("schedule") or []
    if not items or not isinstance(items, list):
        return None
    first = items[0] if isinstance(items[0], Mapping) else {}
    raw = first.get("every")
    if not isinstance(raw, str):
        return None
    try:
        return float(parse_duration(raw))
    except FactoryError:
        return None


def _budget_not_raised(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    for key in ("daily_usd", "turn_usd"):
        if _num(new.get(key)) > _num(old.get(key)):
            return False
    if _num(new.get("turn_timeout_s")) > _num(old.get("turn_timeout_s")):
        return False
    return True


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _as_tuples(value: Any) -> list[tuple[str, ...]]:
    if not isinstance(value, list):
        return []
    found = []
    for item in value:
        if isinstance(item, Mapping):
            found.append(tuple(sorted((str(key), str(val)) for key, val in item.items())))
        else:
            found.append((str(item),))
    return found


def _narrow(old: Mapping[str, Any], new: Mapping[str, Any], prefix: str, only: str) -> list[str]:
    old_rest = {key: value for key, value in old.items() if key != only}
    new_rest = {key: value for key, value in new.items() if key != only}
    found = []
    if _norm(old.get(only)) != _norm(new.get(only)):
        found.append(f"{prefix}.{only}")
    if _norm(old_rest) != _norm(new_rest):
        found.append(prefix)
    return found


def _recipe_fields(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    if _templates(old) != _templates(new) and _without_templates(old) == _without_templates(new):
        return ["recipes.task_template"]
    return ["recipes"]


def _templates(recipes: Mapping[str, Any]) -> Any:
    return {name: (body or {}).get("task_template") for name, body in recipes.items()}


def _without_templates(recipes: Mapping[str, Any]) -> Any:
    out = {}
    for name, body in recipes.items():
        copied = dict(body or {})
        copied.pop("task_template", None)
        out[name] = copied
    return _norm(out)


def _resolution_changed(
    base_rows: Sequence[Mapping[str, Any]], candidate_rows: Sequence[Mapping[str, Any]]
) -> list[list[str]]:
    def key_of(row: Mapping[str, Any]) -> tuple[str, str, str]:
        return (str(row.get("line", "")), str(row.get("step", "")), str(row.get("port", "")))

    candidate = {key_of(row): row for row in candidate_rows}
    moved = []
    for row in base_rows:
        key = key_of(row)
        other = candidate.get(key)
        if (
            other is None
            or row.get("status") != other.get("status")
            or row.get("chain") != other.get("chain")
        ):
            moved.append([key[0], key[1], key[2]])
    return moved
