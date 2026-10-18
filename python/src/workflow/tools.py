from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path

from .telemetry import Trace, canonical, fingerprint
from .types import Action, WorkflowInput


class ToolError(RuntimeError):
    pass


class TransientToolError(ToolError):
    pass


class SimulatedWorld:
    """SQLite-backed business environment. It never contacts an advertising platform."""

    def __init__(self, path: str | Path, trace: Trace):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.trace = trace
        self.now = 0
        self.revision = 0
        self.faults: dict[str, list[str]] = {}
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, state TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS ledger (key TEXT PRIMARY KEY, result TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS memory (id TEXT PRIMARY KEY, campaign TEXT NOT NULL, record TEXT NOT NULL, active INTEGER NOT NULL);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def ingest(self, event: WorkflowInput):
        with self.lock, self.db:
            self.now = event.simulated_time
            self.faults = copy.deepcopy(event.faults)
            for snap in event.snapshots:
                initial = {"bid": snap.bid, "budget": snap.budget, "status": "active", "creatives": {}, "ab_tests": {}}
                self.db.execute("INSERT INTO campaigns VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot",
                                (snap.campaign_id, snap.model_dump_json(), canonical(initial)))

    def state(self, campaign_id: str) -> dict:
        row = self.db.execute("SELECT state FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if row is None:
            raise ToolError("unknown campaign")
        return json.loads(row[0])

    def invoke(self, name: str, args: dict) -> dict:
        started = time.perf_counter()
        operation = "write" if name == "apply_action" else "read"
        status = "ok"
        try:
            with self.lock:
                queue = self.faults.get(name, [])
                fault = queue.pop(0) if queue else None
                if fault in {"timeout", "fail"}:
                    raise TransientToolError(f"injected {fault}: {name}")
                if fault == "incomplete":
                    status = "incomplete"
                    return {"complete": False}
                if name == "apply_action":
                    result = self._apply(args)
                    if fault == "after_commit":
                        raise TransientToolError("injected lost response after commit")
                    return result
                if name == "action_status":
                    row = self.db.execute("SELECT result FROM ledger WHERE key=?", (args["key"],)).fetchone()
                    return {"complete": True, "result": json.loads(row[0]) if row else None}
                ids = sorted(set(args.get("campaign_ids", [])))
                if not ids:
                    return {"complete": True, "data": {}}
                placeholders = ",".join("?" for _ in ids)
                # One SQL request per batch; rows are split back by campaign below.
                rows = self.db.execute(f"SELECT * FROM campaigns WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
                if len(rows) != len(ids):
                    raise ToolError("unknown campaign in batch")
                result = {}
                for row in rows:
                    snap = json.loads(row["snapshot"])
                    if name == "metrics":
                        value = snap
                    elif name == "campaign_state":
                        value = json.loads(row["state"])
                    elif name in {"audience_age", "audience_device"}:
                        # Deterministic synthetic profiles vary with campaign AND evidence version.
                        score = int(fingerprint([row["id"], snap["audience_version"], name])[:8], 16)
                        value = {"version": snap["audience_version"], "segment": ["25-34", "35-44"][score % 2] if name == "audience_age" else ["mobile", "desktop"][score % 2], "sample_size": snap["clicks"]}
                    else:
                        raise ToolError(f"unknown read tool: {name}")
                    result[row["id"]] = value
                return {"complete": True, "data": result}
        except Exception:
            status = "error"
            raise
        finally:
            self.trace.emit("backend", tool=name, operation=operation, args=args, status=status,
                            elapsed_ms=(time.perf_counter() - started) * 1000)

    def _apply(self, args: dict) -> dict:
        action = Action.model_validate(args["action"])
        key = args["key"]
        protect = args.get("idempotent", True)
        with self.db:
            previous = self.db.execute("SELECT result FROM ledger WHERE key=?", (key,)).fetchone()
            if previous and protect:
                return {**json.loads(previous[0]), "deduplicated": True}
            state = self.state(action.campaign_id)
            if action.action_type == "adjust_bid":
                value = float(action.value)
                if not 0 < value <= 100:
                    raise ToolError("bid outside simulated bounds")
                state["bid"] = value
            elif action.action_type == "adjust_budget":
                value = float(action.value)
                if not 0 < value <= 100000:
                    raise ToolError("budget outside simulated bounds")
                state["budget"] = value
            elif action.action_type == "create_creative":
                state["creatives"][action.target_id] = {"variants": action.value, "status": "active"}
            elif action.action_type == "pause_campaign":
                state["status"] = "paused"
            elif action.action_type == "pause_creative":
                if action.target_id not in state["creatives"]:
                    raise ToolError("unknown creative")
                state["creatives"][action.target_id]["status"] = "paused"
            elif action.action_type == "start_ab_test":
                if action.value not in state["creatives"]:
                    raise ToolError("A/B test must reference an existing creative on the same campaign")
                state["ab_tests"][action.target_id] = {"creative_id": action.value, "status": "running"}
            result = {"complete": True, "success": True, "key": key, "campaign_id": action.campaign_id,
                      "action_type": action.action_type, "target_id": action.target_id, "deduplicated": False}
            ledger_key = key if protect or previous is None else f"{key}:{uuid.uuid4()}"
            self.db.execute("UPDATE campaigns SET state=? WHERE id=?", (canonical(state), action.campaign_id))
            self.db.execute("INSERT INTO ledger VALUES (?,?)", (ledger_key, canonical(result)))
        self.revision += 1
        self.trace.emit("mutation", key=key, action=action.model_dump())
        return result


class ToolExecutor:
    def __init__(self, world: SimulatedWorld, trace: Trace, reuse: bool, ttl: int = 120):
        self.world, self.trace, self.reuse, self.ttl = world, trace, reuse, ttl
        self.cache: dict[str, tuple[int, dict]] = {}
        self.inflight: dict[str, Future] = {}
        self.lock = threading.Lock()

    def clear(self):
        with self.lock:
            self.cache.clear()

    def execute(self, name: str, args: dict, data_version: str, config_version: str) -> dict:
        if name == "apply_action":
            raise ValueError("writes must use ActionExecutor, not read cache")
        args = copy.deepcopy(args)
        if "campaign_ids" in args:
            args["campaign_ids"] = sorted(set(args["campaign_ids"]))
        key = fingerprint([name, args, data_version, config_version])
        owner = True
        future = None
        if self.reuse:
            with self.lock:
                cached = self.cache.get(key)
                if cached and self.world.now < cached[0]:
                    self.trace.emit("cache_hit", tool=name, key=key)
                    return copy.deepcopy(cached[1])
                if cached:
                    self.cache.pop(key)
                    self.trace.emit("cache_expired", tool=name, key=key, reason="virtual TTL elapsed")
                if key in self.inflight:
                    future, owner = self.inflight[key], False
                else:
                    future = Future()
                    self.inflight[key] = future
                    self.trace.emit("cache_miss", tool=name, key=key, reason="no valid entry for arguments and evidence/config version")
            if not owner:
                self.trace.emit("coalesced", tool=name, key=key)
                return copy.deepcopy(future.result())
        try:
            for attempt in range(3):
                try:
                    result = self.world.invoke(name, args)
                    if not result.get("complete"):
                        raise TransientToolError("incomplete tool result")
                    break
                except TransientToolError:
                    if attempt == 2:
                        raise
                    self.trace.emit("tool_retry", tool=name, attempt=attempt + 1)
            if self.reuse:
                with self.lock:
                    self.cache[key] = (self.world.now + self.ttl, copy.deepcopy(result))
                future.set_result(copy.deepcopy(result))
            return result
        except BaseException as exc:
            if future is not None:
                future.set_exception(exc)
            raise
        finally:
            if self.reuse:
                with self.lock:
                    self.inflight.pop(key, None)


class ActionExecutor:
    def __init__(self, world: SimulatedWorld, trace: Trace, idempotent: bool = True):
        self.world, self.trace, self.idempotent = world, trace, idempotent

    def execute(self, event_id: str, action: Action, banned: list[str], required: list[str]) -> dict:
        if action.action_type == "create_creative":
            text = canonical(action.value).lower()
            if any(term.lower() in text for term in banned) or any(term.lower() not in text for term in required):
                self.trace.emit("action_blocked", reason="creative violates effective constraints", action=action.model_dump())
                return {"success": False, "blocked": True, "action_type": action.action_type, "campaign_id": action.campaign_id}
        key = fingerprint([event_id, action.campaign_id, action.action_type, action.target_id, action.value])
        for attempt in range(3):
            try:
                result = self.world.invoke("apply_action", {"key": key, "action": action.model_dump(), "idempotent": self.idempotent})
                if not result.get("complete"):
                    raise TransientToolError("incomplete write acknowledgement")
                return result
            except TransientToolError:
                # A timeout does not prove that the write failed. Check the durable ledger first.
                try:
                    status = self.world.invoke("action_status", {"key": key})
                    if self.idempotent and status.get("complete") and status.get("result"):
                        self.trace.emit("write_recovered", key=key)
                        return status["result"]
                except TransientToolError:
                    pass
                if attempt == 2:
                    raise
                self.trace.emit("write_retry", key=key, attempt=attempt + 1)
        raise ToolError("unreachable write retry state")
