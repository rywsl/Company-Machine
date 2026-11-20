"""M11-07 验收：厂长写的监控规则——挂起交厂长审批、拒绝、唤醒与 kernel 编译（spec/system/capabilities.md 第 7 章）。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("mao.delegation")

from tests.acceptance.m11.deleg_world import STATIONS, Upstream, World  # noqa: E402


def _decide(w: World, approval_id: str, decision: str) -> None:
    _, err = w.mcp("approval_decide", {"approval_id": approval_id, "decision": decision, "note": ""})
    assert err is None, err


def test_monitor_hold_waits_for_foreman(tmp_path: Path) -> None:
    up = Upstream()
    try:
        w = World(tmp_path)
        bg = w.start("desk-monitor-hold@1", {"urls": f"{up.url}/one,{up.url}/two"})
        approval = w.pending(bg.job_id)
        w.awaiting(bg.job_id)
        got, err = w.mcp("approval_get", {"approval_id": approval["id"]})
        assert err is None, err
        assert (got["kind"], got["route"]) == ("monitor:hold-up", "foreman")
        assert got["payload"]["summary"] == "GET 127.0.0.1"
        hit = {"job_id": bg.job_id, "monitor": "hold-up", "action": "hold", "summary": "GET 127.0.0.1"}
        assert w.events("monitor.hit") == [hit]
        assert up.hits == []
        _decide(w, approval["id"], "approve")
        job = bg.join()
        assert [item[1] for item in w.output(job)["results"]] == [200, 200]
        assert len(w.ctx.db.query("SELECT id FROM approvals WHERE job_id = ?", (bg.job_id,))) == 1
        assert len(w.events("monitor.hit")) == 1
    finally:
        up.close()


def test_monitor_deny_and_wake(tmp_path: Path) -> None:
    up = Upstream()
    try:
        w = World(tmp_path)
        job = w.run("desk-monitor-deny@1", {"urls": f"{up.url}/x"})
        [result] = w.output(job)["results"]
        assert result[1] == 403 and "MONITOR_DENIED" in result[2]
        assert up.hits == []
        woke = w.run("desk-monitor-wake@1", {"urls": f"{up.url}/a,{up.url}/b"})
        assert [item[1] for item in w.output(woke)["results"]] == [200, 200]
        [wake] = w.wakes("monitor")
        assert wake["subject"] == f"{woke.id}:ping"
        assert json.loads(wake["payload"]) == {"job": woke.id, "monitor": "ping", "summary": "GET 127.0.0.1"}
    finally:
        up.close()


def test_monitor_hold_denied_or_timeout(tmp_path: Path) -> None:
    up = Upstream()
    try:
        w = World(tmp_path)
        bg = w.start("desk-monitor-hold@1", {"urls": f"{up.url}/one,{up.url}/two"})
        _decide(w, w.pending(bg.job_id)["id"], "deny")
        results = w.output(bg.join())["results"]
        assert [item[1] for item in results] == [403, 403]
        assert all("MONITOR_DENIED" in item[2] for item in results)

        w.set_config("autonomy", {"mode": "bypass"})
        bg2 = w.start("desk-monitor-hold@1", {"urls": f"{up.url}/three"})
        held = w.pending(bg2.job_id)
        assert held["route"] == "foreman"
        w.clock.advance(601)
        [result] = w.output(bg2.join())["results"]
        assert result[1] == 403 and "MONITOR_DENIED" in result[2]
        row = w.ctx.db.query("SELECT status, decided_by FROM approvals WHERE id = ?", (held["id"],))[0]
        assert (row["status"], row["decided_by"]) == ("denied", "timeout")
        assert up.hits == []
    finally:
        up.close()


def test_monitor_compiles_in_kernel(tmp_path: Path) -> None:
    w = World(tmp_path)
    registry = tmp_path / "check"
    shutil.copytree(w.repo, registry, ignore=shutil.ignore_patterns(".git"))
    path = registry / "stations" / "desk-monitor-hold.yaml"
    good = STATIONS["desk-monitor-hold"]
    path.write_text(good.replace("request.summary ==", "request.nope =="), encoding="utf-8", newline="\n")
    from mrl.factory.check import check_registry

    diags = [d.to_json() for d in check_registry(registry).diags]
    assert any(d["code"] == "E-EXPR" and "spec.monitors.hold-up.when" in json.dumps(d) for d in diags), diags

    path.write_text(good.replace("on: egress", "on: log"), encoding="utf-8", newline="\n")
    code, body, _ = w.cli("check", str(registry))
    assert code == 3 and "monitors" in json.dumps(body["error"]["detail"]["diags"])
    path.write_text(good, encoding="utf-8", newline="\n")
    assert w.cli("check", str(registry))[0] == 0
