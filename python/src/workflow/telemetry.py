from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class Trace:
    def __init__(self, path: Path | None = None, **tags):
        self.path = path
        self.tags = tags
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.event_id = ""
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, **data) -> None:
        with self.lock:
            row = {**self.tags, "event_id": self.event_id, "seq": len(self.events), "kind": kind, **data}
            self.events.append(row)
            if self.path:
                with self.path.open("a", encoding="utf-8") as out:
                    out.write(canonical(row) + "\n")

    def totals(self) -> dict:
        events = self.events
        responses = [e for e in events if e["kind"] == "llm_response"]
        return {
            "agent_executions": sum(e["kind"] == "agent" for e in events),
            "llm_requests": sum(e["kind"] == "llm_request" for e in events),
            "offline_model_calls": sum(e["kind"] == "offline_model" for e in events),
            "backend_reads": sum(e["kind"] == "backend" and e.get("operation") == "read" for e in events),
            "backend_writes": sum(e["kind"] == "backend" and e.get("operation") == "write" for e in events),
            "cache_hits": sum(e["kind"] == "cache_hit" for e in events),
            "coalesced_requests": sum(e["kind"] == "coalesced" for e in events),
            "input_tokens": sum(e.get("input_tokens", 0) for e in responses),
            "output_tokens": sum(e.get("output_tokens", 0) for e in responses),
            "estimated_context_tokens": sum(e.get("tokens", 0) for e in events if e["kind"] == "context"),
            "usage_estimated_responses": sum(e.get("usage_source") != "provider" for e in responses),
            "unknown_usage_requests": sum(e["kind"] == "llm_failed_attempt" for e in events),
            "model_elapsed_ms": sum(e.get("elapsed_ms", 0) for e in responses) + sum(e.get("elapsed_ms", 0) for e in events if e["kind"] == "llm_failed_attempt"),
            "tool_elapsed_ms": sum(e.get("elapsed_ms", 0) for e in events if e["kind"] == "backend"),
            "state_mutations": sum(e["kind"] == "mutation" for e in events),
            "duplicate_decisions": sum(e["kind"] == "duplicate_decision" for e in events),
            "blocked_actions": sum(e["kind"] == "action_blocked" for e in events),
        }
