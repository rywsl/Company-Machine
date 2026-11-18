"""受限外部操作邮箱（spec/system/operability.md 第 3.4 节）。"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from mao.base import SystemTokens, canonical_json, utc_iso
from mao.errors import FactoryError

_ALLOWED = {
    "guide",
    "capabilities",
    "doctor",
    "status",
    "events",
    "agent",
    "talk",
    "objective",
    "line",
    "job",
    "changeset",
    "inbox",
    "audit",
    "receipt",
    "operation",
}
_FORBIDDEN = {"authorize", "resume", "revoke", "approve", "apply", "init", "run", "retry"}


def issue_credential(ctx: Any, subject: str, expires_at: str, out: Path) -> dict[str, Any]:
    path = Path(out)
    if path.exists():
        raise FactoryError("OPERATION_FORBIDDEN", "凭据文件已存在", field="out")
    tokens = ctx.tokens or SystemTokens()
    secret = tokens.token_hex(32)
    if len(secret) < 64:
        raise FactoryError("OPERATION_FORBIDDEN", "秘密太短", field="secret")
    credential_id = ctx.ids.new("ocred")
    endpoint = ctx.root / "operations" / credential_id
    (endpoint / "requests").mkdir(parents=True)
    (endpoint / "responses").mkdir(parents=True)
    private = ctx.root / "private" / "operation-credentials"
    private.mkdir(parents=True, exist_ok=True)
    key_path = private / f"{credential_id}.key"
    key_path.write_text(secret, encoding="utf-8")
    body = {
        "id": credential_id,
        "root_id": str(ctx.root),
        "subject": subject,
        "secret": secret,
        "endpoint": str(endpoint),
        "expiry": expires_at,
    }
    path.write_text(canonical_json(body), encoding="utf-8")
    now = utc_iso(ctx.clock.now())
    with ctx.db.tx() as conn:
        conn.execute(
            """INSERT INTO operation_credentials (
                   id, token_digest, root_id, subject, scope, secret_ref, expires_at, revoked_at, created_at
               ) VALUES (?, ?, ?, ?, 'operate', ?, ?, NULL, ?)""",
            (
                credential_id,
                hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                str(ctx.root),
                subject,
                str(key_path),
                expires_at,
                now,
            ),
        )
    return {"credential_id": credential_id, "endpoint": str(endpoint)}


def revoke_credential(ctx: Any, credential_id: str) -> dict[str, Any]:
    now = utc_iso(ctx.clock.now())
    with ctx.db.tx() as conn:
        row = conn.execute("SELECT id FROM operation_credentials WHERE id = ?", (credential_id,)).fetchone()
        if row is None:
            raise FactoryError("OPERATION_FORBIDDEN", "凭据不存在", id=credential_id)
        conn.execute(
            "UPDATE operation_credentials SET revoked_at = ? WHERE id = ?",
            (now, credential_id),
        )
    return {"credential_id": credential_id, "revoked_at": now}


def submit_operation(
    credential_file: Path, command: list[str], *, request_key: str | None = None
) -> dict[str, Any]:
    """客户端只写邮箱。不打开工厂数据库。"""
    cred = json.loads(Path(credential_file).read_text(encoding="utf-8"))
    endpoint = Path(str(cred["endpoint"]))
    request_id = (
        "req-"
        + hashlib.sha256(f"{cred['id']}:{request_key}:{canonical_json(command)}".encode()).hexdigest()[:16]
    )
    payload = {
        "schema_version": "mao-operation-1",
        "credential_id": cred["id"],
        "request_id": request_id,
        "command": command,
        "request_key": request_key,
    }
    payload["signature"] = _sign(str(cred["secret"]), payload)
    dest = endpoint / "requests" / f"{request_id}.json"
    if not dest.exists():
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(canonical_json(payload), encoding="utf-8")
        tmp.replace(dest)
    return {"request_id": request_id, "endpoint": str(endpoint)}


def handle_mailbox(ctx: Any, credential_id: str) -> list[dict[str, Any]]:
    row = _credential(ctx, credential_id)
    endpoint = ctx.root / "operations" / credential_id
    done = []
    for path in sorted((endpoint / "requests").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = _handle_one(ctx, row, payload)
        response = {
            "schema_version": "mao-operation-1",
            "credential_id": credential_id,
            "request_id": payload.get("request_id"),
            "state": result["state"],
            "result": result.get("result"),
            "error": result.get("error"),
        }
        secret = Path(str(row["secret_ref"])).read_text(encoding="utf-8")
        response["signature"] = _sign(secret, response)
        out = endpoint / "responses" / f"{payload.get('request_id')}.json"
        out.write_text(canonical_json(response), encoding="utf-8")
        done.append(response)
    return done


def show_operation(ctx: Any, credential_id: str, request_id: str) -> dict[str, Any]:
    path = ctx.root / "operations" / credential_id / "responses" / f"{request_id}.json"
    if not path.is_file():
        return {"request_id": request_id, "state": "pending", "recovery": "factory operation show"}
    return json.loads(path.read_text(encoding="utf-8"))


def _handle_one(ctx: Any, row: Any, payload: dict[str, Any]) -> dict[str, Any]:
    now = utc_iso(ctx.clock.now())
    if row["revoked_at"] or str(row["expires_at"]) <= now:
        return _error("OPERATION_CREDENTIAL_EXPIRED", "凭据已失效")
    secret = Path(str(row["secret_ref"])).read_text(encoding="utf-8")
    signed = dict(payload)
    signature = signed.pop("signature", "")
    if not hmac.compare_digest(_sign(secret, signed), str(signature)):
        return _error("OPERATION_FORBIDDEN", "签名不正确")
    command = list(payload.get("command") or [])
    if not command or command[0] not in _ALLOWED or any(part in _FORBIDDEN for part in command):
        return _error("OPERATION_FORBIDDEN", "命令不在 operate 范围")
    if "human:" in " ".join(command):
        return _error("OPERATION_FORBIDDEN", "外部身份不能冒充人")
    request_key = payload.get("request_key")
    digest = canonical_json(command)
    if request_key:
        existing = ctx.db.query(
            "SELECT args_digest, message_id, result FROM operation_request_keys WHERE credential_id = ? AND request_key = ?",
            (row["id"], request_key),
        )
        if existing:
            if str(existing[0]["args_digest"]) != digest:
                return _error("OPERATION_CONFLICT", "同一请求键参数已改变")
            return {"state": "completed", "result": json.loads(existing[0]["result"] or "{}")}
    message_id = ctx.ids.new("om")
    result = {"message_id": message_id, "by": "external:" + str(row["subject"]), "command": command}
    with ctx.db.tx() as conn:
        conn.execute(
            """INSERT INTO operation_requests (
                   credential_id, request_id, request_key, args_digest, state, result, error, message_id, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'completed', ?, NULL, ?, ?, ?)""",
            (
                row["id"],
                payload["request_id"],
                request_key,
                digest,
                canonical_json(result),
                message_id,
                now,
                now,
            ),
        )
        if request_key:
            conn.execute(
                """INSERT INTO operation_request_keys (
                       credential_id, request_key, args_digest, message_id, result, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (row["id"], request_key, digest, message_id, canonical_json(result), now),
            )
    return {"state": "completed", "result": result}


def _credential(ctx: Any, credential_id: str) -> Any:
    rows = ctx.db.query("SELECT * FROM operation_credentials WHERE id = ?", (credential_id,))
    if not rows:
        raise FactoryError("OPERATION_FORBIDDEN", "凭据不存在", id=credential_id)
    return rows[0]


def _sign(secret: str, payload: dict[str, Any]) -> str:
    body = {key: payload[key] for key in payload if key != "signature"}
    return hmac.new(secret.encode("utf-8"), canonical_json(body).encode("utf-8"), hashlib.sha256).hexdigest()


def _error(code: str, message: str) -> dict[str, Any]:
    return {"state": "failed", "error": {"code": code, "message": message}}
