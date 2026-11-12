"""工厂状态、指标与事件流。只读。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from mao.base import utc_iso
from mao.errors import FactoryError

_SECRET = re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S+")
_ALERTS = (
    ("SERVE_DOWN", "error"),
    ("SAFE_MODE", "error"),
    ("WAKES_FAILED", "error"),
    ("SUPERVISOR_DOWN", "warning"),
    ("DLQ_NONEMPTY", "warning"),
    ("HEARTBEAT_STALE", "warning"),
)
_METRICS = (
    ("mao_up", "gauge", "serve 与 supervisor 是否在线"),
    ("mao_jobs", "gauge", "job 按状态计数"),
    ("mao_runs", "gauge", "run 按状态计数"),
    ("mao_changesets", "gauge", "ChangeSet 按状态计数"),
    ("mao_wakes", "gauge", "唤醒票按状态计数"),
    ("mao_safe_mode", "gauge", "安全模式是否开启"),
    ("mao_spend_usd_total", "counter", "历史花费"),
    ("mao_foreman_sessions_total", "counter", "厂长会话按状态计数"),
    ("mao_alerts", "gauge", "当前告警"),
)


def factory_status(ctx: Any, *, epochs: Any | None = None, pipeline: Any | None = None) -> dict[str, Any]:
    config = _factory_config(ctx.root)
    now = ctx.clock.now()
    day = utc_iso(now)[:10]
    with ctx.db.tx() as conn:
        jobs = _counts(conn, "SELECT state AS k, COUNT(*) AS n FROM jobs GROUP BY state")
        runs = _counts(conn, "SELECT state AS k, COUNT(*) AS n FROM runs GROUP BY state")
        changesets = (
            {}
            if epochs is None
            else _counts(conn, "SELECT state AS k, COUNT(*) AS n FROM changesets GROUP BY state")
        )
        wakes = _counts(conn, "SELECT status AS k, COUNT(*) AS n FROM wakes GROUP BY status")
        pending = conn.execute("SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'").fetchone()
        approvals_pending = int(pending["n"]) if pending else 0
        spend = _spend(conn, day)
        sessions = _counts(conn, "SELECT state AS k, COUNT(*) AS n FROM foreman_sessions GROUP BY state")
        totals = _spend_totals(conn)
    serve = _lease_view(ctx, "serve", heartbeat=True)
    supervisor = _lease_view(ctx, "supervisor", heartbeat=False)
    epoch_doc = None
    safe = None
    if epochs is not None:
        pointers = epochs.pointers()
        epoch_doc = {
            "active": pointers.get("active"),
            "candidate": pointers.get("candidate"),
            "previous": pointers.get("previous"),
        }
        if pipeline is None:
            from mao.governance.changesets import ChangePipeline

            pipeline = ChangePipeline(ctx, epochs)
        safe = pipeline.safe_mode()
    alerts = _alerts(config, serve, supervisor, jobs, wakes, safe, now)
    return {
        "at": utc_iso(now),
        "serve": serve,
        "supervisor": supervisor,
        "epochs": epoch_doc,
        "safe_mode": safe,
        "jobs": jobs,
        "runs": runs,
        "changesets": changesets,
        "wakes": wakes,
        "approvals_pending": approvals_pending,
        "spend": spend,
        "alerts": alerts,
        "sessions": sessions,
        "totals": totals,
    }


def render_metrics(ctx: Any, *, epochs: Any | None = None, pipeline: Any | None = None) -> str:
    status = factory_status(ctx, epochs=epochs, pipeline=pipeline)
    lines: list[str] = []
    for name, kind, help_text in _METRICS:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(_samples(name, status))
    return "\n".join(lines) + "\n"


def tail_events(
    ctx: Any, *, since: str | None = None, kind: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    if since is not None:
        try:
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FactoryError("EVENTS_QUERY_INVALID", "since 不是 ISO 时间", field="since") from exc
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 10000:
        raise FactoryError("EVENTS_QUERY_INVALID", "limit 超出范围", field="limit")
    sql = "SELECT id, kind, at, job_id, data, seq FROM events"
    where: list[str] = []
    params: list[Any] = []
    if since is not None:
        where.append("at >= ?")
        params.append(since)
    if kind is not None:
        where.append("kind LIKE ?")
        params.append(kind.replace("%", "\\%") + "%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(limit)
    rows = ctx.db.query(sql, tuple(params))
    found = []
    for row in reversed(rows):
        data = json.loads(row["data"])
        found.append(
            {
                "id": str(row["id"]),
                "kind": str(row["kind"]),
                "at": str(row["at"]),
                "job_id": None if row["job_id"] is None else str(row["job_id"]),
                "data": _redact(data),
            }
        )
    return found


def _samples(name: str, status: dict[str, Any]) -> list[str]:
    if name == "mao_up":
        return [
            f'mao_up{{component="serve"}} {1 if status["serve"]["online"] else 0}',
            f'mao_up{{component="supervisor"}} {1 if status["supervisor"]["online"] else 0}',
        ]
    if name == "mao_jobs":
        return [_labeled("mao_jobs", "state", key, value) for key, value in status["jobs"].items()]
    if name == "mao_runs":
        return [_labeled("mao_runs", "state", key, value) for key, value in status["runs"].items()]
    if name == "mao_changesets":
        return [
            _labeled("mao_changesets", "state", key, value) for key, value in status["changesets"].items()
        ]
    if name == "mao_wakes":
        return [_labeled("mao_wakes", "status", key, value) for key, value in status["wakes"].items()]
    if name == "mao_safe_mode":
        on = bool(status["safe_mode"] and status["safe_mode"].get("on"))
        return [f"mao_safe_mode {1 if on else 0}"]
    if name == "mao_spend_usd_total":
        totals = status["totals"]
        return [
            f'mao_spend_usd_total{{source="{key}"}} {totals[key]!r}'
            for key in ("foreman", "model", "transcript")
        ]
    if name == "mao_foreman_sessions_total":
        return [
            _labeled("mao_foreman_sessions_total", "state", key, value)
            for key, value in status["sessions"].items()
        ]
    if name == "mao_alerts":
        return [_labeled("mao_alerts", "code", item["code"], 1) for item in status["alerts"]]
    return []


def _labeled(name: str, label: str, value: str, number: int) -> str:
    return f'{name}{{{label}="{value}"}} {number}'


def _alerts(
    config: dict[str, Any],
    serve: dict[str, Any],
    supervisor: dict[str, Any],
    jobs: dict[str, int],
    wakes: dict[str, int],
    safe: dict[str, Any] | None,
    now: datetime,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    serve_cfg = config.get("serve") if isinstance(config.get("serve"), dict) else None
    foreman_cfg = config.get("foreman") if isinstance(config.get("foreman"), dict) else None
    if serve_cfg is not None and not serve["online"]:
        found.append({"code": "SERVE_DOWN", "severity": "error", "detail": {}})
    if safe and safe.get("on"):
        found.append({"code": "SAFE_MODE", "severity": "error", "detail": {"level": safe.get("level")}})
    if wakes.get("failed", 0) > 0:
        found.append({"code": "WAKES_FAILED", "severity": "error", "detail": {"count": wakes["failed"]}})
    if foreman_cfg is not None:
        supervised = bool(serve_cfg and serve_cfg.get("supervise"))
        online = serve["online"] if supervised else supervisor["online"]
        if not online:
            found.append({"code": "SUPERVISOR_DOWN", "severity": "warning", "detail": {}})
    if jobs.get("DEAD", 0) > 0:
        found.append({"code": "DLQ_NONEMPTY", "severity": "warning", "detail": {"count": jobs["DEAD"]}})
    if serve["online"] and serve.get("heartbeat_at"):
        tick_s = float(serve_cfg.get("tick_s", 1) if serve_cfg else 1)
        age = now - datetime.fromisoformat(str(serve["heartbeat_at"]).replace("Z", "+00:00"))
        if age.total_seconds() > 3 * max(tick_s, 1):
            found.append(
                {
                    "code": "HEARTBEAT_STALE",
                    "severity": "warning",
                    "detail": {"heartbeat_at": serve["heartbeat_at"]},
                }
            )
    order = {code: index for index, (code, _sev) in enumerate(_ALERTS)}
    return sorted(found, key=lambda item: order[item["code"]])


def _lease_view(ctx: Any, resource: str, *, heartbeat: bool) -> dict[str, Any]:
    from mao.durable import LeaseStore

    lease = LeaseStore(ctx).valid(resource)
    body: dict[str, Any] = {
        "online": lease is not None,
        "owner": None if lease is None else lease.owner,
        "expires_at": None if lease is None else lease.expires_at,
    }
    if heartbeat:
        body["heartbeat_at"] = None
        body["tick"] = None
        path = Path(ctx.root) / "serve.json"
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw = {}
            if isinstance(raw, dict):
                body["heartbeat_at"] = raw.get("last_tick_at")
                body["tick"] = raw.get("tick")
    return body


def _spend(conn: Any, day: str) -> dict[str, float]:
    start = day + "T00:00:00.000Z"
    end = day + "T23:59:59.999Z"
    model = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS n FROM model_usage WHERE at >= ? AND at <= ? "
        "AND job_id NOT IN (SELECT id FROM foreman_sessions)",
        (start, end),
    ).fetchone()
    transcript = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS n FROM jobs WHERE updated_at >= ? AND updated_at <= ? "
        "AND id NOT IN (SELECT job_id FROM model_usage)",
        (start, end),
    ).fetchone()
    foreman = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS n FROM foreman_sessions WHERE started_at >= ? AND started_at <= ?",
        (start, end),
    ).fetchone()
    jobs_today = round(float(model["n"]) + float(transcript["n"]), 6)
    foreman_today = round(float(foreman["n"]), 6)
    return {"today_usd": round(jobs_today + foreman_today, 6), "foreman_today_usd": foreman_today}


def _spend_totals(conn: Any) -> dict[str, float]:
    model = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS n FROM model_usage "
        "WHERE job_id NOT IN (SELECT id FROM foreman_sessions)"
    ).fetchone()
    transcript = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS n FROM jobs WHERE id NOT IN (SELECT job_id FROM model_usage)"
    ).fetchone()
    foreman = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS n FROM foreman_sessions").fetchone()
    return {
        "model": round(float(model["n"]), 6),
        "transcript": round(float(transcript["n"]), 6),
        "foreman": round(float(foreman["n"]), 6),
    }


def _counts(conn: Any, sql: str) -> dict[str, int]:
    rows = [(str(row["k"]), int(row["n"])) for row in conn.execute(sql)]
    return dict(sorted(rows))


def _factory_config(root: Path) -> dict[str, Any]:
    path = Path(root) / "factory.json"
    if not path.is_file():
        return {}
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    return value
