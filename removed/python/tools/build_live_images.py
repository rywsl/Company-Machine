"""构建 M6 现场测试镜像（spec/system/live.md 第 4.1 节）。

用法：
    uv run python tools/build_live_images.py [--only NAME]... [--docker PATH]

- 版本固定在 tools/live_versions.json：base 必须是带 digest 的镜像引用，claude_code、codex 必须是精确的 x.y.z。
  文件里还是占位值（PIN_ME）时拒绝构建。
- 构建上下文是 tools/live_images/<名字>/。构建需要出网（npm、apt），由人执行一次。
- 镜像：mao-test/live-bench:dev、mao-test/claude-code:dev、mao-test/codex:dev。
  每个镜像带标签 mao.live.<键>=<值>，验收测试据此核对版本。
- 成功时 stdout 输出 JSON：{"live-bench": "sha256:...", "claude-code": "sha256:...", "codex": "sha256:..."}。

退出码：0 成功；1 某个镜像构建失败；2 用法错误或版本没有固定；4 Docker 不可用。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONTEXTS = REPO / "tools" / "live_images"
VERSIONS = REPO / "tools" / "live_versions.json"
DIGEST_RE = re.compile(r"^[a-z0-9./:_-]+@sha256:[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
IMAGES = {
    "live-bench": ("bench", {}),
    "claude-code": ("claude-code", {"CLAUDE_CODE_VERSION": "claude_code"}),
    "codex": ("codex", {"CODEX_VERSION": "codex"}),
}


def load_versions(path: Path = VERSIONS) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    if not DIGEST_RE.match(str(data.get("base", ""))):
        problems.append("base 必须是带 digest 的镜像引用")
    for key in ("claude_code", "codex"):
        if not SEMVER_RE.match(str(data.get(key, ""))):
            problems.append(f"{key} 必须是精确的 x.y.z")
    if problems:
        raise ValueError("；".join(problems))
    return {key: str(data[key]) for key in ("base", "claude_code", "codex")}


def build(name: str, versions: dict[str, str], docker: str) -> str:
    folder, args = IMAGES[name]
    argv = [
        docker,
        "build",
        "--build-arg",
        f"BASE={versions['base']}",
        "--label",
        f"mao.live.base={versions['base']}",
    ]
    for arg, key in args.items():
        argv += ["--build-arg", f"{arg}={versions[key]}", "--label", f"mao.live.{key}={versions[key]}"]
    argv += ["-t", f"mao-test/{name}:dev", str(CONTEXTS / folder)]
    proc = subprocess.run(argv, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(f"{name} 构建失败，退出码 {proc.returncode}")
    out = subprocess.run(
        [docker, "image", "inspect", "--format", "{{.Id}}", f"mao-test/{name}:dev"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    return out.stdout.strip()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append", choices=sorted(IMAGES))
    parser.add_argument("--docker", default="docker")
    args = parser.parse_args(argv)
    try:
        versions = load_versions()
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"tools/live_versions.json：{exc}\n")
        return 2
    try:
        subprocess.run([args.docker, "version"], capture_output=True, timeout=60, check=True)
    except (OSError, subprocess.SubprocessError):
        sys.stderr.write("Docker 不可用\n")
        return 4
    ids: dict[str, str] = {}
    for name in args.only or list(IMAGES):
        try:
            ids[name] = build(name, versions, args.docker)
        except (RuntimeError, subprocess.SubprocessError) as exc:
            sys.stderr.write(f"{exc}\n")
            return 1
    sys.stdout.write(json.dumps(ids, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
