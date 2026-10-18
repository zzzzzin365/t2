from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pydantic import ValidationError

from src.benchmark.evaluate import evaluate
from src.benchmark.scenarios import Expectation, make_scenarios
from src.workflow.engine import WorkflowEngine
from src.workflow.llm import Budget, BudgetExceeded, ModelFailure, OfflineModel, OpenAICompatibleModel
from src.workflow.memory import ContextBuilder, ContextOverflow, MemoryStore
from src.workflow.telemetry import Trace, canonical
from src.workflow.tools import ActionExecutor, SimulatedWorld, ToolError, ToolExecutor, TransientToolError
from src.workflow.types import Action, ExperimentConfig, MemoryRecord, Snapshot, WorkflowInput


@pytest.fixture
def world():
    trace = Trace()
    w = SimulatedWorld(":memory:", trace)
    w.ingest(event())
    yield w
    w.close()


def snapshot(**kwargs):
    return Snapshot(campaign_id="a", product="水杯", data_version="v1", **kwargs)


def event(**kwargs):
    defaults = dict(event_id="e1", simulated_time=0, snapshots=[snapshot()])
    defaults.update(kwargs)
    return WorkflowInput(**defaults)


def engine(world, group="B3", **config):
    return WorkflowEngine(group, ExperimentConfig(**config), world, world.trace, OfflineModel(world.trace))


def action(kind="adjust_budget", value=800, target="a"):
    return Action(campaign_id="a", action_type=kind, target_id=target, value=value, evidence_version="v1", reason="test")


def test_stable_skips_four_nodes_but_baseline_runs_all(world):
    result = engine(world).run(event())
    assert result.status == "completed"
    assert result.executed_branches == {"a": ["monitor"]}
    trace2 = Trace()
    other = SimulatedWorld(":memory:", trace2)
    try:
        baseline = engine(other, "B0").run(event())
        assert baseline.executed_branches["a"] == ["monitor", "audience", "creative", "bidding", "optimize"]
        assert not baseline.proposed_actions
        assert trace2.totals()["offline_model_calls"] == 1
        assert trace2.totals()["llm_requests"] == 0
    finally:
        other.close()


def test_langgraph_partial_updates_preserve_state_and_campaign_mapping(world):
    flow = engine(world)
    request = event(snapshots=[snapshot(), Snapshot(campaign_id="b", product="耳机", data_version="v1", creative_requested=True)])
    result = flow.run(request)
    assert result.status == "completed"
    assert all(a.campaign_id == "b" for a in result.proposed_actions)
    state = world.state("b")
    assert len(state["creatives"]) == len(state["ab_tests"]) == 1
    assert next(iter(state["ab_tests"].values()))["creative_id"] in state["creatives"]
    assert not world.state("a")["creatives"]


def test_wait_then_reuse_then_new_evidence(world):
    flow = engine(world)
    initial = event(snapshots=[snapshot(creative_requested=True)])
    assert flow.run(initial).proposed_actions
    second = initial.model_copy(update={"event_id": "e2", "simulated_time": 10})
    assert flow.run(second).status == "waiting_for_observation"
    third = initial.model_copy(update={"event_id": "e3", "simulated_time": 70})
    result = flow.run(third)
    assert result.reused_branches["a"] == ["creative"]
    assert not result.proposed_actions
    fourth = third.model_copy(update={"event_id": "e4", "simulated_time": 90,
                                      "snapshots": [initial.snapshots[0].model_copy(update={"data_version": "v2"})]})
    assert flow.run(fourth).proposed_actions


def test_constraint_update_overrides_observation(world):
    flow = engine(world)
    req = event(snapshots=[snapshot(creative_requested=True)])
    flow.run(req)
    changed = req.model_copy(update={"event_id": "e2", "simulated_time": 10, "current_banned": {"a": ["限时"]}})
    result = flow.run(changed)
    assert result.status == "completed"
    assert result.proposed_actions
    assert "限时" not in canonical(result.raw_creatives)


@pytest.mark.parametrize("field", ["audience_version", "data_version"])
def test_dependency_version_invalidates_results(world, field):
    flow = engine(world)
    req = event(snapshots=[snapshot(audience_due=True)])
    flow.run(req)
    changed = req.model_copy(update={"event_id": "e2", "simulated_time": 80,
                                    "snapshots": [req.snapshots[0].model_copy(update={field: "v2"})]})
    result = flow.run(changed)
    assert all(b in result.executed_branches["a"] for b in ["audience", "creative", "bidding"])


def test_cache_ttl_versions_and_copy_isolation(world):
    tools = ToolExecutor(world, world.trace, True, ttl=10)
    args = {"campaign_ids": ["a"]}
    first = tools.execute("metrics", args, "v1", "c1")
    first["data"]["a"]["product"] = "corrupted"
    assert tools.execute("metrics", args, "v1", "c1")["data"]["a"]["product"] == "水杯"
    tools.execute("metrics", args, "v2", "c1")
    tools.execute("metrics", args, "v2", "c2")
    world.now = 10
    tools.execute("metrics", args, "v2", "c2")
    assert world.trace.totals()["backend_reads"] == 4
    assert world.trace.totals()["cache_hits"] == 1


def test_incomplete_results_not_cached(world):
    world.faults = {"metrics": ["incomplete", "incomplete", "incomplete"]}
    tools = ToolExecutor(world, world.trace, True)
    with pytest.raises(TransientToolError):
        tools.execute("metrics", {"campaign_ids": ["a"]}, "v1", "c1")
    assert not tools.cache
    assert tools.execute("metrics", {"campaign_ids": ["a"]}, "v1", "c1")["complete"]
    assert world.trace.totals()["backend_reads"] == 4


def test_concurrent_identical_requests_singleflight(world):
    tools = ToolExecutor(world, world.trace, True)
    original = world.invoke
    entered, release, followers = threading.Event(), threading.Event(), threading.Event()
    original_emit = world.trace.emit
    def observe(kind, **data):
        original_emit(kind, **data)
        if kind == "coalesced" and world.trace.totals()["coalesced_requests"] == 3:
            followers.set()
    world.trace.emit = observe
    def slow(name, args):
        entered.set()
        assert release.wait(5)
        return original(name, args)
    world.invoke = slow
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(tools.execute, "metrics", {"campaign_ids": ["a"]}, "v1", "c1")
        assert entered.wait(5)
        rest = [pool.submit(tools.execute, "metrics", {"campaign_ids": ["a"]}, "v1", "c1") for _ in range(3)]
        assert followers.wait(5), "all followers must join the outstanding request before release"
        release.set()
        outputs = [f.result() for f in [first] + rest]
    assert all(o == outputs[0] for o in outputs)
    assert world.trace.totals()["backend_reads"] == 1
    assert world.trace.totals()["coalesced_requests"] == 3


@pytest.mark.parametrize("fault", ["timeout", "incomplete", "after_commit"])
def test_write_recovery_idempotent(world, fault):
    world.faults = {"apply_action": [fault]}
    executor = ActionExecutor(world, world.trace)
    a = action()
    one = executor.execute("e1", a, [], [])
    two = executor.execute("e1", a, [], [])
    assert one["success"] and two["success"]
    assert world.trace.totals()["state_mutations"] == 1
    executor.execute("e2", a, [], [])
    assert world.trace.totals()["state_mutations"] == 2


def test_unprotected_write_fault_demonstrates_duplicate_execution(world):
    world.faults = {"apply_action": ["after_commit"]}
    ActionExecutor(world, world.trace, idempotent=False).execute("e1", action(), [], [])
    assert world.trace.totals()["state_mutations"] == 2


def test_ab_test_rejects_foreign_or_nonexistent_creative(world):
    with pytest.raises(ToolError):
        ActionExecutor(world, world.trace).execute("e1", action("start_ab_test", "foreign_creative", "test"), [], [])
    assert world.trace.totals()["state_mutations"] == 0


def test_memory_isolation_expiry_supersession(world):
    store = MemoryStore(world)
    old = MemoryRecord(record_id="old", campaign_id="a", timestamp=0, kind="constraint", content="old", banned_terms=["旧词"])
    store.put(old)
    store.put(MemoryRecord(record_id="foreign", campaign_id="b", timestamp=0, kind="constraint", content="secret", banned_terms=["别的活动"], supersedes="old"))
    assert store.constraints("a", 1)[0] == ["旧词"]
    store.put(MemoryRecord(record_id="new", campaign_id="a", timestamp=2, kind="constraint", content="new", banned_terms=["新词"], supersedes="old", expires_at=5))
    assert store.constraints("a", 3)[0] == ["新词"]
    assert store.constraints("a", 5)[0] == []


def test_context_retains_mandatory_and_limits_optional(world):
    store = MemoryStore(world)
    for i in range(12):
        store.put(MemoryRecord(record_id=f"h{i}", campaign_id="a", timestamp=i, content="记录" * 30))
    store.put(MemoryRecord(record_id="policy", campaign_id="a", timestamp=0, kind="constraint", content="hard", banned_terms=["限时"]))
    builder = ContextBuilder(store, world.trace, top_k=2, optional_budget=300, total_limit=8000, output_reservation=600)
    selected = builder.build(snapshot(), 20, "selective", {}, [], [])
    full = builder.build(snapshot(), 20, "full", {}, [], [])
    assert "限时" in selected["banned_terms"]
    assert len(selected["history"]) <= 3
    assert len(full["history"]) == 13
    with pytest.raises(ContextOverflow):
        ContextBuilder(store, world.trace, 5, 2000, 800, 600).build(snapshot(), 20, "full", {}, [], [])


def test_raw_violations_not_hidden_by_executor(world):
    flow = engine(world, "B2")
    req = event(snapshots=[snapshot(creative_requested=True)], memory_updates=[MemoryRecord(record_id="rule", campaign_id="a", timestamp=0, kind="constraint", content="no urgency", banned_terms=["限时"])])
    result = flow.run(req)
    assert result.status == "failed"
    assert result.raw_creatives and not world.state("a")["creatives"]
    assessment = evaluate(result, Expectation(required_branches={"a": ["creative"]}, required_actions={"a": ["create_creative", "start_ab_test"]}, banned={"a": ["限时"]}))
    assert not assessment["success"] and assessment["constraint_pass"] == 0


def test_all_generated_fixtures_are_reproducible_and_separate():
    dev, test = make_scenarios("dev"), make_scenarios("test")
    assert len(dev) == 70 and len(test) == 210
    assert {s.case_id for s in dev}.isdisjoint({s.case_id for s in test})
    assert [s.model_dump() for s in dev] == [s.model_dump() for s in make_scenarios("dev")]
    assert all(4 <= len(s.events) <= 8 for s in dev + test)
    assert "required_branches" not in canonical(dev[0].events[0].model_dump())


@pytest.mark.parametrize("n", [10, 100, 1000])
def test_batch_queries_scale_in_two_calls(world, n):
    snaps = [Snapshot(campaign_id=f"c{i}", product="产品", data_version="v1") for i in range(n)]
    world.ingest(event(snapshots=snaps))
    tools = ToolExecutor(world, world.trace, True)
    ids = [s.campaign_id for s in snaps]
    age = tools.execute("audience_age", {"campaign_ids": ids}, "v1", "c1")
    device = tools.execute("audience_device", {"campaign_ids": ids}, "v1", "c1")
    assert len(age["data"]) == len(device["data"]) == n
    assert world.trace.totals()["backend_reads"] == 2
    # Verify a sampled batch row against the single-campaign implementation.
    assert age["data"][ids[-1]] == world.invoke("audience_age", {"campaign_ids": [ids[-1]]})["data"][ids[-1]]


def response(content=None, usage=None):
    output = {"campaign_id": "a", "variants": [{"headline": "水杯", "description": "便携", "cta_text": "了解"}]}
    return {"model": "test-model", "choices": [{"message": {"content": content if content is not None else json.dumps(output)}}], "usage": usage if usage is not None else {"prompt_tokens": 100, "completion_tokens": 20}}


def live_adapter(trace, handler, **config):
    cfg = ExperimentConfig(mode="live", max_requests=config.pop("max_requests", 10), max_tokens=config.pop("max_tokens", 100000), **config)
    budget = Budget(cfg.max_requests, cfg.max_tokens)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatibleModel(cfg, trace, budget, base_url="https://test.invalid/v1", model="test-model", api_key="secret-test-key", client=client, sleep=lambda _: None)


def test_real_adapter_protocol_usage_and_no_key_in_trace():
    trace = Trace()
    def handler(req):
        assert req.url.path == "/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer secret-test-key"
        return httpx.Response(200, json=response())
    adapter = live_adapter(trace, handler)
    assert adapter.generate({"campaign_id": "a"}).campaign_id == "a"
    assert trace.totals()["llm_requests"] == 1
    assert trace.totals()["input_tokens"] == 100
    assert adapter.budget.charged_tokens == 120
    assert "secret-test-key" not in canonical(trace.events)


def test_model_retry_counts_every_attempt_and_unknown_usage():
    trace = Trace()
    replies = iter([httpx.Response(429), httpx.Response(200, json=response())])
    adapter = live_adapter(trace, lambda _: next(replies))
    adapter.generate({"campaign_id": "a"})
    assert trace.totals()["llm_requests"] == 2
    assert trace.totals()["unknown_usage_requests"] == 1
    assert adapter.budget.charged_tokens > 120


def test_format_repair_has_one_extra_counted_call():
    trace = Trace()
    replies = iter([response("not json"), response()])
    adapter = live_adapter(trace, lambda _: httpx.Response(200, json=next(replies)))
    adapter.generate({"campaign_id": "a"})
    assert trace.totals()["llm_requests"] == 2
    assert adapter.budget.charged_tokens == 240


def test_failure_never_falls_back_to_mock():
    trace = Trace()
    adapter = live_adapter(trace, lambda _: httpx.Response(401, text="secret-test-key"))
    with pytest.raises(ModelFailure):
        adapter.generate({"campaign_id": "a"})
    assert trace.totals()["offline_model_calls"] == 0
    assert "secret-test-key" not in canonical(trace.events)


def test_token_budget_checked_before_network():
    trace = Trace()
    adapter = live_adapter(trace, lambda _: pytest.fail("must not send request"), max_tokens=1)
    with pytest.raises(BudgetExceeded):
        adapter.generate({"campaign_id": "a"})
    assert trace.totals()["llm_requests"] == 0


def test_missing_usage_is_labeled_estimate():
    trace = Trace()
    adapter = live_adapter(trace, lambda _: httpx.Response(200, json=response(usage={})))
    adapter.generate({"campaign_id": "a"})
    assert trace.totals()["usage_estimated_responses"] == 1


def test_schema_rejects_invalid_or_nonfinite_metrics():
    with pytest.raises(ValidationError):
        snapshot(cost=float("inf"))
    with pytest.raises(ValidationError):
        snapshot(clicks=2, conversions=3)


def test_empty_input_and_zero_denominator(world):
    assert engine(world).run(event(snapshots=[])).status == "completed"
    zero = snapshot(impressions=0, clicks=0, conversions=0, cost=0, revenue=0)
    assert zero.metrics() == {"ctr": None, "cvr": None, "cpa": None, "roas": None}


def test_permanent_tool_failure_is_explicit(world):
    req = event(faults={"metrics": ["fail", "fail", "fail"]})
    result = engine(world).run(req)
    assert result.status == "failed"
    assert not result.proposed_actions


def test_thinking_mode_is_explicit_and_truncation_does_not_retry():
    trace = Trace()
    def handler(request):
        assert json.loads(request.content)["thinking"] == {"type": "disabled"}
        body = response(content="")
        body["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=body)
    adapter = live_adapter(trace, handler, thinking_mode="disabled")
    with pytest.raises(ModelFailure, match="truncated"):
        adapter.generate({"campaign_id": "a"})
    assert trace.totals()["llm_requests"] == 1
    assert trace.totals()["input_tokens"] == 100
