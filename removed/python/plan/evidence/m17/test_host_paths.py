"""Deployment compatibility diagnostics, separate from the original 25-case baseline.

The host command runs directly, then must also run through LocalProvider while
its original virtual-path argument is translated. No historical script is used.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from mao.sandbox import LocalProvider, Mount, SandboxSpec


@pytest.mark.parametrize("mode", ["exec", "start"])
def test_mapped_host_script_and_virtual_argument(tmp_path: Path, mode: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "result.txt"
    with tempfile.TemporaryDirectory(prefix="m17-host-command-", dir="/tmp") as folder:
        script = Path(folder) / "adapter.py"
        script.write_text(
            "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('fixture-ran')\n",
            encoding="utf-8",
        )
        control = subprocess.run(
            [sys.executable, str(script), str(marker)],
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert control.returncode == 0 and marker.read_text() == "fixture-ran"
        marker.unlink()
        provider = LocalProvider(
            tmp_path / "factory", commands={"fixture-cli": [sys.executable, str(script)]}
        )
        spec = SandboxSpec(
            name="host-path-" + mode,
            image="unused",
            mounts=(Mount(source=str(workspace), target="/workspace", readonly=False),),
            command=("fixture-cli", "/workspace/result.txt") if mode == "start" else (),
        )
        sandbox = provider.open(spec)
        try:
            if mode == "exec":
                out, err = tmp_path / "stdout", tmp_path / "stderr"
                result = sandbox.exec(
                    ["fixture-cli", "/workspace/result.txt"], timeout_s=5, stdout=out, stderr=err
                )
                assert result.exit_code == 0, {
                    "exit_code": result.exit_code,
                    "stderr": err.read_text(encoding="utf-8"),
                    "host_script_exists": script.is_file(),
                }
            else:
                deadline = time.monotonic() + 2
                while not marker.exists() and sandbox.alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
            assert marker.is_file(), {"mode": mode, "host_script_exists": script.is_file()}
            assert marker.read_text() == "fixture-ran"
        finally:
            sandbox.close()
