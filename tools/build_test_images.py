"""构建 M1 测试镜像 mao-test/<名字>:dev（spec/system/testing.md 第 5 节）。

用法：
    uv run python tools/build_test_images.py [--only NAME]... [--base IMAGE] [--docker PATH]

- 构建上下文是一个临时目录：tests/fixtures/images/<名字>/ 的内容，加上 tests/fakes/（放在 fakes/ 下）。
  复制时把文本文件的 CRLF 换成 LF，避免 Windows 检出的脚本在 Linux 容器里因 `python3\\r` 无法执行。
- --base 覆盖 Dockerfile 的 ARG BASE（默认 python:3.12-alpine），用于需要镜像加速的网络环境。
- 成功时 stdout 输出 JSON：{"tool": "sha256:...", "studio": "sha256:...", "operator": "sha256:..."}。

退出码：0 成功；1 某个镜像构建失败；2 用法错误；4 Docker 不可用。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IMAGES = REPO / "tests" / "fixtures" / "images"
FAKES = REPO / "tests" / "fakes"
NAMES = ("tool", "studio", "operator")
TEXT_SUFFIXES = {".py", ".sh", ".txt", ".json", ".yaml", ".md", ".factory", ""}


def _copy_tree(src: Path, dst: Path) -> None:
    for path in sorted(src.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        target = dst / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        if path.suffix in TEXT_SUFFIXES or path.name == "Dockerfile":
            data = data.replace(b"\r\n", b"\n")
        target.write_bytes(data)


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )


def build(name: str, docker: str, base: str | None) -> str:
    with tempfile.TemporaryDirectory(prefix=f"mao-img-{name}-") as tmp:
        ctx = Path(tmp)
        _copy_tree(IMAGES / name, ctx)
        _copy_tree(FAKES, ctx / "fakes")
        argv = [docker, "build", "--tag", f"mao-test/{name}:dev"]
        if base:
            argv += ["--build-arg", f"BASE={base}"]
        argv.append(str(ctx))
        r = _run(argv, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(f"构建 mao-test/{name}:dev 失败：\n{r.stdout}\n{r.stderr}")
    r = _run([docker, "image", "inspect", "--format", "{{.Id}}", f"mao-test/{name}:dev"], timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"构建后找不到 mao-test/{name}:dev：{r.stderr}")
    return r.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    p = argparse.ArgumentParser(prog="build_test_images", description="构建 M1 测试镜像")
    p.add_argument("--only", action="append", choices=NAMES, help="只构建指定镜像（可重复）")
    p.add_argument("--base", help="覆盖基础镜像（默认 python:3.12-alpine）")
    p.add_argument("--docker", default="docker", help="docker 可执行文件")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if shutil.which(args.docker) is None or _run([args.docker, "version"], timeout=60).returncode != 0:
        print("Docker 不可用：请先启动 Docker 守护进程", file=sys.stderr)
        return 4
    ids: dict[str, str] = {}
    for name in args.only or NAMES:
        try:
            ids[name] = build(name, args.docker, args.base)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"✔ mao-test/{name}:dev {ids[name]}", file=sys.stderr)
    print(json.dumps(ids, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
