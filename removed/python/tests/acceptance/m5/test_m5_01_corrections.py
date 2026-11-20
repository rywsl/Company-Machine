"""M5-01 验收：M1–M4 纠偏（spec/system/corrections.md 第 7 章，plan/drift.md）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("mao.governance")
pytest.importorskip("mao.reconcile")
pytest.importorskip("mao.egress")

from tests.acceptance.m2.world import EXAMPLE, FIXTURE, line_inputs, open_factory, seal_input  # noqa: E402
from tests.acceptance.m3.gov_world import (  # noqa: E402
    changeset_doc,
    commit_change,
    fixture_text,
    insert_run,
    open_world,
    upper_v2_limits,
    upsert,
)

NOTE_TO_TEXT_V2_FALSE = """apiVersion: mao/v1
kind: Station
metadata: {name: note-to-text, version: 2}
spec:
  role: converter
  converts: {from: note.raw@1, to: note.text@1, lossless: true, preserves: [non_empty]}
  machine: toolbox@1
  recipe: note-to-text
  operator: {runtime: script@1}
  inputs:  {in: {type: note.raw@1}}
  outputs: {out: {type: note.text@1, guarantees: [non_empty]}}
  equivalence:
    - "false"
  fixtures:
    accept: [fixtures/note/ok.txt]
    reject: [fixtures/note/empty.txt]
  limits: {timeout: 60s, budget_usd: 0, retries: {rejected: 0, failed: 0, mode: fresh}}
  policy: converter-default@1
"""


class _PassResult:
    def __init__(self, n: int) -> None:
        self.id = f"ct-{n:04d}"
        self.kind = "fake"
        self.subject = "fake"
        self.verdict = "pass"
        self.detail: dict = {}
        self.at = "2026-11-10T00:00:00.000Z"


class _PassContracts:
    """只在需要先走到 verified 的测试里用；被测的缺省组件不用它。"""

    def __init__(self) -> None:
        self.n = 0

    def __getattr__(self, name: str):
        def call(*args, **kwargs):
            self.n += 1
            return _PassResult(self.n)

        return call


def _post_model(port: int, job_id: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=json.dumps({"model": "meter-model"}).encode(),
        headers={"X-Factory-Job": job_id, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_snapshot_keys() -> None:
    from mao.snapshot import Snapshot

    snapshot = Snapshot.load(FIXTURE)
    stations = snapshot.keys("Station")
    assert stations == sorted(stations)
    assert "Station:upper@1" in stations
    assert all(key.startswith("Station:") for key in stations)
    everything = snapshot.keys()
    assert {key.split(":", 1)[0] for key in everything} >= {"Station", "Line", "Type", "Machine", "Policy"}
    assert snapshot.keys("NoSuchKind") == []
    stations.clear()
    assert snapshot.keys("Station")


def test_proxy_requires_registered_cap(tmp_path: Path) -> None:
    from mao.base import FakeClock
    from mao.egress import ModelProxy, SecretStore

    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    station = snapshot.key("Station", "explainer@3")
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at) "
            "VALUES ('j-nocap', ?, ?, 'RUNNING', 1, '{}', '{}', '{}', ?, ?)",
            (station, snapshot.epoch, "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
        )
    secrets = tmp_path / "secrets-root"
    (secrets / "secrets" / "values").mkdir(parents=True)
    (secrets / "secrets" / "manifest.json").write_text("{}", encoding="utf-8")
    proxy = ModelProxy(SecretStore(secrets), FakeClock(), ctx.root)
    port = proxy.start(port=0)
    try:
        status, body = _post_model(port, "j-nocap")
    finally:
        proxy.close()
    assert status == 403
    assert body["code"] == "EGRESS_JOB_INVALID"


def test_runner_registers_cap(tmp_path: Path) -> None:
    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    station = snapshot.key("Station", "upper@1")
    note = seal_input(ctx, snapshot, "note.text@1")
    job = runner.submit(station, {"note": note}, {})
    runner.run(job.id)
    rows = ctx.db.query("SELECT station, cap_usd FROM model_caps WHERE job_id = ?", (job.id,))
    assert len(rows) == 1
    assert rows[0]["station"] == station
    assert rows[0]["cap_usd"] == float(snapshot.spec(station)["limits"]["budget_usd"])


def test_pipeline_defaults_installed(tmp_path: Path) -> None:
    from mao.governance import ChangePipeline, LineShadowRunner

    w = open_world(tmp_path)
    pipeline = ChangePipeline(w.ctx, w.epochs)
    assert callable(pipeline.contracts)
    assert callable(pipeline.rebuild)
    assert isinstance(pipeline.shadow, LineShadowRunner)


def test_v4_runs_by_default(tmp_path: Path) -> None:
    from mao.governance import ChangePipeline

    w = open_world(tmp_path)
    pipeline = ChangePipeline(w.ctx, w.epochs)
    source = commit_change(w.repo, {"stations/note-to-text-2.yaml": NOTE_TO_TEXT_V2_FALSE})
    cs = pipeline.propose(
        changeset_doc(w.base, [upsert("Station", "note-to-text")]),
        source,
        by="human:acceptance",
        timeout_s=60,
    )
    done = pipeline.advance(cs.id)
    assert done.state == "rejected"
    assert done.report["code"] == "V4_FAILED"


def test_shadow_dry_run_does_not_publish(tmp_path: Path) -> None:
    from mao.governance import ChangePipeline, LineShadowRunner
    from mao.runner import Runner
    from mao.runs import InlineExecutor, LineRunner, RunState

    w = open_world(tmp_path)
    runner = Runner(w.ctx, w.snapshot)
    lines = LineRunner(w.ctx, w.snapshot, runner)
    run = lines.start_run("note-pipeline@1", line_inputs(w.ctx, w.snapshot, "note-pipeline@1"))
    base = lines.run_to_completion(run.id, InlineExecutor(runner))
    assert base.state == RunState.SUCCEEDED
    receipts_before = len(w.ctx.db.query("SELECT id FROM receipts"))
    assert receipts_before >= 1

    fake = _PassContracts()
    pipeline = ChangePipeline(w.ctx, w.epochs, contracts=lambda snapshot: fake)
    source = commit_change(w.repo, upper_v2_limits())
    cs = pipeline.propose(
        changeset_doc(w.base, [upsert("Station", "upper")]), source, by="human:acceptance", timeout_s=60
    )
    cs = pipeline.advance(cs.id)
    assert cs.state == "verified"
    report = LineShadowRunner(w.ctx).run(
        cs,
        candidate=w.epochs.materialize(cs.commit),
        active=w.snapshot,
        rollout={"shadow": {"runs": 1, "budget_usd": 5.0}},
        golden_only=False,
    )
    assert report.ok is True
    assert report.runs == 1
    assert report.regressions == []
    assert len(w.ctx.db.query("SELECT id FROM receipts")) == receipts_before
    skipped = w.ctx.db.query("SELECT data FROM events WHERE kind = 'run.sink_skipped'")
    assert skipped
    shadow_runs = w.ctx.db.query("SELECT sink_mode FROM runs WHERE id != ?", (run.id,))
    assert [row["sink_mode"] for row in shadow_runs] == ["dry-run"]


def test_reconciler_i8_not_permanent(tmp_path: Path) -> None:
    from mao.reconcile import Reconciler

    w = open_world(tmp_path)
    findings = Reconciler(w.ctx, w.epochs).audit_once(timeout_s=60)
    assert [f for f in findings if f.invariant == "I8"] == []


def test_reconciler_i10_i12(tmp_path: Path) -> None:
    from mao.reconcile import Reconciler

    w = open_world(tmp_path)
    at = "2026-11-10T00:00:00.000Z"
    insert_run(w.ctx, "r-0100", epoch=w.base, state="SUCCEEDED", created_at=at, spent_usd=6.0)
    insert_run(w.ctx, "r-0101", epoch="f" * 40, state="RUNNING", created_at=at)
    findings = Reconciler(w.ctx, w.epochs).audit_once(timeout_s=60)
    over = [f for f in findings if f.invariant == "I10"]
    assert any(f.detail.get("run") == "r-0100" for f in over)
    assert not any(f.detail.get("run") != "r-0100" for f in over)
    missing = [f for f in findings if f.invariant == "I12"]
    assert any(f.detail.get("commit") == "f" * 40 for f in missing)


def test_drift_ignores_working_tree(tmp_path: Path) -> None:
    from mao.reconcile import Reconciler

    w = open_world(tmp_path)
    # 只写进工作区、不提交、不提议：这不是 active epoch 的一部分
    loose = (
        fixture_text("baselines/upper-baseline.yaml")
        .replace("name: upper-baseline", "name: loose-baseline")
        .replace("cost_usd: {max: 1.0}", "judge_score: {max: 1.0}")
    )
    assert "judge_score" in loose
    (w.repo / "baselines" / "loose-baseline.yaml").write_text(loose, encoding="utf-8", newline="\n")
    findings = Reconciler(w.ctx, w.epochs).audit_once(timeout_s=60)
    drift = [f for f in findings if f.invariant == "drift"]
    assert not any(f.detail.get("metric") == "judge_score" for f in drift)
    assert not any("loose-baseline" in json.dumps(f.detail, ensure_ascii=False) for f in findings)
