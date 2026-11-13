"""M11-04 验收：出网主体与令牌、通配域名、按需放行与回执核验沙箱（spec/system/capabilities.md 第 7 章）。"""

from __future__ import annotations

import base64
import http.client
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("mao.delegation")

from tests.acceptance.m11.deleg_world import Upstream, World  # noqa: E402


def _statuses(w: World, job) -> list:
    return [item[1] for item in w.output(job)["results"]]


def _via_proxy(port: int, target: str, headers: dict[str, str]) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("GET", target, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
        return resp.status, json.loads(text).get("code", "") if text.startswith("{") else text
    finally:
        conn.close()


def test_job_reaches_upstream_with_token(tmp_path: Path) -> None:
    up = Upstream()
    try:
        w = World(tmp_path)
        url = f"{up.url}/data?x=1"
        job = w.run("desk-fetch@1", {"urls": url})
        assert job.state.value == "SEALED", job.error
        out = w.output(job)
        assert out["results"] == [[url, 200, "upstream-ok"]]
        assert out["user"] == job.id and out["has_token"] is True
        [hit] = up.hits
        sent = {key.lower() for key in hit["headers"]}
        assert "proxy-authorization" not in sent and "x-factory-job" not in sent
        [row] = w.ctx.db.query("SELECT token_sha256, kind FROM egress_plans WHERE job_id = ?", (job.id,))
        assert re.fullmatch(r"[0-9a-f]{64}", str(row["token_sha256"])) and row["kind"] == "job"
        assert {"job_id": job.id, "method": "GET", "host": "127.0.0.1"} in w.events("egress.allowed")
    finally:
        up.close()


def test_proxy_challenges_and_rejects(tmp_path: Path) -> None:
    up = Upstream()
    try:
        w = World(tmp_path)
        job = w.run("desk-probe@1", {"target": f"{up.url}/probe"})
        assert job.state.value == "SEALED", job.error
        out = w.output(job)
        assert out["ok"] == [200, "upstream-ok", ""]
        assert out["none"][:2] == [407, "EGRESS_AUTH_REQUIRED"] and out["none"][2].startswith("Basic")
        assert out["wrong"][:2] == [403, "EGRESS_AUTH_INVALID"]
        assert out["header_only"][:2] == [407, "EGRESS_AUTH_REQUIRED"]
        assert len(up.hits) == 1

        basic = "Basic " + base64.b64encode(f"{out['user']}:{out['token']}".encode()).decode()
        status, code = _via_proxy(w.ctx.proxies.egress_port, f"{up.url}/late", {"Proxy-Authorization": basic})
        assert (status, code) == (403, "EGRESS_JOB_INVALID")
        assert len(up.hits) == 1
    finally:
        up.close()


def test_wildcard_hosts(tmp_path: Path) -> None:
    w = World(tmp_path)
    urls = "http://a.example.invalid/x,http://a.b.example.invalid/y,http://example.invalid/z"
    job = w.run("desk-fetch@1", {"urls": urls})
    assert job.state.value == "SEALED", job.error
    assert _statuses(w, job) == [502, 502, 403]
    allowed = {e["host"] for e in w.events("egress.allowed") if e["job_id"] == job.id}
    assert allowed == {"a.example.invalid", "a.b.example.invalid"}
    denied = [e for e in w.events("egress.denied") if e["job_id"] == job.id]
    assert [(e["host"], e["code"]) for e in denied] == [("example.invalid", "EGRESS_DENIED")]
    for station in ("desk-badwild1@1", "desk-badwild2@1"):
        code, body, err = w.cli("egress", "check", station)
        assert code == 1 and w.error_code(body, err) == "EGRESS_CONFIG_INVALID", station


def test_unlisted_host_asks(tmp_path: Path) -> None:
    up = Upstream()
    try:
        w = World(tmp_path)
        urls = f"{up.url}/auto,http://denied.example.invalid/x,http://denied.example.invalid/again"
        bg = w.start("desk-asknet@1", {"urls": urls})
        approval = w.pending(bg.job_id)
        w.awaiting(bg.job_id)
        got, err = w.mcp("approval_get", {"approval_id": approval["id"]})
        assert err is None, err
        assert (got["kind"], got["route"]) == ("egress", "foreman")
        assert got["payload"]["summary"] == "GET denied.example.invalid"
        _, err = w.mcp(
            "approval_decide", {"approval_id": approval["id"], "decision": "deny", "note": "不认识"}
        )
        assert err is None, err
        job = bg.join()
        assert job.state.value == "SEALED", job.error
        results = w.output(job)["results"]
        assert [item[1] for item in results] == [200, 403, 403]
        assert "APPROVAL_DENIED" in results[1][2]
        rows = w.ctx.db.query(
            "SELECT status, decided_by FROM approvals WHERE job_id = ? ORDER BY id", (bg.job_id,)
        )
        assert [(r["status"], r["decided_by"]) for r in rows] == [
            ("approved", "auto"),
            ("denied", "foreman:fake-agent@1"),
        ]
        asked = [e["host"] for e in w.events("egress.asked") if e["job_id"] == bg.job_id]
        assert asked == ["127.0.0.1", "denied.example.invalid"]
        assert [hit["path"] for hit in up.hits] == ["/auto"]
    finally:
        up.close()


def test_unlisted_without_rule_is_denied(tmp_path: Path) -> None:
    up = Upstream()
    try:
        w = World(tmp_path)
        job = w.run("desk-asknorule@1", {"urls": f"{up.url}/x"})
        assert job.state.value == "SEALED", job.error
        [result] = w.output(job)["results"]
        assert result[1] == 403 and "APPROVAL_RULE_MISSING" in result[2]
        assert w.ctx.db.query("SELECT id FROM approvals WHERE job_id = ?", (job.id,)) == []

        plain = w.run("desk-fetch@1", {"urls": "http://other.invalid/x"})
        assert _statuses(w, plain) == [403]
        assert [e for e in w.events("egress.asked") if e["job_id"] == plain.id] == []
        assert up.hits == []
        code, body, err = w.cli("egress", "check", "desk-badask@1")
        assert code == 1 and w.error_code(body, err) == "EGRESS_CONFIG_INVALID"
    finally:
        up.close()


def test_receipt_verify_gets_secrets_and_proxy(tmp_path: Path) -> None:
    up = Upstream(tokens={"/receipt": "sink-secret"})
    try:
        w = World(tmp_path, upstream=up.url, secrets={"SINK_TOKEN": ("publish", "sink-secret")})
        job = w.run("desk-sink@1")
        assert job.state.value == "SEALED", job.error
        [hit] = [h for h in up.hits if h["path"] == "/receipt"]
        assert hit["headers"].get("X-Token") == "sink-secret"
        assert "proxy-authorization" not in {key.lower() for key in hit["headers"]}
        [row] = w.ctx.db.query(
            "SELECT kind, closed_at FROM egress_plans WHERE job_id = ?", (f"{job.id}-verify-1",)
        )
        assert row["kind"] == "check" and row["closed_at"] is not None
        rows = w.ctx.db.query("SELECT status FROM receipts WHERE job_id = ?", (job.id,))
        assert [r["status"] for r in rows] == ["verified"]
    finally:
        up.close()
