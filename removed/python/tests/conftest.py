"""全局 pytest 配置：标记与 Docker 门控（spec/system/README.md 第 8 节）。

- `docker` 标记的测试需要 Docker 守护进程。Docker 不可用时跳过；
  设置 MAO_REQUIRE_DOCKER=1 时改为失败（里程碑检验用它防止 Docker 测试被悄悄跳过）。
- `slow` 标记耗时超过 10 秒的测试。
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "docker: 需要 Docker 守护进程与测试镜像")
    config.addinivalue_line("markers", "slow: 耗时超过 10 秒")


@functools.lru_cache(maxsize=1)
def docker_available() -> bool:
    exe = shutil.which("docker")
    if exe is None:
        return False
    try:
        r = subprocess.run(
            [exe, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


@pytest.fixture(autouse=True)
def _docker_gate(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("docker") is None:
        return
    if docker_available():
        return
    if os.environ.get("MAO_REQUIRE_DOCKER") == "1":
        pytest.fail("设置了 MAO_REQUIRE_DOCKER=1，但 Docker 不可用（docker info 失败）")
    pytest.skip("Docker 不可用（设置 MAO_REQUIRE_DOCKER=1 可改为失败）")
