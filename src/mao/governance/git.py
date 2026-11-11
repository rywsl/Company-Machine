"""治理面唯一允许调用 git 的地方。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from mao.errors import FactoryError


def git(repo: Path, *args: str, timeout_s: float = 60, env: dict[str, str] | None = None) -> str:
    command = ["git", "-C", str(repo), *args]
    merged = os.environ.copy()
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout_s,
            check=False,
            env=merged,
        )
    except subprocess.TimeoutExpired as exc:
        raise FactoryError("GIT_FAILED", "git 超时", args=list(command), stderr="") from exc
    if proc.returncode != 0:
        raise FactoryError(
            "GIT_FAILED",
            "git 失败",
            args=list(command),
            stderr=(proc.stderr or "")[:4000],
        )
    return proc.stdout


def git_bytes(repo: Path, *args: str, timeout_s: float = 60) -> bytes:
    command = ["git", "-C", str(repo), *args]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise FactoryError("GIT_FAILED", "git 超时", args=list(command), stderr="") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[:4000]
        raise FactoryError("GIT_FAILED", "git 失败", args=list(command), stderr=stderr)
    return proc.stdout
