from __future__ import annotations

import time
from langgraph.graph import END, START, StateGraph

from .llm import BudgetExceeded, ModelConfigurationError, ModelFailure
from .memory import ContextBuilder, ContextOverflow, MemoryStore
from .telemetry import Trace, fingerprint
from .tools import ActionExecutor, SimulatedWorld, ToolError, ToolExecutor
from .types import Action, Alert, ExperimentConfig, MemoryRecord, RouteDecision, RunResult, WorkflowInput, WorkflowState


class WorkflowEngine:
    """Shared corrected semantics; group switches change scheduling/reuse/memory only."""

    def __init__(self, group: str, config: ExperimentConfig, world: SimulatedWorld, trace: Trace, model):
        if group not in {"B0", "B1", "B2", "B3", "M-Full"}:
            raise ValueError("unknown experiment group")
        self.group, self.config, self.world, self.trace, self.model = group, config, world, trace, model
        self.tools = ToolExecutor(world, trace, reuse=group in {"B2", "B3", "M-Full"}, ttl=config.cache_ttl_seconds)
        self.executor = ActionExecutor(world, trace)
        self.memory = MemoryStore(world)
        self.context = ContextBuilder(self.memory, trace, config.memory_top_k, config.memory_token_budget,
                                      config.context_token_limit, config.max_output_tokens)
        self.completed: dict[str, str] = {}
        self.observations: dict[str, tuple[int, str]] = {}
        self.last_proposed: dict[str, tuple[str, int]] = {}
        self.graph = self._build()

    def _build(self):
        graph = StateGraph(WorkflowState)
        for name in ["monitor", "audience", "creative", "bidding", "optimize"]:
            graph.add_node(name, getattr(self, "_" + name))
        graph.add_edge(START, "monitor")
        if self.group == "B0":
            for left, right in zip(["monitor", "audience", "creative", "bidding"], ["audience", "creative", "bidding", "optimize"]):
                graph.add_edge(left, right)
        else:
            for name in ["monitor", "audience", "creative", "bidding"]:
                graph.add_conditional_edges(name, lambda state, after=name: self._next(state, after),
                                            {n: n for n in ["audience", "creative", "bidding", "optimize"]} | {"end": END})
        graph.add_edge("optimize", END)
        return graph.compile()

    def _next(self, state: WorkflowState, after: str) -> str:
        ordered = ["monitor", "audience", "creative", "bidding", "optimize"]
        needed = {b for route in state["routes"] for b in route.branches if not route.reused and not route.waiting}
        for candidate in ordered[ordered.index(after) + 1:]:
            if candidate in needed or (candidate == "optimize" and needed):
                return candidate
        return "end"

    def _ids(self, state: WorkflowState, branch: str):
        if self.group == "B0":
            return [s.campaign_id for s in state["event"].snapshots]
        return [r.campaign_id for r in state["routes"] if branch in r.branches and not r.reused and not r.waiting]

    def _enter(self, state, name):
        ids = self._ids(state, name) if name != "monitor" else [s.campaign_id for s in state["event"].snapshots]
        self.trace.emit("agent", agent=name, campaign_ids=ids)
        for cid in ids:
            state["result"].executed_branches.setdefault(cid, []).append(name)

    def _read(self, state, tool, ids):
        event = state["event"]
        evidence = [(s.campaign_id, s.data_version, s.audience_version) for s in event.snapshots if s.campaign_id in ids]
        version = fingerprint([evidence, self.world.revision if tool == "campaign_state" else None])
        return self.tools.execute(tool, {"campaign_ids": ids}, version, event.config_version)["data"]

    def run(self, event: WorkflowInput, cache_mode: str = "warm") -> RunResult:
        started = time.perf_counter()
        result = RunResult(event_id=event.event_id, status="running")
        self.trace.event_id = event.event_id
        self.world.ingest(event)
        for record in event.memory_updates:
            self.memory.put(record)
        if cache_mode == "cold":
            self.tools.clear()
        state: WorkflowState = {"event": event, "result": result, "alerts": [], "routes": [], "audience": {}, "creatives": {}, "bids": {}}
        try:
            for iteration in range(self.config.max_iterations):
                result.iteration = iteration + 1
                state = self.graph.invoke(state)
                result = state["result"]
                if self.group != "B0" or not state["alerts"]:
                    break
            result.routes = state["routes"]
            if result.status == "running":
                waiting = any(r.waiting for r in result.routes)
                result.status = "waiting_for_observation" if waiting else "completed"
            if self.group == "B0" and state["alerts"] and self.config.max_iterations > 1:
                result.status = "budget_exhausted"
                result.error = "internal iteration ceiling reached with unresolved evidence"
        except BudgetExceeded as exc:
            result.status, result.error = "budget_exhausted", str(exc)
        except ContextOverflow as exc:
            result.status, result.error = "context_overflow", str(exc)
        except ModelConfigurationError as exc:
            result.status, result.error = "model_configuration_error", str(exc)
        except (ToolError, ModelFailure) as exc:
            result.status, result.error = "failed", str(exc)
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        self.trace.emit("run_result", result=result.model_dump())
        return result

    def _monitor(self, state):
        self._enter(state, "monitor")
        event = state["event"]
        self._read(state, "metrics", [s.campaign_id for s in event.snapshots])
        alerts, routes = [], []
        for snap in event.snapshots:
            m = snap.metrics()
            branches = []
            if (snap.impressions > 100 and m["ctr"] < 0.005) or snap.creative_requested:
                alerts.append(Alert(campaign_id=snap.campaign_id, type="creative", value=m["ctr"]))
                branches.append("creative")
            if (m["cpa"] is not None and m["cpa"] > 200) or snap.bidding_requested or snap.budget_requested:
                alerts.append(Alert(campaign_id=snap.campaign_id, type="bidding", value=m["cpa"]))
                branches.append("bidding")
            if snap.audience_changed or snap.audience_due:
                branches += ["audience", "creative", "bidding"]
            if snap.review_due:
                branches += ["audience", "bidding"]
            if snap.cost > 100 and m["roas"] < 1:
                alerts.append(Alert(campaign_id=snap.campaign_id, type="low_roas", value=m["roas"]))
                branches.append("bidding")
            constraints = self.memory.constraints(snap.campaign_id, event.simulated_time)
            policy = fingerprint([event.config_version, event.current_banned.get(snap.campaign_id, []),
                                  event.current_required.get(snap.campaign_id, []), constraints])
            signature = fingerprint([snap.model_dump(), policy, event.task])
            branches = [b for b in ["audience", "creative", "bidding"] if b in branches]
            previous = self.observations.get(snap.campaign_id)
            waiting = bool(previous and event.simulated_time < previous[0] and policy == previous[1] and not snap.urgent)
            reused = bool(branches and self.completed.get(snap.campaign_id) == signature)
            reason = "necessary branches from evidence/task"
            if not branches:
                reason = "stable; no explicit task or periodic review"
            elif waiting:
                reason = "action still in observation window"
            elif reused:
                reason = "same evidence/config/dependencies already completed"
            if self.group == "B0":
                reason, waiting, reused = "corrected fixed five-node baseline", False, False
            route = RouteDecision(campaign_id=snap.campaign_id, branches=branches, signature=signature,
                                  reason=reason, waiting=waiting, reused=reused)
            routes.append(route)
            if reused:
                state["result"].reused_branches[snap.campaign_id] = branches
            self.trace.emit("route", **route.model_dump())
        state["result"].routes = routes
        return {"alerts": alerts, "routes": routes}

    def _audience(self, state):
        self._enter(state, "audience")
        ids = self._ids(state, "audience")
        output = {}
        batches = [ids] if self.tools.reuse else [[cid] for cid in ids]
        for batch in batches:
            age = self._read(state, "audience_age", batch)
            device = self._read(state, "audience_device", batch)
            for cid in batch:
                output[cid] = {"age": age[cid], "device": device[cid]}
        return {"audience": output}

    def _creative(self, state):
        self._enter(state, "creative")
        event, output = state["event"], {}
        mode = "selective" if self.group == "B3" else "full" if self.group == "M-Full" else "none"
        for snap in event.snapshots:
            if snap.campaign_id not in self._ids(state, "creative"):
                continue
            context = self.context.build(snap, event.simulated_time, mode, state["audience"].get(snap.campaign_id, {}),
                                         event.current_banned.get(snap.campaign_id, []), event.current_required.get(snap.campaign_id, []))
            creative = self.model.generate(context)
            output[snap.campaign_id] = creative
            state["result"].raw_creatives.append(creative.model_dump())
        return {"creatives": output}

    def _bidding(self, state):
        self._enter(state, "bidding")
        ids = self._ids(state, "bidding")
        current = self._read(state, "campaign_state", ids)
        output = {}
        for snap in state["event"].snapshots:
            if snap.campaign_id not in ids:
                continue
            m = snap.metrics()
            factor = 0.8 if m["roas"] is not None and m["roas"] < 1 else 0.9 if m["cpa"] is not None and m["cpa"] > 200 else 1.05
            # A deterministic synthetic segment adjustment makes this dependency observable.
            segment = state["audience"].get(snap.campaign_id, {}).get("device", {}).get("segment")
            if segment == "mobile":
                factor *= 1.02
            output[snap.campaign_id] = round(min(100, max(0.01, current[snap.campaign_id]["bid"] * factor)), 4)
        return {"bids": output}

    def _optimize(self, state):
        self._enter(state, "optimize")
        event, result = state["event"], state["result"]
        for route in state["routes"]:
            if self.group != "B0" and (route.reused or route.waiting or not route.branches):
                continue
            snap = next(s for s in event.snapshots if s.campaign_id == route.campaign_id)
            cid = snap.campaign_id
            # Baseline generates unnecessary suggestions, but only relevant ones become business actions.
            actions = []
            if "creative" in route.branches and cid in state["creatives"]:
                target = "creative_" + fingerprint([cid, route.signature, event.event_id])[:16]
                actions += [Action(campaign_id=cid, action_type="create_creative", target_id=target,
                                   value=[v.model_dump() for v in state["creatives"][cid].variants], evidence_version=snap.data_version, reason="creative task"),
                            Action(campaign_id=cid, action_type="start_ab_test", target_id="ab_" + target,
                                   value=target, evidence_version=snap.data_version, reason="test new creative")]
            if "bidding" in route.branches and cid in state["bids"]:
                actions.append(Action(campaign_id=cid, action_type="adjust_bid", target_id=cid, value=state["bids"][cid],
                                      evidence_version=snap.data_version, reason="bidding task"))
                if snap.budget_requested:
                    actions.append(Action(campaign_id=cid, action_type="adjust_budget", target_id=cid, value=round(snap.budget * 0.9, 2),
                                          evidence_version=snap.data_version, reason="explicit budget task"))
            previous = self.last_proposed.get(cid)
            if actions and previous and (previous[0] == route.signature or event.simulated_time < previous[1]):
                # A policy change/urgent event legitimizes re-evaluation during observation.
                old_policy = self.observations.get(cid, (0, ""))[1]
                current_policy = fingerprint([event.config_version, event.current_banned.get(cid, []), event.current_required.get(cid, []), self.memory.constraints(cid, event.simulated_time)])
                if old_policy == current_policy and not snap.urgent:
                    self.trace.emit("duplicate_decision", campaign_id=cid, signature=route.signature, actions=len(actions))
            banned, required = self.memory.constraints(cid, event.simulated_time)
            banned = sorted((set(banned) - set(event.current_required.get(cid, []))) | set(event.current_banned.get(cid, [])))
            required = sorted((set(required) - set(event.current_banned.get(cid, []))) | set(event.current_required.get(cid, [])))
            ok = True
            creative_ok = True
            for action in actions:
                result.proposed_actions.append(action)
                if action.action_type == "start_ab_test" and not creative_ok:
                    continue
                outcome = self.executor.execute(event.event_id, action, banned, required)
                result.action_results.append(outcome)
                ok = ok and outcome.get("success", False)
                if action.action_type == "create_creative":
                    creative_ok = outcome.get("success", False)
            if ok:
                self.completed[cid] = route.signature
            elif actions:
                result.status = "failed"
                result.error = "one or more actions failed effective-constraint validation"
            if actions:
                until = event.simulated_time + self.config.observation_seconds
                policy = fingerprint([event.config_version, event.current_banned.get(cid, []), event.current_required.get(cid, []), self.memory.constraints(cid, event.simulated_time)])
                self.last_proposed[cid] = (route.signature, until)
                if ok:
                    self.observations[cid] = (until, policy)
                if self.config.history_mode == "end_to_end":
                    self.memory.put(MemoryRecord(record_id=f"decision:{event.event_id}:{cid}", campaign_id=cid,
                                                 timestamp=event.simulated_time, kind="decision", evidence_version=snap.data_version,
                                                 status="executed_waiting" if ok else "insufficient_evidence",
                                                 content=f"Actions: {[a.action_type for a in actions]}; success={ok}"))
                    if ok:
                        self.memory.put(MemoryRecord(record_id=f"observation:{event.event_id}:{cid}", campaign_id=cid,
                                                     timestamp=event.simulated_time, kind="observation", expires_at=until,
                                                     status="executed_waiting", content="Wait for the next observation window before repeating intervention."))
        return {"result": result}
