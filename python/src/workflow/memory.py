from __future__ import annotations

import json
import tiktoken

from .telemetry import Trace, canonical
from .tools import SimulatedWorld
from .types import MemoryRecord, Snapshot


class ContextOverflow(RuntimeError):
    pass


_encoding = tiktoken.get_encoding("cl100k_base")


def tokens(value) -> int:
    return len(_encoding.encode(value if isinstance(value, str) else canonical(value), disallowed_special=()))


class MemoryStore:
    def __init__(self, world: SimulatedWorld):
        self.world = world

    def put(self, record: MemoryRecord):
        with self.world.lock, self.world.db:
            existing = self.world.db.execute("SELECT campaign FROM memory WHERE id=?", (record.record_id,)).fetchone()
            if existing and existing[0] != record.campaign_id:
                raise ValueError("memory record ID cannot migrate across campaigns")
            if record.supersedes:
                # A record may only supersede history in its own campaign.
                self.world.db.execute("UPDATE memory SET active=0 WHERE id=? AND campaign=?", (record.supersedes, record.campaign_id))
            self.world.db.execute("INSERT INTO memory VALUES (?,?,?,1) ON CONFLICT(id) DO UPDATE SET record=excluded.record, active=1",
                                  (record.record_id, record.campaign_id, record.model_dump_json()))

    def relevant(self, campaign_id: str, now: int) -> list[MemoryRecord]:
        with self.world.lock:
            rows = self.world.db.execute("SELECT record FROM memory WHERE campaign=? AND active=1", (campaign_id,)).fetchall()
        values = [MemoryRecord.model_validate_json(r[0]) for r in rows]
        return sorted([r for r in values if r.timestamp <= now and (r.expires_at is None or now < r.expires_at)],
                      key=lambda r: (r.timestamp, r.record_id), reverse=True)

    def constraints(self, campaign_id: str, now: int) -> tuple[list[str], list[str]]:
        records = self.relevant(campaign_id, now)
        return (sorted({x for r in records if r.kind == "constraint" for x in r.banned_terms}),
                sorted({x for r in records if r.kind == "constraint" for x in r.required_terms}))


class ContextBuilder:
    def __init__(self, store: MemoryStore, trace: Trace, top_k: int, optional_budget: int, total_limit: int, output_reservation: int):
        self.store, self.trace = store, trace
        self.top_k, self.optional_budget = top_k, optional_budget
        self.total_limit, self.output_reservation = total_limit, output_reservation

    def build(self, snapshot: Snapshot, now: int, mode: str, audience: dict, banned: list[str], required: list[str]) -> dict:
        records = self.store.relevant(snapshot.campaign_id, now) if mode != "none" else []
        mandatory = [r for r in records if r.kind in {"constraint", "observation"}]
        optional = [r for r in records if r.kind not in {"constraint", "observation"}]
        optional.sort(key=lambda r: (r.task_type == "creative", r.timestamp, r.record_id), reverse=True)
        selected = []
        if mode == "full":
            selected = optional
        elif mode == "selective":
            for record in optional:
                if len(selected) >= self.top_k:
                    break
                if tokens([r.model_dump() for r in selected + [record]]) <= self.optional_budget:
                    selected.append(record)
        history = mandatory + selected
        # Current explicit constraints supersede conflicting historical terms.
        history_banned = {t for r in mandatory if r.kind == "constraint" for t in r.banned_terms}
        history_required = {t for r in mandatory if r.kind == "constraint" for t in r.required_terms}
        effective_banned = sorted((history_banned - set(required)) | set(banned))
        effective_required = sorted((history_required - set(banned)) | set(required))
        context = {"campaign_id": snapshot.campaign_id, "product": snapshot.product, "task": "generate_creatives",
                   "metrics": snapshot.metrics(), "audience": audience, "banned_terms": effective_banned,
                   "required_terms": effective_required, "history": [r.model_dump() for r in history]}
        count = tokens(context)
        self.trace.emit("context", campaign_id=snapshot.campaign_id, memory_mode=mode, tokens=count,
                        token_source="cl100k_base_estimate", record_ids=[r.record_id for r in history], context=context)
        # Allow an explicit margin for instructions and message framing; the adapter checks again.
        if count + self.output_reservation + 256 > self.total_limit:
            raise ContextOverflow("required or full history exceeds context capacity; no silent truncation")
        return context
