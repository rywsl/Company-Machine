"""M11-09 验收：bypass 下厂长自己接入一个外部发布工位的完整演练（spec/system/delegation.md 第 7 章）。

厂长会话经 MCP 提议机器、策略、Sink 工位与产线；ChangeSet 不等复核，经影子与金丝雀推广。厂长申请秘密，
人填值并通过核验。产线每次运行时操作工请求验证码协助，人回答后发布，回执核验带秘密经出网代理访问上游。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("mao.handoff")

from tests.acceptance.m3.gov_world import ROLLOUT, upsert  # noqa: E402
from tests.acceptance.m5.auto_world import commit_actions, propose_action  # noqa: E402
from tests.acceptance.m11.deleg_world import IMAGE, Upstream, World  # noqa: E402


def onboarding_files(upstream: str) -> dict[str, str]:
    return {
        "machines/bilibox/machine.yaml": f"""apiVersion: mao/v1
kind: Machine
metadata: {{name: bilibox, version: 1}}
spec:
  form: tool
  runtime:
    image: {IMAGE}
    instance: per_job
    resources: {{cpus: 1, memory: 512Mi, shm_mb: 256, pids: 512}}
  needs:
    egress: none
    secrets: [BILI_COOKIE]
    secret_checks:
      BILI_COOKIE: {{run: [desk-check, BILI_COOKIE, "SESSDATA=ok"], timeout_s: 30}}
  recipes:
    upload:
      inputs:  {{note: {{type: note.text@1}}}}
      outputs: {{}}
      run: [desk-upload, "{upstream}"]
    upload-check:
      inputs:  {{note: {{type: note.text@1}}}}
      outputs: {{}}
      run: [desk-sink-check, "{upstream}/receipt", BILI_COOKIE]
""",
        "policies/bili-sink.yaml": """apiVersion: mao/v1
kind: Policy
metadata: {name: bili-sink, version: 1}
spec:
  applies_to: [sink]
  egress:
    publish: {hosts: [127.0.0.1], methods: ["*"]}
    on_unlisted: ask
  secrets: [BILI_COOKIE]
  handoff: {kinds: [captcha], timeout: 10m}
""",
        "stations/bili-upload.yaml": """apiVersion: mao/v1
kind: Station
metadata: {name: bili-upload, version: 1}
spec:
  role: sink
  machine: bilibox@1
  recipe: upload
  operator: {runtime: script@1}
  inputs:  {note: {type: note.text@1}}
  outputs: {}
  idempotency_key: "{{ inputs.note.id }}"
  verify_receipt: upload-check
  approval:
    in_run: {auto_if: 'request.kind == "egress" && request.summary.endsWith(".bilivideo.com")', else: foreman}
    timeout: 10m
  limits: {timeout: 15m, budget_usd: 0, retries: {rejected: 0, failed: 0, mode: fresh}}
  policy: bili-sink@1
""",
        "lines/bili-publish.yaml": """apiVersion: mao/v1
kind: Line
metadata: {name: bili-publish, version: 1}
spec:
  interface:
    inputs:
      note: {type: note.text@1, requires: [non_empty]}
  budget: {cap_usd: 1.0}
  steps:
    upload:
      station: bili-upload@1
      in: {note: $inputs.note}
""",
    }


CHANGES = [
    upsert("Machine", "bilibox"),
    upsert("Policy", "bili-sink"),
    upsert("Station", "bili-upload"),
    upsert("Line", "bili-publish"),
]
SECRET_REQUEST = {
    "do": "mcp",
    "calls": [
        {
            "name": "secret_request",
            "arguments": {
                "name": "BILI_COOKIE",
                "secret_class": "publish",
                "station": "bili-upload@1",
                "reason": "B 站投稿需要登录 Cookie",
            },
        }
    ],
}


def _run_with_captcha(w: World, text: str) -> dict:
    from mao.inbox import answer

    note = w.note(text)
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.update(out=w.cli("line", "run", "bili-publish@1", "--in", f"note={note}")),
        daemon=True,
    )
    worker.start()
    rows = w.wait("SELECT id FROM human_requests WHERE kind = 'handoff' AND status = 'open'", timeout=120)
    answer(w.ctx, str(rows[0]["id"]), {"text": "8k3d"}, by="human:alice")
    worker.join(180)
    assert not worker.is_alive()
    code, run, err = result["out"]
    assert code == 0, (run, err)
    return run


def test_bypass_onboarding_walkthrough(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue

    up = Upstream(tokens={"/receipt": "SESSDATA=ok"})
    w = World(tmp_path, autonomy={"mode": "bypass"})
    rollout = dict(ROLLOUT, canary={"percent": 1, "min_runs": 1})
    actions = commit_actions(onboarding_files(up.url), "onboard bilibili")
    w.script({"roles": {"foreman": [*actions, propose_action(CHANGES, rollout=rollout)]}})
    WakeQueue(w.ctx).file("other", "onboard-bilibili", {"note": "接入 B 站投稿"})
    sup = w.supervisor()
    sup.start()
    try:
        record = sup.run_once()
        assert record is not None and record.state == "ok" and len(record.proposed) == 1, record
        pipeline = w.pipeline()
        cs_id = record.proposed[0]
        for want in ("verified", "shadow", "canary"):
            cs = pipeline.advance(cs_id)
            assert cs.state == want, cs.report
        assert cs.risk_tier == "T3" and pipeline.approvals(cs_id) == []
        assert w.cli("inbox", "ls")[1] == []

        w.script({"roles": {"foreman": [SECRET_REQUEST]}})
        WakeQueue(w.ctx).file("other", "need-cookie", {"station": "bili-upload@1"})
        assert sup.run_once().state == "ok"
        [request] = w.cli("inbox", "ls")[1]
        assert (request["kind"], request["subject"]) == ("secret", "BILI_COOKIE")
        code, body, _ = w.cli("inbox", "answer", request["id"], "--secret-stdin", stdin="SESSDATA=ok\n")
        assert code == 0 and body["answer"]["verified"] is True, body

        for n in range(2):
            run = _run_with_captcha(w, f"video {n}\n")
            assert run["state"] == "SUCCEEDED" and run["epoch"] == cs.commit, run
        assert len([h for h in up.hits if h["path"] == "/receipt"]) == 2

        cs = pipeline.advance(cs_id)
        assert cs.state == "active", cs.report
        assert w.epochs.current() == cs.commit
        assert [
            item["status"] for item in w.cli("inbox", "ls", "--kind", "handoff", "--status", "answered")[1]
        ] == [
            "answered",
            "answered",
        ]
    finally:
        sup.shutdown()
        up.close()
