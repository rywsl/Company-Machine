"""M5-02 验收：补齐的 factory 命令行（spec/system/commands.md 第 7 章）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.commands")

from mao.cli import main  # noqa: E402
from tests.acceptance.m2.world import FIXTURE  # noqa: E402
from tests.acceptance.m3.gov_world import (  # noqa: E402
    changeset_doc,
    commit_change,
    open_world,
    upper_v2_limits,
    upsert,
)

COMMANDS = [
    ["line", "plan"],
    ["line", "run"],
    ["line", "ls"],
    ["line", "show"],
    ["line", "nodes"],
    ["line", "cancel"],
    ["line", "retry"],
    ["line", "rebuild"],
    ["bean", "label"],
    ["trigger", "process"],
    ["cache", "ls"],
    ["cache", "explain"],
    ["cache", "invalidate"],
    ["approval", "ls"],
    ["approval", "show"],
    ["approval", "decide"],
    ["secret", "ls"],
    ["egress", "check"],
    ["contract", "validator"],
    ["contract", "golden"],
    ["contract", "equivalence"],
    ["contract", "diff"],
    ["contract", "sink-dry-run"],
    ["contract", "ls"],
    ["schedule", "tick"],
    ["schedule", "dlq", "ls"],
    ["schedule", "quarantine"],
    ["schedule", "release"],
    ["epoch", "init"],
    ["epoch", "ls"],
    ["epoch", "show"],
    ["epoch", "rollback"],
    ["epoch", "adopt"],
    ["changeset", "propose"],
    ["changeset", "ls"],
    ["changeset", "show"],
    ["changeset", "withdraw"],
    ["changeset", "advance"],
    ["governance", "tick"],
    ["governance", "safe-mode"],
    ["audit", "once"],
    ["audit", "ls"],
    ["drift", "ls"],
    ["foreman", "next"],
    ["foreman", "done"],
    ["foreman", "get"],
    ["foreman", "list"],
    ["foreman", "diff"],
    ["foreman", "bean", "get"],
    ["foreman", "bean", "lineage"],
    ["foreman", "job", "get"],
    ["foreman", "job", "transcript"],
    ["foreman", "dlq", "ls"],
    ["foreman", "drift", "ls"],
    ["foreman", "invariant", "ls"],
    ["foreman", "metrics"],
    ["foreman", "approval", "ls"],
    ["foreman", "changeset", "propose"],
    ["foreman", "changeset", "status"],
    ["foreman", "changeset", "withdraw"],
    ["foreman", "review"],
    ["foreman", "conversion", "ls"],
    ["maintain", "bootstrap"],
    ["gateway", "manifest"],
    ["backend", "status"],
    ["backend", "migrate"],
    ["backend", "verify"],
    ["mcp"],
]


def _commands_file(tmp_path: Path) -> Path:
    from tests.fakes.fakelib import local_commands

    path = tmp_path / "local-commands.json"
    path.write_text(json.dumps(local_commands()), encoding="utf-8")
    return path


def _base(tmp_path: Path, root: Path, registry: Path | None = None) -> list[str]:
    argv = ["--root", str(root), "--provider", "local", "--local-commands", str(_commands_file(tmp_path))]
    if registry is not None:
        argv += ["--registry", str(registry)]
    return argv


def _run(capsys, argv: list[str]) -> tuple[int, object]:
    capsys.readouterr()
    code = main(argv)
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else None)


def _import_task(capsys, base: list[str], tmp_path: Path) -> str:
    task = tmp_path / "task.json"
    task.write_bytes((FIXTURE / "fixtures" / "task" / "ok.json").read_bytes())
    code, out = _run(
        capsys, [*base, "--json", "bean", "import", "--type", "agent.task@1", "--file", f"task.json={task}"]
    )
    assert code == 0, out
    return out["id"]


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: " ".join(c))
def test_every_command_parses(command: list[str], capsys) -> None:
    assert main([*command, "--help"]) == 0
    assert capsys.readouterr().out.strip()


def test_options_before_or_after(tmp_path: Path, capsys) -> None:
    base = _base(tmp_path, tmp_path / "root", FIXTURE)
    code1, before = _run(capsys, [*base, "--json", "line", "plan", "note-pipeline@1"])
    code2, after = _run(capsys, [*base, "line", "plan", "note-pipeline@1", "--json"])
    assert code1 == code2 == 0
    assert before == after
    assert before["nodes"]


def test_line_run_show_nodes(tmp_path: Path, capsys) -> None:
    base = _base(tmp_path, tmp_path / "root", FIXTURE)
    task = _import_task(capsys, base, tmp_path)
    code, run = _run(capsys, [*base, "--json", "line", "run", "note-pipeline@1", "--in", f"task={task}"])
    assert code == 0, run
    assert run["state"] == "SUCCEEDED"
    code, shown = _run(capsys, [*base, "--json", "line", "show", run["id"]])
    assert code == 0 and shown["id"] == run["id"]
    code, nodes = _run(capsys, [*base, "--json", "line", "nodes", run["id"]])
    assert code == 0 and nodes and all(node["state"] == "DONE" for node in nodes)
    code, listed = _run(capsys, [*base, "--json", "line", "ls"])
    assert code == 0 and [item["id"] for item in listed] == [run["id"]]


def test_error_body_and_exit_code(tmp_path: Path, capsys) -> None:
    base = _base(tmp_path, tmp_path / "root", FIXTURE)
    code, out = _run(capsys, [*base, "--json", "schedule", "tick"])
    assert code == 1
    assert out["error"]["code"] == "SCHEDULER_CONFIG_MISSING"
    assert isinstance(out["error"]["message"], str)


def test_contract_validator_json(tmp_path: Path, capsys) -> None:
    base = _base(tmp_path, tmp_path / "root", FIXTURE)
    code, out = _run(
        capsys, [*base, "--json", "contract", "validator", "note.text@1", "count-text", "--timeout", "30"]
    )
    assert code == 0, out
    assert out["verdict"] == "pass"


def test_secret_ls_has_no_values(tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    (root / "secrets" / "values").mkdir(parents=True)
    (root / "secrets" / "manifest.json").write_text('{"TOOL_TOKEN": "other"}', encoding="utf-8")
    (root / "secrets" / "values" / "TOOL_TOKEN").write_text("CANARY-SECRET-VALUE-42", encoding="utf-8")
    base = _base(tmp_path, root, FIXTURE)
    capsys.readouterr()
    code = main([*base, "--json", "secret", "ls"])
    captured = capsys.readouterr()
    assert code == 0
    assert "TOOL_TOKEN" in captured.out
    assert "other" in captured.out
    assert "CANARY-SECRET-VALUE-42" not in captured.out + captured.err


def test_epoch_and_changeset(tmp_path: Path, capsys) -> None:
    w = open_world(tmp_path / "w")
    base = _base(tmp_path, w.ctx.root)
    code, epoch = _run(capsys, [*base, "--json", "epoch", "show"])
    assert code == 0 and epoch["commit"] == w.base
    source = commit_change(w.repo, upper_v2_limits())
    doc = tmp_path / "cs.json"
    doc.write_text(json.dumps(changeset_doc(w.base, [upsert("Station", "upper")])), encoding="utf-8")
    code, cs = _run(
        capsys, [*base, "--json", "changeset", "propose", str(doc), "--commit", source, "--by", "human:cli"]
    )
    assert code == 0, cs
    assert cs["state"] == "draft"
    code, _advanced = _run(capsys, [*base, "--json", "changeset", "advance", cs["id"]])
    assert code == 0
    code, shown = _run(capsys, [*base, "--json", "changeset", "show", cs["id"]])
    assert code == 0 and shown["state"] in {"verified", "rejected"}


def test_line_run_uses_active_epoch(tmp_path: Path, capsys) -> None:
    w = open_world(tmp_path / "w")
    base = _base(tmp_path, w.ctx.root)
    task = _import_task(capsys, base, tmp_path)
    code, run = _run(capsys, [*base, "--json", "line", "run", "note-pipeline@1", "--in", f"task={task}"])
    assert code == 0, run
    assert run["epoch"] == w.base


def test_foreman_readonly_queries(tmp_path: Path, capsys) -> None:
    w = open_world(tmp_path / "w")
    base = _base(tmp_path, w.ctx.root)
    code, wake = _run(capsys, [*base, "--json", "foreman", "next"])
    assert code == 0 and wake is None
    code, doc = _run(capsys, [*base, "--json", "foreman", "get", "Station:upper@1"])
    assert code == 0 and doc["kind"] == "Station"
    code, keys = _run(capsys, [*base, "--json", "foreman", "list", "Station"])
    assert code == 0 and "Station:upper@1" in keys
    source = commit_change(w.repo, upper_v2_limits())
    code, diff = _run(capsys, [*base, "--json", "foreman", "diff", w.base, source])
    assert code == 0 and "stations/upper.yaml" in diff["files"]


def test_foreman_identity_from_env(tmp_path: Path, capsys, monkeypatch) -> None:
    w = open_world(tmp_path / "w")
    base = _base(tmp_path, w.ctx.root)
    source = commit_change(w.repo, upper_v2_limits())
    doc = tmp_path / "cs.json"
    doc.write_text(json.dumps(changeset_doc(w.base, [upsert("Station", "upper")])), encoding="utf-8")
    monkeypatch.delenv("FACTORY_ACTOR", raising=False)
    code, _ = _run(
        capsys, [*base, "--json", "foreman", "changeset", "propose", "--doc", str(doc), "--commit", source]
    )
    assert code == 2
    monkeypatch.setenv("FACTORY_ACTOR", "fake-agent@1")
    code, _ = _run(
        capsys,
        [
            *base,
            "--json",
            "foreman",
            "review",
            "cs-0001",
            "--runtime",
            "script@1",
            "--verdict",
            "approve",
            "--note",
            "x",
        ],
    )
    assert code == 2
