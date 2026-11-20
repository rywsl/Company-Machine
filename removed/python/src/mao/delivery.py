"""可靠外部效果 outbox（spec/system/continuity.md 第 3.1、4.1、5.8 节）。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from mao.base import canonical_json, utc_iso

OUTBOX_KINDS = ("epoch_refs", "rebuild_run", "agent_mail")
_CLAIM_S = 60.0
_HANDLERS: dict[str, Any] = {}


def register_effect(kind: str, handler: Any) -> None:
    if kind not in ("epoch_refs", "rebuild_run"):
        raise ValueError(kind)
    _HANDLERS[kind] = handler


class DeliveryStore:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def enqueue(
        self,
        conn: Any,
        *,
        kind: str,
        effect_key: str,
        body: Mapping[str, Any],
    ) -> str:
        if kind not in OUTBOX_KINDS:
            raise ValueError(kind)
        existing = conn.execute(
            "SELECT id FROM effect_outbox WHERE effect_key = ?",
            (effect_key,),
        ).fetchone()
        if existing is not None:
            return str(existing["id"])
        out_id = self.ctx.ids.new("ob")
        now = utc_iso(self.ctx.clock.now())
        conn.execute(
            """INSERT INTO effect_outbox (
                   id, effect_key, kind, body, state, revision, attempts, next_at, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'pending', 0, 0, ?, ?, ?)""",
            (out_id, effect_key, kind, canonical_json(dict(body)), now, now, now),
        )
        return out_id

    def drain(self, *, limit: int = 100) -> list[str]:
        done: list[str] = []
        for _ in range(max(0, int(limit))):
            claimed = self._claim_one()
            if claimed is None:
                break
            try:
                self._handle(claimed)
            except Exception as exc:
                self._fail(claimed, exc)
                continue
            if self._complete(claimed):
                done.append(str(claimed["id"]))
        return done

    def _claim_one(self) -> dict[str, Any] | None:
        now_dt = self.ctx.clock.now()
        now = utc_iso(now_dt)
        until = utc_iso(now_dt + timedelta(seconds=_CLAIM_S))
        owner = self.ctx.owner
        with self.ctx.db.tx() as conn:
            row = conn.execute(
                """SELECT * FROM effect_outbox
                   WHERE next_at <= ?
                     AND (
                       state = 'pending'
                       OR (state = 'claimed' AND (claim_until IS NULL OR claim_until <= ?))
                     )
                   ORDER BY created_at, id
                   LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            generation = int(row["lease_generation"] or 0) + 1
            cur = conn.execute(
                """UPDATE effect_outbox
                   SET state = 'claimed', owner = ?, lease_generation = ?, claim_until = ?,
                       updated_at = ?
                   WHERE id = ? AND revision = ? AND state IN ('pending', 'claimed')
                     AND (
                       state = 'pending'
                       OR claim_until IS NULL
                       OR claim_until <= ?
                     )""",
                (owner, generation, until, now, row["id"], int(row["revision"]), now),
            )
            if cur.rowcount != 1:
                return None
            return {
                "id": str(row["id"]),
                "effect_key": str(row["effect_key"]),
                "kind": str(row["kind"]),
                "body": json.loads(row["body"]),
                "owner": owner,
                "generation": generation,
                "revision": int(row["revision"]),
            }

    def _handle(self, claimed: Mapping[str, Any]) -> None:
        kind = str(claimed["kind"])
        if kind == "agent_mail":
            self._deliver_mail(dict(claimed["body"]))
            return
        handler = _HANDLERS.get(kind)
        if handler is None:
            raise RuntimeError(f"outbox handler missing: {kind}")
        handler(self.ctx, dict(claimed["body"]))

    def _deliver_mail(self, body: Mapping[str, Any]) -> None:
        from mao.agents import Mailbox

        agent = str(body["agent"])
        kind = str(body["kind"])
        source = str(body["source"])
        payload = body.get("body") if isinstance(body.get("body"), Mapping) else {}
        key_source = str(body.get("delivery_source") or source)
        with self.ctx.db.tx() as conn:
            existing = conn.execute(
                "SELECT message_id FROM delivery_keys WHERE kind = ? AND source = ? AND target = ?",
                ("agent_mail", key_source, agent),
            ).fetchone()
            if existing is not None:
                return
        message = Mailbox(self.ctx).post(
            agent,
            kind,
            source,
            dict(payload),
            delivery_source=key_source,
            delivery_kind="agent_mail",
        )
        if message.id:
            return

    def _complete(self, claimed: Mapping[str, Any]) -> bool:
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            cur = conn.execute(
                """UPDATE effect_outbox
                   SET state = 'done', error = NULL, updated_at = ?
                   WHERE id = ? AND owner = ? AND lease_generation = ? AND revision = ?
                     AND state = 'claimed'""",
                (
                    now,
                    claimed["id"],
                    claimed["owner"],
                    int(claimed["generation"]),
                    int(claimed["revision"]),
                ),
            )
            return cur.rowcount == 1

    def _fail(self, claimed: Mapping[str, Any], exc: BaseException) -> None:
        now_dt = self.ctx.clock.now()
        now = utc_iso(now_dt)
        with self.ctx.db.tx() as conn:
            row = conn.execute(
                "SELECT attempts FROM effect_outbox WHERE id = ?",
                (claimed["id"],),
            ).fetchone()
            attempts = 1 if row is None else int(row["attempts"]) + 1
            delay = min(60, 2 ** (attempts - 1))
            nxt = utc_iso(now_dt + timedelta(seconds=delay))
            conn.execute(
                """UPDATE effect_outbox
                   SET state = 'pending', owner = NULL, claim_until = NULL,
                       revision = revision + 1, attempts = ?, next_at = ?, error = ?, updated_at = ?
                   WHERE id = ? AND owner = ? AND lease_generation = ? AND revision = ?""",
                (
                    attempts,
                    nxt,
                    canonical_json({"code": type(exc).__name__, "message": str(exc)[:200]}),
                    now,
                    claimed["id"],
                    claimed["owner"],
                    int(claimed["generation"]),
                    int(claimed["revision"]),
                ),
            )
