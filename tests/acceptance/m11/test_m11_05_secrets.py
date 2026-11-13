"""M11-05 验收：秘密申请、核验后保存、secret set/verify（spec/system/inbox.md 第 7 章）。"""

from __future__ import annotations

import base64
import http.client
import json
from pathlib import Path

import pytest

pytest.importorskip("mao.secrets")

from tests.acceptance.m11.deleg_world import Upstream, World  # noqa: E402

DESK = {"name": "DESK_TOKEN", "secret_class": "other", "station": "desk-secret@1", "reason": "需要令牌"}


def _request(w: World, **override) -> tuple:
    return w.mcp("secret_request", dict(DESK, **override))


def _manifest(w: World) -> dict:
    return json.loads((w.root / "secrets" / "manifest.json").read_text(encoding="utf-8"))


def _texts(w: World) -> str:
    """数据库里的事件、收件箱，加上 inbox/ 下的全部文件（不含 secrets/）。"""
    rows = [r["data"] for r in w.ctx.db.query("SELECT data FROM events")]
    rows += [json.dumps(dict(r)) for r in w.ctx.db.query("SELECT * FROM human_requests")]
    for path in (w.root / "inbox").rglob("*"):
        if path.is_file():
            rows.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(rows)


def test_request_checks(tmp_path: Path) -> None:
    w = World(tmp_path)
    _, err = _request(w, name="OTHER_TOKEN")
    assert err["code"] == "SECRET_CHECK_MISSING" and err["detail"]["name"] == "OTHER_TOKEN"
    for override, reason in (
        ({"name": "UP_TOKEN"}, "not-bound"),
        ({"secret_class": "publish"}, "role"),
        ({"secret_class": "model"}, "class"),
        ({"name": "bad-name"}, "name"),
        ({"station": "nope@1"}, "station"),
    ):
        _, err = _request(w, **override)
        assert err["code"] == "SECRET_REQUEST_INVALID" and err["detail"]["reason"] == reason, override
    assert w.cli("inbox", "ls")[1] == []

    req, err = _request(w)
    assert err is None, err
    assert (req["kind"], req["subject"], req["summary"]) == ("secret", "DESK_TOKEN", "需要令牌")
    assert req["payload"] == {
        "name": "DESK_TOKEN",
        "class": "other",
        "station": "desk-secret@1",
        "check": {"timeout_s": 30},
        "exists": False,
        "rejected": 0,
    }
    again, _ = _request(w)
    assert again["id"] == req["id"]


def test_secret_fulfil_verifies_before_store(tmp_path: Path) -> None:
    w = World(tmp_path)
    req, _ = _request(w)
    bad = tmp_path / "bad.txt"
    bad.write_text("wrong-token\n", encoding="utf-8")
    code, body, err = w.cli("inbox", "answer", req["id"], "--secret-file", str(bad))
    assert code == 1 and w.error_code(body, err) == "SECRET_INVALID"
    assert not (w.root / "secrets" / "values" / "DESK_TOKEN").exists()
    shown = w.cli("inbox", "show", req["id"])[1]
    assert shown["status"] == "open" and shown["payload"]["rejected"] == 1
    assert w.wakes("inbox") == []

    code, body, _ = w.cli("inbox", "answer", req["id"], "--secret-stdin", stdin="good-token\n")
    assert code == 0, body
    assert (body["status"], body["answer"]) == ("answered", {"verified": True, "check": 2})
    assert (w.root / "secrets" / "values" / "DESK_TOKEN").read_text(encoding="utf-8") == "good-token"
    assert _manifest(w)["DESK_TOKEN"] == "other"
    assert [wake["subject"] for wake in w.wakes("inbox")] == [req["id"]]
    assert w.events("secret.rejected") == [
        {"name": "DESK_TOKEN", "request": req["id"], "code": "SECRET_INVALID", "exit_code": 1}
    ]
    assert w.events("secret.stored") == [
        {"name": "DESK_TOKEN", "class": "other", "by": "human:cli", "request": req["id"]}
    ]
    texts = _texts(w)
    assert "wrong-token" not in texts and "good-token" not in texts


def test_check_reaches_upstream_with_candidate(tmp_path: Path) -> None:
    up = Upstream(tokens={"/check": "up-good"})
    try:
        w = World(tmp_path, upstream=up.url)
        req, err = _request(w, name="UP_TOKEN", station="desk-upcheck@1", reason="上游令牌")
        assert err is None, err
        code, body, _ = w.cli("inbox", "answer", req["id"], "--secret-stdin", stdin="up-good")
        assert code == 0 and body["answer"]["verified"] is True, body
        [hit] = [h for h in up.hits if h["path"] == "/check"]
        assert hit["headers"].get("X-Token") == "up-good"
        assert "proxy-authorization" not in {key.lower() for key in hit["headers"]}

        label = f"{req['id']}-check-1"
        [row] = w.ctx.db.query("SELECT kind, closed_at FROM egress_plans WHERE job_id = ?", (label,))
        assert row["kind"] == "check" and row["closed_at"] is not None
        creds = json.loads(
            (w.root / "inbox" / req["id"] / "check-1" / "workspace" / "creds.json").read_text()
        )
        assert creds["user"] == label
        basic = "Basic " + base64.b64encode(f"{creds['user']}:{creds['token']}".encode()).decode()
        from mao.egress import EgressProxy, SecretStore, plans_from_db

        proxy = EgressProxy(
            plans_from_db(w.ctx.db), SecretStore(w.root), w.ctx.clock, db=w.ctx.db, root=w.root
        )
        port = proxy.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            conn.request(
                "GET", f"{up.url}/check", headers={"Proxy-Authorization": basic, "X-Token": "up-good"}
            )
            resp = conn.getresponse()
            assert resp.status == 403 and json.loads(resp.read())["code"] == "EGRESS_JOB_INVALID"
            conn.close()
        finally:
            proxy.close()
    finally:
        up.close()


def test_secret_set_and_verify_commands(tmp_path: Path) -> None:
    w = World(tmp_path)
    bad, good = tmp_path / "bad", tmp_path / "good"
    bad.write_text("nope", encoding="utf-8")
    good.write_text("good-token", encoding="utf-8")
    base = ("secret", "set", "DESK_TOKEN", "--class", "other", "--check", "desk-secret@1")
    code, body, err = w.cli(*base, "--file", str(bad))
    assert code == 1 and w.error_code(body, err) == "SECRET_INVALID"
    assert not (w.root / "secrets" / "values" / "DESK_TOKEN").exists()
    code, body, _ = w.cli(*base, "--file", str(good))
    assert code == 0 and body == {"name": "DESK_TOKEN", "class": "other", "verified": True}

    code, body, _ = w.cli("secret", "verify", "DESK_TOKEN", "--station", "desk-secret@1")
    assert code == 0 and body == {"name": "DESK_TOKEN", "ok": True, "code": "", "exit_code": 0}
    got, err = w.mcp("secret_verify", {"name": "DESK_TOKEN", "station": "desk-secret@1"})
    assert err is None and got == {"name": "DESK_TOKEN", "ok": True, "code": "", "exit_code": 0}

    w.put_secret("DESK_TOKEN", "other", "expired-token")
    code, body, err = w.cli("secret", "verify", "DESK_TOKEN", "--station", "desk-secret@1")
    assert code == 1 and w.error_code(body, err) == "SECRET_INVALID"
    got, err = w.mcp("secret_verify", {"name": "DESK_TOKEN", "station": "desk-secret@1"})
    assert err is None and got == {
        "name": "DESK_TOKEN",
        "ok": False,
        "code": "SECRET_INVALID",
        "exit_code": 1,
    }
    assert "expired-token" not in json.dumps(got)

    code, body, _ = w.cli("secret", "set", "OTHER_TOKEN", "--class", "other", "--stdin", stdin="plain\n")
    assert code == 0 and body == {"name": "OTHER_TOKEN", "class": "other", "verified": False}
    assert (w.root / "secrets" / "values" / "OTHER_TOKEN").read_text(encoding="utf-8") == "plain"


def test_json_secret_is_compacted(tmp_path: Path) -> None:
    w = World(tmp_path)
    req, err = _request(w, name="JSON_TOKEN", reason="浏览器登录状态")
    assert err is None, err
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"cookies": [{"name": "SESSDATA", "value": "abc"}]}, indent=2), encoding="utf-8"
    )
    code, body, _ = w.cli("inbox", "answer", req["id"], "--secret-file", str(state))
    assert code == 0 and body["answer"]["verified"] is True, body
    stored = (w.root / "secrets" / "values" / "JSON_TOKEN").read_text(encoding="utf-8")
    assert stored == '{"cookies":[{"name":"SESSDATA","value":"abc"}]}'

    code, body, err = w.cli(
        "secret", "set", "OTHER_TOKEN", "--class", "other", "--stdin", stdin="line one\nline two\n"
    )
    assert code == 1 and w.error_code(body, err) == "SECRET_VALUE_INVALID"
    assert body["error"]["detail"]["reason"] == "newline"
