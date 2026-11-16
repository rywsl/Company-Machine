"""把 M15 工厂接到 still-to-clip。密钥只从环境变量读，不写进仓库。

需要：
  MAO_OPENAI_API_KEY   gpt-6.1-sol，https://openai.createsis.cn
  MAO_TBTK_API_KEY     newapi，https://tbtk.asia，给 OpenMontage 生图
  MAO_AUTODL_AK        AutoDL ComfyUI 令牌，给 MiniMax H3
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "registry"
REPO = Path(__file__).resolve().parents[2]


def main() -> int:
    home = Path(os.environ.get("MAO_HOME") or str(Path.home() / "mao-m15"))
    root = Path(os.environ.get("FACTORY_ROOT") or str(home / "factory"))
    registry = Path(os.environ.get("FACTORY_REGISTRY") or str(home / "registry"))
    root.mkdir(parents=True, exist_ok=True)
    _copy_registry(registry)
    commit = _commit(registry)
    _factory_json(root, registry)
    _egress(root)
    _commands(root)
    _secrets(root)
    check = _factory(root, ["check", str(SOURCE)])
    if check.returncode != 0:
        sys.stderr.write(check.stderr)
        sys.stderr.write(check.stdout)
        return check.returncode
    if not (root / "factory.db").is_file() or _epoch_missing(root):
        init = _factory(root, ["epoch", "init", commit, "--by", "human:bootstrap"])
        if init.returncode != 0:
            sys.stderr.write(init.stderr)
            sys.stderr.write(init.stdout)
            return init.returncode
    print(json.dumps({"root": str(root), "registry": str(registry), "commit": commit}))
    return 0


def _copy_registry(dest: Path) -> None:
    if dest.exists():
        for child in dest.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        dest.mkdir(parents=True)
    for path in SOURCE.rglob("*"):
        if not path.is_file():
            continue
        target = dest / path.relative_to(SOURCE)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _commit(registry: Path) -> str:
    if not (registry / ".git").exists():
        subprocess.run(["git", "init"], cwd=registry, check=True)
    subprocess.run(["git", "add", "-A"], cwd=registry, check=True)
    dirty = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=registry, check=False)
    if dirty.returncode != 0:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=mao",
                "-c",
                "user.email=mao@factory.invalid",
                "commit",
                "-m",
                "still-to-clip registry",
            ],
            cwd=registry,
            check=True,
        )
    shown = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=registry, check=True, capture_output=True, text=True
    )
    return shown.stdout.strip()


def _factory_json(root: Path, registry: Path) -> None:
    image = "local/bench@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    body = {
        "serve": {
            "tick_s": 2.0,
            "max_parallel_jobs": 2,
            "audit_every_s": None,
            "canary_every_s": None,
            "docker_timeout_s": 60,
            "supervise": False,
        },
        "governance": {"registry": str(registry), "git_timeout_s": 60},
        "foreman": {
            "runtime": "openai-sol@1",
            "model": "gpt-6.1-sol",
            "reviewers": [],
            "models": {},
            "image": image,
            "network": "egress",
            "session_timeout_s": 1200,
            "session_budget_usd": 1.0,
            "daily_budget_usd": 5.0,
            "max_turns": 8,
            "max_attempts": 2,
            "backoff_s": 30,
            "env": {},
            "mode": "resident",
        },
        "agents": {"image": image, "network": "egress", "poll_s": 2},
        "autonomy": {
            "mode": "standard",
            "changesets": {"T0": "human", "T1": "human", "T2": "human", "T3": "review"},
        },
    }
    (root / "factory.json").write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "scheduler.json").write_text(
        json.dumps(
            {
                "pools": {"cpu": None, "memory_mb": None, "gpu": None},
                "compartments": [],
                "breakers": [],
                "wip": [],
                "rates": [],
                "run_cap": None,
                "toxic_failures": None,
                "tick_s": None,
            }
        ),
        encoding="utf-8",
    )


def _egress(root: Path) -> None:
    folder = root / "egress"
    folder.mkdir(parents=True, exist_ok=True)
    upstream = {
        "gpt-6.1-sol": {
            "url": "https://openai.createsis.cn",
            "secret": "OPENAI_API_KEY",
            "header": "Authorization",
        }
    }
    prices = {"gpt-6.1-sol": {"input_per_mtok": 2.0, "output_per_mtok": 8.0}}
    (folder / "upstreams.json").write_text(json.dumps(upstream), encoding="utf-8")
    (folder / "prices.json").write_text(json.dumps(prices), encoding="utf-8")


def _secrets(root: Path) -> None:
    openai = os.environ.get("MAO_OPENAI_API_KEY", "")
    tbtk = os.environ.get("MAO_TBTK_API_KEY", "")
    autodl = os.environ.get("MAO_AUTODL_AK", "")
    missing = [
        name
        for name, value in (
            ("MAO_OPENAI_API_KEY", openai),
            ("MAO_TBTK_API_KEY", tbtk),
            ("MAO_AUTODL_AK", autodl),
        )
        if not value
    ]
    if missing:
        raise SystemExit("缺少环境变量：" + ", ".join(missing))
    _secret(root, "OPENAI_API_KEY", "model", "Bearer " + openai)
    _secret(root, "TBTK_API_KEY", "other", tbtk)
    _secret(root, "AUTODL_AK", "other", autodl)


def _secret(root: Path, name: str, kind: str, value: str) -> None:
    proc = _factory(root, ["secret", "set", name, "--class", kind, "--stdin"], input_text=value)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.stderr.write(proc.stdout)
        raise SystemExit(f"secret set {name} 失败")


def _commands(root: Path) -> None:
    # 脚本不能放在 /workspace、/tmp、/factory 下面：本地沙箱会把这些前缀改写成工位目录。
    bindir = root.parent / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    agent = bindir / "openai_agent.py"
    video = bindir / "minimax_h3.py"
    shutil.copy2(HERE / "bin" / "openai_agent.py", agent)
    shutil.copy2(HERE / "bin" / "minimax_h3.py", video)
    body = {
        "/opt/operator/bin/openai-agent": ["/usr/bin/python3", str(agent)],
        "minimax-h3": ["/usr/bin/python3", str(video)],
    }
    path = root.parent / "commands.json"
    path.write_text(json.dumps(body), encoding="utf-8")


def _epoch_missing(root: Path) -> bool:
    shown = _factory(root, ["epoch", "ls", "--json"])
    if shown.returncode != 0:
        return True
    try:
        found = json.loads(shown.stdout)
    except json.JSONDecodeError:
        return True
    return not found


def _factory(
    root: Path, args: list[str], *, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    commands = root.parent / "commands.json"
    cmd = [
        "uv",
        "run",
        "factory",
        "--root",
        str(root),
        "--provider",
        "local",
        "--local-commands",
        str(commands),
        *args,
    ]
    return subprocess.run(cmd, cwd=REPO, input=input_text, capture_output=True, text=True)


if __name__ == "__main__":
    sys.exit(main())
