"""Registry 快照：只读加载、epoch、已编译表达式。"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from mao.base import canonical_bytes, digest_bytes, digest_file
from mao.errors import FactoryError
from mrl.errors import StaticError
from mrl.expr import compile_expr, compile_template
from mrl.factory.check import check_registry
from mrl.factory.registry import load_registry
from mrl.factory.sites import TypeEnv, site_env
from mrl.stdlib import make_table


class Snapshot:
    root: Path
    epoch: str
    result: Any

    def __init__(
        self, root: Path, epoch: str, result: Any, docs: dict[str, Any], compiled_cache: dict[str, Any]
    ) -> None:
        self.root = root
        self.epoch = epoch
        self.result = result
        self._docs = docs
        self._compiled = compiled_cache

    @classmethod
    def load(cls, root: Path) -> Snapshot:
        root = Path(root).resolve()
        result = check_registry(root)
        if result.model is None or result.extraction is None:
            raise AssertionError("检查器没有返回 model 或 extraction")
        errors = [diag for diag in result.diags if diag.severity == "error"]
        if errors:
            raise FactoryError(
                "REGISTRY_INVALID",
                f"Registry 有 {len(errors)} 个错误，不能作为运行快照",
                diags=[diag.to_json() for diag in errors],
            )
        epoch = _tree_epoch(root)
        registry = load_registry(root)
        docs: dict[str, Any] = {}
        for doc in registry.docs:
            key = doc.key
            if key:
                docs[key] = doc.data
        compiled_cache = _compile_sites(result)
        return cls(root, epoch, result, docs, compiled_cache)

    def key(self, kind: str, ref: str) -> str:
        if ":" in ref:
            return ref
        return f"{kind}:{ref}"

    def doc(self, key: str) -> dict[str, Any]:
        if key not in self._docs:
            raise FactoryError("SPEC_NOT_FOUND", f"找不到 Spec {key}", key=key)
        return copy.deepcopy(self._docs[key])

    def spec(self, key: str) -> dict[str, Any]:
        return copy.deepcopy(self.doc(key)["spec"])

    def type_key(self, tid: str) -> str:
        for row in self.result.model.rows("type_key"):
            if row[0] == tid:
                return str(row[1])
        raise FactoryError("TYPE_NOT_FOUND", f"类型 {tid} 在快照中不存在", tid=tid)

    def is_subtype(self, t: str, s: str) -> bool:
        return any(row[0] == t and row[1] == s for row in self.result.model.rows("subtype"))

    def facts_decl(self, tid: str) -> dict[str, str]:
        self.type_key(tid)
        return {
            str(fact): str(typ) for owner, fact, typ in self.result.model.rows("type_fact") if owner == tid
        }

    def validators(self, tid: str) -> list[dict[str, Any]]:
        key = self.type_key(tid)
        return copy.deepcopy(self.spec(key).get("validators") or [])

    def properties(self, tid: str) -> dict[str, Any]:
        self.type_key(tid)
        found: dict[str, Any] = {}
        for owner_tid, name, owner in self.result.model.rows("type_prop"):
            if owner_tid != tid:
                continue
            subject = f"{self.type_key(str(owner))}/properties.{name}"
            found[str(name)] = self.compiled(subject)
        return {name: found[name] for name in sorted(found)}

    def always(self, tid: str) -> frozenset[str]:
        self.type_key(tid)
        return frozenset(str(name) for owner, name in self.result.model.rows("type_always_p") if owner == tid)

    def compiled(self, subject: str) -> Any:
        if subject not in self._compiled:
            raise FactoryError("SUBJECT_UNKNOWN", f"快照中没有表达式位置 {subject}", subject=subject)
        return self._compiled[subject]


def _tree_epoch(root: Path) -> str:
    listing: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        listing[path.relative_to(root).as_posix()] = digest_file(path)
    tree = digest_bytes(canonical_bytes(listing))
    return "e-" + tree.split(":", 1)[1][:16]


def _compile_sites(result: Any) -> dict[str, Any]:
    functions = make_table()
    env = TypeEnv(result.extraction, result.model)
    cache: dict[str, Any] = {}
    for site in result.extraction.sites:
        if site.subject in cache:
            raise AssertionError(f"表达式主体重复：{site.subject}")
        types, expect, problem = site_env(site, env)
        if problem:
            raise AssertionError(problem)
        try:
            if site.template:
                compiled = compile_template(site.text, types, functions)
            else:
                compiled = compile_expr(site.text, types, functions, expect=expect)
        except StaticError:
            raise
        cache[site.subject] = compiled
    return cache
