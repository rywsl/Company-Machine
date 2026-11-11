"""控制面代码的静态规则（spec/system/README.md 第 1、3 节；plan/README.md 第 6 节；plan/drift.md）。

这些规则用 AST 检查 src/mao 下的全部源码，违反即失败。没有豁免注释：
确实需要例外时，先改 README 并写 ADR，再由人修改本文件。本文件受保护（plan/protected.lock）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAO = ROOT / "src" / "mao"
FACTORYCTL = MAO / "ctl" / "factoryctl.py"

# 只有这些文件可以接触真实时间与随机数（D9）
CLOCK_FILES = {"base.py", "ctl/factoryctl.py"}
# 只有这些文件可以 print（其余模块是库代码，输出走返回值、事件或 logging）。
# commands/ 是命令行的处理模块（spec/system/commands.md 3.1）
PRINT_FILES = {"cli.py", "ctl/factoryctl.py"}
PRINT_PREFIXES = ("commands/",)
# D2：运行时依赖白名单（标准库之外）
THIRD_PARTY = {"yaml", "jsonschema", "mrl", "mao"}

FORBIDDEN_CALLS = {
    ("os", "system"): "禁止 os.system：用 subprocess 并传参数列表（README 3.7）",
    ("os", "popen"): "禁止 os.popen：用 subprocess 并传参数列表（README 3.7）",
}
CLOCK_CALLS = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("datetime", "today"),
    ("date", "today"),
    ("time", "time"),
    ("time", "time_ns"),
    ("time", "sleep"),
    ("time", "monotonic"),
    ("time", "perf_counter"),
    ("uuid", "uuid1"),
    ("uuid", "uuid4"),
}
FORBIDDEN_LITERALS = {
    "docker.sock": "禁止出现 docker.sock（D11）",
    "--privileged": "禁止 --privileged（D11）",
    "--cap-add": "禁止 --cap-add（D11）",
    "--network=host": "禁止 host 网络（D11）",
}
# 控制面不认识测试基础设施（plan/README.md 第 6 节第 4 条，plan/drift.md D-02、D-03、D-06）
TEST_INFRA_LITERALS = (
    "tests/",
    "tests\\",
    "registry-m1",
    "registry-m2",
    "spec/examples",
    "fake_agent",
    "fake-agent",
    "fakelib",
)
# 只有命令行入口可以读当前目录（README 3.1：FACTORY_ROOT 的缺省值是当前目录下的 .factory）
CWD_FILES = {"cli.py"}
# 夹具与示例 Registry 里与普通词汇不冲突的名字（不含连字符的只列这些）；其余带连字符的名字自动收集
PLAIN_FIXTURE_NAMES = {
    "explainer",
    "upper",
    "quote",
    "toolbox",
    "studio",
    "vertical",
    "openmontage",
    "moneyprinter",
    "servicebox",
    "idlebox",
    "writer",
}


def _fixture_names() -> frozenset[str]:
    """示例 Registry 与测试夹具里的工位、产线、机器、黄金样例、基线名。控制面代码不得写死这些名字。"""
    import yaml

    names: set[str] = set()
    registries = [
        ROOT / "spec" / "examples" / "registry",
        *sorted((ROOT / "tests" / "fixtures").glob("registry-*")),
    ]
    for registry in registries:
        for path in sorted(registry.rglob("*.yaml")):
            try:
                docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
            except yaml.YAMLError:
                continue
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                if doc.get("kind") not in ("Station", "Line", "Machine", "GoldenSet", "Baseline"):
                    continue
                name = str((doc.get("metadata") or {}).get("name") or "")
                if "-" in name or name in PLAIN_FIXTURE_NAMES:
                    names.add(name)
    return frozenset(names)


def _private_owners() -> dict[str, set[str]]:
    """私有成员名 → 定义它的源文件。私有成员指 self._x 赋值或 def _x 方法。"""
    owners: dict[str, set[str]] = {}
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(node.name)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                if isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
                    names.append(node.attr)
            for name in names:
                if name.startswith("_") and not name.startswith("__"):
                    owners.setdefault(name, set()).add(_rel(path))
    return owners


def _sources() -> list[Path]:
    return sorted(p for p in MAO.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(p: Path) -> str:
    return p.relative_to(MAO).as_posix()


def _dotted(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return tuple(reversed(parts))
    return ()


def _violations(path: Path) -> list[str]:
    rel = _rel(path)
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    out: list[str] = []
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(a.asname or a.name for a in node.names)
    owners = _PRIVATE_OWNERS

    def bad(node: ast.AST, msg: str) -> None:
        out.append(f"src/mao/{rel}:{getattr(node, 'lineno', 0)}: {msg}")

    def foreign_private(attr: str) -> bool:
        """attr 是别的源文件定义的私有成员，本文件自己没有定义它。"""
        where = owners.get(attr)
        return bool(where) and rel not in where

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    if target.value.id in imported:
                        bad(
                            node,
                            f"禁止给导入的对象打补丁（{target.value.id}.{target.attr} = ...）（plan/drift.md D-01）",
                        )
        if isinstance(node, ast.Attribute):
            if node.attr in ("cwd", "getcwd") and rel not in CWD_FILES:
                bad(node, "库代码禁止读当前目录：路径来自 Context.root 或调用方参数（plan/drift.md D-03）")
            base = node.value
            if not (isinstance(base, ast.Name) and base.id in ("self", "cls")) and foreign_private(node.attr):
                bad(
                    node,
                    f"禁止访问别的模块的私有成员 {node.attr}：需要就在规范里加公开接口（plan/drift.md D-02）",
                )
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in (("setattr",), ("getattr",), ("hasattr",)) and len(node.args) >= 2:
                first, second = node.args[0], node.args[1]
                if name == ("setattr",) and isinstance(first, ast.Name) and first.id in imported:
                    bad(node, "禁止用 setattr 给导入的对象打补丁（plan/drift.md D-01）")
                if isinstance(second, ast.Constant) and isinstance(second.value, str):
                    if foreign_private(second.value):
                        bad(
                            node, f"禁止用 {name[0]} 绕过私有成员检查（{second.value}）（plan/drift.md D-02）"
                        )
            if name in (("eval",), ("exec",), ("compile",)):
                bad(node, f"禁止调用 {name[0]}()：Spec 中的表达式一律用 MRL 编译（D3）")
            if len(name) >= 2 and name[-2:] in FORBIDDEN_CALLS:
                bad(node, FORBIDDEN_CALLS[name[-2:]])
            if rel not in CLOCK_FILES and len(name) >= 2 and name[-2:] in CLOCK_CALLS:
                bad(node, f"禁止直接调用 {'.'.join(name)}：时间与 ID 必须来自注入的 Clock / IdSource（D9）")
            if rel not in PRINT_FILES and not rel.startswith(PRINT_PREFIXES) and name == ("print",):
                bad(node, "库代码禁止 print：结果用返回值、事件或 logging 表达")
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    bad(node, "禁止 shell=True（README 3.7）")
            if name and name[-1] in ("open", "read_text", "write_text"):
                mode = ""
                if name == ("open",) and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    mode = str(node.args[1].value)
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = str(kw.value.value)
                if "b" not in mode and not any(kw.arg == "encoding" for kw in node.keywords):
                    if name == ("open",) or name[-1] in ("read_text", "write_text"):
                        bad(node, f"{name[-1]}() 必须显式指定 encoding（README 3.8）")
        elif isinstance(node, ast.ExceptHandler):
            body_is_pass = all(isinstance(s, ast.Pass) for s in node.body)
            if node.type is None:
                bad(node, "禁止裸 except:（README 3.3）")
            elif body_is_pass and _dotted(node.type) in (("Exception",), ("BaseException",)):
                bad(node, "禁止 except Exception: pass 吞掉错误（README 3.3）")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.level:
                continue  # 包内相对导入
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for mod in names:
                top = mod.split(".")[0]
                if rel == "ctl/factoryctl.py":
                    if top not in sys.stdlib_module_names and top != "__future__":
                        bad(node, f"factoryctl 只能使用标准库，不能导入 {mod}（D7）")
                elif top not in sys.stdlib_module_names and top not in THIRD_PARTY and top != "__future__":
                    bad(node, f"不允许的依赖 {mod}：新增第三方依赖必须先有 ADR（D2）")
                if top == "random" and rel not in CLOCK_FILES:
                    bad(node, "禁止使用 random：ID 与随机性来自注入的 IdSource（D9）")
                if top in ("jinja2", "docker", "requests", "psutil", "pydantic"):
                    bad(node, f"禁止导入 {top}（D2/D3）")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for lit, msg in FORBIDDEN_LITERALS.items():
                if lit in node.value:
                    bad(node, msg)
            for lit in TEST_INFRA_LITERALS:
                if lit in node.value:
                    bad(node, f"控制面代码不得引用测试基础设施 {lit!r}（plan/README.md 第 6 节第 4 条）")
            for fixture in _FIXTURE_NAMES:
                text = node.value
                if text == fixture or text.startswith(fixture + "@") or (":" + fixture + "@") in text:
                    bad(
                        node,
                        f"控制面代码不得写死夹具或示例里的名字 {fixture!r}（plan/README.md 第 6 节第 4 条）",
                    )
    return out


_FIXTURE_NAMES = _fixture_names()
_PRIVATE_OWNERS = _private_owners()


def test_mao_package_exists() -> None:
    """src/mao 是控制面的包（D1）。"""
    assert (MAO / "__init__.py").is_file()


def test_pytest_config_only_in_pyproject() -> None:
    """pytest 配置只放在受保护的 pyproject.toml。另起 pytest.ini / tox.ini / setup.cfg 会让 pyproject 的设置失效。"""
    for name in ("pytest.ini", "tox.ini", "setup.cfg"):
        assert not (ROOT / name).exists(), f"不允许新增 {name}（plan/drift.md D-11）"
    extra = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").rglob("conftest.py"))
    assert extra == ["tests/conftest.py"], f"不允许新增 conftest.py：{extra}"


@pytest.mark.parametrize("path", _sources(), ids=_rel)
def test_source_follows_policy(path: Path) -> None:
    """每个源文件都遵守 README 的静态规则。"""
    problems = _violations(path)
    assert not problems, "\n".join(problems)


def test_factoryctl_is_python38_compatible() -> None:
    """factoryctl 在镜像里的 python3 下运行，语法必须兼容 Python 3.8，且不能导入 mao（D7）。"""
    if not FACTORYCTL.exists():
        pytest.skip("factoryctl 尚未实现（M1-05）")
    src = FACTORYCTL.read_text(encoding="utf-8")
    ast.parse(src, filename=str(FACTORYCTL), feature_version=(3, 8))
    assert "\r\n" not in src, "factoryctl 必须是 LF 换行（README 3.8）"
    assert "from mao" not in src and "import mao" not in src


def test_migrations_are_numbered() -> None:
    """迁移文件按 NNN_<名字>.sql 命名且编号不重复（README 3.6）。"""
    mig = MAO / "migrations"
    if not mig.exists():
        pytest.skip("还没有迁移文件（M1-01）")
    nums = []
    for p in sorted(mig.glob("*.sql")):
        head = p.name.split("_", 1)[0]
        assert len(head) == 3 and head.isdigit(), f"迁移文件名不合规：{p.name}"
        nums.append(int(head))
    assert len(nums) == len(set(nums)), f"迁移编号重复：{nums}"
