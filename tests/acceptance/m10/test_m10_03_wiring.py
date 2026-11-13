"""M10-03 验收：生产路径经 open_runner / open_line_runner 构造，产线运行查缓存（spec/system/wiring.md 5.3）。"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

pytest.importorskip("mao.daemon")

from tests.acceptance.m10.wiring_world import SCHEDULER, Cli, World  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


def test_line_run_reuses_cached_jobs(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    task = cli.task()
    first = cli.json("line", "run", "task-publish@1", "--in", f"task={task}")
    second = cli.json("line", "run", "task-publish@1", "--in", f"task={task}")
    assert first["state"] == "SUCCEEDED" and second["state"] == "SUCCEEDED", (first["error"], second["error"])
    assert first["spent_usd"] == pytest.approx(0.02)
    assert second["spent_usd"] == 0.0
    before, after = cli.nodes(first["id"]), cli.nodes(second["id"])
    cached = [node_id for node_id, node in before.items() if node_id != "publish"]
    assert "write" in cached and len(cached) >= 2
    for node_id in cached:
        assert after[node_id]["cache_hit"] is True, node_id
        assert after[node_id]["job_id"] == before[node_id]["job_id"], node_id
    assert after["publish"]["state"] == "DONE"


def test_no_cache_flag_runs_fresh(tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    task = cli.task()
    first = cli.json("line", "run", "task-publish@1", "--in", f"task={task}")
    fresh = cli.json("line", "run", "task-publish@1", "--in", f"task={task}", "--no-cache")
    assert fresh["state"] == "SUCCEEDED", fresh["error"]
    assert fresh["spent_usd"] == pytest.approx(0.02)
    write = cli.nodes(fresh["id"])["write"]
    assert write["cache_hit"] is False and write["job_id"] != cli.nodes(first["id"])["write"]["job_id"]
    third = cli.json("line", "run", "task-publish@1", "--in", f"task={task}")
    assert cli.nodes(third["id"])["write"]["cache_hit"] is True


def test_serve_tick_binds_cache_keys(tmp_path: Path) -> None:
    from mao.daemon import Daemon, InlineJobExecutor, ServeConfig
    from mao.runs import RUN_TERMINAL

    w = World(tmp_path)
    (w.ctx.root / "scheduler.json").write_text(json.dumps(SCHEDULER), encoding="utf-8")
    config = ServeConfig(
        tick_s=0.0,
        max_parallel_jobs=4,
        audit_every_s=None,
        canary_every_s=None,
        docker_timeout_s=60.0,
        supervise=False,
    )
    daemon = Daemon(w.ctx, config, executor=InlineJobExecutor(lambda job_id: w.runner), registry=w.registry)
    note = w.note()
    w.ctx.warehouse.label(note, "line", "note-publish", by="acceptance")
    daemon.start()
    triggered: list[str] = []
    try:
        for _ in range(20):
            triggered.extend(daemon.tick().triggered)
            rows = w.ctx.db.query("SELECT state FROM runs")
            if triggered and rows and all(row["state"] in {s.value for s in RUN_TERMINAL} for row in rows):
                break
    finally:
        daemon.shutdown(timeout_s=5)
    [run_id] = triggered
    served = {
        row["node_id"]: row for row in w.ctx.db.query("SELECT * FROM run_nodes WHERE run_id = ?", (run_id,))
    }
    assert served["publish"]["state"] == "DONE"

    cli = Cli(tmp_path, world=w)
    again = cli.json("line", "run", "note-publish@1", "--in", f"note={note}")
    assert again["state"] == "SUCCEEDED", again["error"]
    upper = cli.nodes(again["id"])["upper"]
    assert upper["cache_hit"] is True and upper["job_id"] == served["upper"]["job_id"]


def test_constructions_go_through_factories() -> None:
    allowed = {("runner.py", "open_runner", "Runner"), ("runs.py", "open_line_runner", "LineRunner")}
    bad = []
    for path in sorted((ROOT / "src" / "mao").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for top in tree.body:
            owner = top.name if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ""
            for node in ast.walk(top):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else ""
                )
                if name in {"Runner", "LineRunner"} and (path.name, owner, name) not in allowed:
                    bad.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}(...) in {owner or '<module>'}")
    assert bad == [], "\n".join(bad)
