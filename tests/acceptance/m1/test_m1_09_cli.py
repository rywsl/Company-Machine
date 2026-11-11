"""M1-09 验收：factory 命令行（spec/system/cli.md 第 7 章）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.cli")

import mao  # noqa: E402
from mao.cli import build_parser, main  # noqa: E402
from tests.fakes.fakelib import local_commands, render_registry  # noqa: E402


def _setup(tmp_path: Path) -> list[str]:
    registry = render_registry(tmp_path / "registry")
    commands = tmp_path / "commands.json"
    commands.write_text(json.dumps(local_commands()), encoding="utf-8")
    return [
        "--root", str(tmp_path / "factory"),
        "--registry", str(registry),
        "--provider", "local",
        "--local-commands", str(commands),
    ]  # fmt: skip


def _import_note(tmp_path: Path, base: list[str], text: str, capsys) -> str:
    src = tmp_path / "in" / "note.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(text, encoding="utf-8")
    capsys.readouterr()
    assert main([*base, "bean", "import", "--type", "note.text@1", "--file", f"note.txt={src}"]) == 0
    bean_id = capsys.readouterr().out.strip()
    assert bean_id.startswith("sha256:")
    return bean_id


def test_usage_exit_2(tmp_path: Path) -> None:
    assert main([]) == 2
    assert main(["job"]) == 2
    assert main(["--provider", "podman", "check", str(tmp_path)]) == 2
    commands = tmp_path / "c.json"
    commands.write_text("{}", encoding="utf-8")
    assert main(["--provider", "docker", "--local-commands", str(commands), "check", str(tmp_path)]) == 2


def test_check_invalid_registry(tmp_path: Path) -> None:
    good = render_registry(tmp_path / "good")
    assert main(["check", str(good)]) == 0
    bad = render_registry(tmp_path / "bad", patches=[("stations/upper.yaml", "spec.machine", "nope@1")])
    assert main(["check", str(bad)]) == 3


def test_job_run_sealed(tmp_path: Path, capsys) -> None:
    base = _setup(tmp_path)
    note = _import_note(tmp_path, base, "hello\n", capsys)
    assert main([*base, "job", "run", "upper@1", "--input", f"note={note}"]) == 0
    port, out_id = capsys.readouterr().out.strip().split("\t")
    assert port == "note"
    assert out_id.startswith("sha256:") and out_id != note
    assert main([*base, "--json", "job", "run", "upper@1", "--input", f"note={note}"]) == 0
    job = json.loads(capsys.readouterr().out)
    assert job["state"] == "SEALED"
    assert main([*base, "--json", "job", "show", job["id"]]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["state"] == "SEALED"
    assert shown["outputs"]["note"]


def test_job_run_dead(tmp_path: Path, capsys) -> None:
    base = _setup(tmp_path)
    note = _import_note(tmp_path, base, "#fail\n", capsys)
    assert main([*base, "job", "run", "upper@1", "--input", f"note={note}"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "OPERATOR_FAILED" in captured.err
    assert main([*base, "--json", "job", "run", "upper@1", "--input", f"note={note}"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "OPERATOR_FAILED"


def test_bean_lineage_json(tmp_path: Path, capsys) -> None:
    base = _setup(tmp_path)
    note = _import_note(tmp_path, base, "hello\n", capsys)
    assert main([*base, "job", "run", "upper@1", "--input", f"note={note}"]) == 0
    out_id = capsys.readouterr().out.strip().split("\t")[1]
    assert main([*base, "--json", "bean", "lineage", out_id, "--direction", "up"]) == 0
    lineage = json.loads(capsys.readouterr().out)
    assert lineage["beans"] == [note]


def test_default_provider_is_docker() -> None:
    args = build_parser().parse_args(["check", "registry"])
    assert args.provider == "docker"


def test_version(capsys) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == mao.__version__
