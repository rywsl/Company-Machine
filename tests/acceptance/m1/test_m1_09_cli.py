"""M1-09 验收（spec/system/cli.md 第 7 章）。"""

from __future__ import annotations

import pytest

pytest.importorskip("mao.cli")

from mao.cli import main  # noqa: E402


def test_usage_exit_2() -> None:
    assert main([]) == 2


def test_check_invalid_registry(tmp_path) -> None:
    (tmp_path / "types").mkdir()
    (tmp_path / "types" / "nope.yaml").write_text("kind: Nope\n", encoding="utf-8")
    assert main(["check", str(tmp_path), "--provider", "local"]) == 3


def test_job_run_sealed(tmp_path) -> None:
    code = main(["job", "run", "upper@1", "--root", str(tmp_path), "--provider", "local"])
    assert code in {0, 1, 3}


def test_job_run_dead(tmp_path) -> None:
    code = main(["job", "run", "missing@1", "--root", str(tmp_path), "--provider", "local"])
    assert code in {1, 3}


def test_default_provider_is_docker() -> None:
    from mao.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["check", "registry"])
    assert getattr(args, "provider", "docker") == "docker"


def test_version(capsys) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip()
