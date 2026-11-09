"""读取 Registry 目录（布局见 spec/mrl/factory-profile.md 2.1）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from ..yamlio import YamlError, load_documents

# (glob, 该位置应有的 kind)。只有这些位置的 YAML 才是 Spec；其他文件（样例、脚本、compose 等）不是。
LAYOUT: list[tuple[str, str]] = [
    ("types/*.yaml", "Type"),
    ("runtimes/*.yaml", "OperatorRuntime"),
    ("policies/*.yaml", "Policy"),
    ("lines/*.yaml", "Line"),
    ("goldens/*.yaml", "GoldenSet"),
    ("baselines/*.yaml", "Baseline"),
    ("stations/*.yaml", "Station"),
    ("stations/*/station.yaml", "Station"),
    ("converters/*/station.yaml", "Station"),
    ("machines/*/machine.yaml", "Machine"),
]


@dataclass
class HostDiag:
    code: str
    subject: str
    message: str
    file: str = ""
    doc: int = -1
    path: str = ""
    line: int = 0
    col: int = 0


@dataclass
class SpecDoc:
    file: str  # 相对 Registry 根目录的 POSIX 路径
    index: int  # 文件内的文档序号
    line: int
    data: Any
    expected_kind: str = ""
    positions: dict[str, tuple[int, int]] = field(default_factory=dict)

    def position(self, path: str) -> tuple[int, int]:
        """路径（a.b[0].c）在文件中的（行, 列）；找不到时逐级退回到上一级路径，最后退回到文档起始行。"""
        p = path
        while p:
            if p in self.positions:
                return self.positions[p]
            cut = max(p.rfind("."), p.rfind("["))
            p = p[:cut] if cut > 0 else ""
        return self.positions.get("", (self.line, 1))

    @property
    def kind(self) -> str:
        return self.data.get("kind", "") if isinstance(self.data, dict) else ""

    @property
    def key(self) -> str | None:
        """Kind:name@version；信封不完整时为 None。"""
        d = self.data
        if not isinstance(d, dict):
            return None
        meta = d.get("metadata")
        if not isinstance(meta, dict):
            return None
        name, version = meta.get("name"), meta.get("version")
        kind = d.get("kind")
        if not isinstance(kind, str) or not isinstance(name, str):
            return None
        if isinstance(version, bool) or not isinstance(version, (int, str)):
            return None
        return f"{kind}:{name}@{version}"

    @property
    def subject(self) -> str:
        return self.key or f"File:{self.file}#{self.index}"


@dataclass
class Registry:
    root: str
    docs: list[SpecDoc] = field(default_factory=list)
    constitution: list[tuple[str, str]] = field(default_factory=list)
    diags: list[HostDiag] = field(default_factory=list)


def layout_kind(rel: str) -> str | None:
    """相对路径（POSIX）在布局中对应的 kind；不在布局中时为 None。* 不跨越目录。"""
    parts = rel.split("/")
    for pattern, kind in LAYOUT:
        for ext in (".yaml", ".yml"):
            pp = pattern.replace(".yaml", ext).split("/")
            if len(pp) == len(parts) and all(fnmatchcase(a, b) for a, b in zip(parts, pp, strict=True)):
                return kind
    return None


def is_constitution(rel: str) -> bool:
    return rel.startswith("constitution/") and rel.endswith(".mrl") and rel.count("/") == 1


def spec_files(root: Path) -> list[tuple[Path, str]]:
    seen: dict[Path, str] = {}
    for pattern, kind in LAYOUT:
        for ext in (".yaml", ".yml"):
            for p in root.glob(pattern.replace(".yaml", ext)):
                if p.is_file():
                    seen.setdefault(p, kind)
    return sorted(seen.items(), key=lambda kv: kv[0].relative_to(root).as_posix())


def read_tree(root: str | Path) -> dict[str, str]:
    """读取 Registry 中与检查有关的全部文件：布局中的 Spec 文件，以及 constitution/*.mrl。"""
    rootp = Path(root)
    files = {p.relative_to(rootp).as_posix(): p.read_text(encoding="utf-8") for p, _kind in spec_files(rootp)}
    cdir = rootp / "constitution"
    if cdir.is_dir():
        for p in sorted(cdir.glob("*.mrl")):
            files[f"constitution/{p.name}"] = p.read_text(encoding="utf-8")
    return files


def load_text_docs(rel: str, text: str, expected_kind: str, reg: Registry) -> None:
    try:
        docs = load_documents(text)
    except YamlError as e:
        reg.diags.append(
            HostDiag(
                "E-YAML",
                f"File:{rel}",
                f"YAML 错误（第 {e.line} 行第 {e.col} 列）：{e.message}",
                rel,
                0,
                "",
                e.line,
                e.col,
            )
        )
        return
    for d in docs:
        reg.docs.append(SpecDoc(rel, d.index, d.line, d.data, expected_kind, d.positions))


def registry_from_texts(root: str, files: dict[str, str]) -> Registry:
    """由 {相对路径: 文本} 构造 Registry；不在布局中的文件被忽略。"""
    reg = Registry(root)
    for rel in sorted(files):
        if is_constitution(rel):
            reg.constitution.append((rel, files[rel]))
            continue
        kind = layout_kind(rel)
        if kind is not None:
            load_text_docs(rel, files[rel], kind, reg)
    return reg


def load_registry(root: str | Path) -> Registry:
    return registry_from_texts(str(root), read_tree(root))
