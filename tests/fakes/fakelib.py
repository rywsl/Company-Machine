"""测试替身的公共工具（spec/system/testing.md 第 4–6 节）。

本模块不依赖 mao，可以在任何 mao 模块实现之前导入。提供：
- 夹具 Registry 的渲染（复制到临时目录、替换镜像 digest、按路径打补丁）；
- LocalProvider 的命令映射（把 upper-text 等命令映射到本目录下的 Python 脚本）；
- 测试镜像的 image ID 查询；
- 构造输入文件的小工具。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
FAKES = REPO / "tests" / "fakes"
TOOLS = FAKES / "tools"
FAKE_AGENT = FAKES / "fake_agent.py"
FIXTURES = REPO / "tests" / "fixtures"
REGISTRY_M1 = FIXTURES / "registry-m1"
IMAGES_DIR = FIXTURES / "images"
GOLDEN = FIXTURES / "golden"
FACTORYCTL_SRC = REPO / "src" / "mao" / "ctl" / "factoryctl.py"
REFERENCE_CONSTITUTION = REPO / "spec" / "mrl" / "constitution" / "invariants.mrl"

# 测试镜像：名字 → 夹具 Registry 中的占位 digest（镜像名的 sha256）
IMAGE_NAMES = ("tool", "studio", "operator")
PLACEHOLDERS = {
    "tool": "09b42648636a8405354592f2ae8985670dfda5b61377ac6c75e3cebbd22e0500",
    "studio": "f6bf945c5ebf73ef9407b0a47f424feeccd5a1783675d678bf1b66278c5e81f3",
    "operator": "cef52db3ca9921be9f4a7c0c1e2c5eab79aa09b827c55fcf60c8a353247d7433",
}

# 夹具工具：沙箱内的命令名 → 本目录下的脚本
TOOL_SCRIPTS = {
    "upper-text": TOOLS / "upper_text.py",
    "count-text": TOOLS / "count_text.py",
    "check-task": TOOLS / "check_task.py",
}
FAKE_AGENT_COMMAND = "/opt/operator/bin/fake-agent"


def image_tag(name: str) -> str:
    return f"mao-test/{name}:dev"


def local_commands() -> dict[str, list[str]]:
    """LocalProvider 的 commands 映射：夹具工具与 fake agent 都用当前解释器运行。"""
    cmds = {name: [sys.executable, str(path)] for name, path in TOOL_SCRIPTS.items()}
    cmds[FAKE_AGENT_COMMAND] = [sys.executable, str(FAKE_AGENT)]
    return cmds


class MissingImages(Exception):
    pass


def docker_image_ids(docker: str = "docker") -> dict[str, str]:
    """测试镜像的 image ID（"sha256:<hex>"）。缺少任何一个都抛出 MissingImages。"""
    ids: dict[str, str] = {}
    missing: list[str] = []
    for name in IMAGE_NAMES:
        r = subprocess.run(
            [docker, "image", "inspect", "--format", "{{.Id}}", image_tag(name)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        value = r.stdout.strip()
        if r.returncode != 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            missing.append(image_tag(name))
        else:
            ids[name] = value
    if missing:
        raise MissingImages(
            "测试镜像不存在：" + "、".join(missing) + "。先运行 uv run python tools/build_test_images.py"
        )
    return ids


def _set_path(doc: Any, path: str, value: Any) -> None:
    parts = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    cur = doc
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part.startswith("["):
            idx = int(part[1:-1])
            if last:
                cur[idx] = value
            else:
                cur = cur[idx]
        elif last:
            if value is None:
                cur.pop(part, None)
            else:
                cur[part] = value
        else:
            cur = cur.setdefault(part, {})


def render_registry(
    dest: Path,
    *,
    images: dict[str, str] | None = None,
    patches: list[tuple[str, str, Any]] | None = None,
    files: dict[str, str] | None = None,
) -> Path:
    """把夹具 Registry 复制到 dest 并返回 dest。

    - images：镜像名 → "sha256:<hex>"，把占位 digest 换成真实 image ID（Docker 测试用）；
    - patches：[(相对文件, 点路径, 值)]，值为 None 表示删除该键。文件按 YAML 读写；
    - files：额外写入的文件（相对路径 → 文本），可以新增或覆盖 Spec。
    """
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(REGISTRY_M1, dest)
    if images:
        for path in sorted(dest.rglob("*.yaml")):
            text = path.read_text(encoding="utf-8")
            new = text
            for name, image_id in images.items():
                new = new.replace(PLACEHOLDERS[name], image_id.removeprefix("sha256:"))
            if new != text:
                path.write_text(new, encoding="utf-8", newline="\n")
    for rel, dotted, value in patches or []:
        path = dest / rel
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        _set_path(doc, dotted, value)
        path.write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8", newline="\n"
        )
    for rel, text in (files or {}).items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return dest


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def write_json(path: Path, obj: Any) -> Path:
    return write_text(path, json.dumps(obj, ensure_ascii=False, sort_keys=True))


def task_script(attempts: dict[int, list[dict[str, Any]]] | None = None, **kw: Any) -> dict[str, Any]:
    """构造 fake agent 脚本：task_script({1: [...], 2: [...]}, default=[...], cost_usd=0.1)。"""
    script: dict[str, Any] = dict(kw)
    if attempts:
        script["attempts"] = {str(k): v for k, v in attempts.items()}
    return script


def has_crlf(path: Path) -> bool:
    return b"\r\n" in path.read_bytes()
