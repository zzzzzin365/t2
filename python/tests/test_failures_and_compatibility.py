import json

import httpx

from src.benchmark.runner import BenchmarkRunner
from src.orchestrator.supervisor import AdOptimizerSupervisor
from src.workflow.llm import OpenAICompatibleModel
from src.workflow.types import ExperimentConfig, Snapshot, WorkflowInput


def test_supervisor_uses_corrected_graph():
    supervisor = AdOptimizerSupervisor()
    try:
        result = supervisor.run(workflow_input=WorkflowInput(event_id="test", simulated_time=0, snapshots=[
            Snapshot(campaign_id="target", product="杯子", data_version="1", creative_requested=True)]))
        assert result["status"] == "completed"
        assert {a["campaign_id"] for a in result["optimization_actions"]} == {"target"}
        assert "模拟" in supervisor.get_summary(result)
    finally:
        supervisor.close()


def test_live_budget_interrupt_retains_usage_excludes_quality(tmp_path, monkeypatch):
    import src.benchmark.runner as runner
    def handler(request):
        messages = json.loads(request.content)["messages"]
        context = json.loads(messages[1]["content"])
        result = {"campaign_id": context["campaign_id"], "variants": [
            {"headline": "产品", "description": "介绍", "cta_text": "了解"}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 30}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    def factory(config, trace, budget):
        return OpenAICompatibleModel(config, trace, budget, base_url="https://test.invalid/v1", model="fixture", api_key="private", client=client)
    monkeypatch.setattr(runner, "OpenAICompatibleModel", factory)
    config = ExperimentConfig(mode="live", groups=["B3"], cases_per_category=1, cache_modes=["warm"],
                              output_dir=str(tmp_path), max_requests=1, max_tokens=10000)
    run_dir = BenchmarkRunner(config).run()
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    report = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert checkpoint["interrupted"] and checkpoint["requests"] == 1
    assert report["all_started_jobs_usage_including_incomplete"]["llm_requests"] == 1
    overall = next(s for s in report["summaries"] if s["category"] == "ALL")
    assert overall["incomplete_cases"] == 1
    client.close()


def test_bad_model_configuration_stops_pilot_before_repeated_calls(tmp_path, monkeypatch):
    import src.benchmark.runner as runner
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401)))
    def factory(config, trace, budget):
        return OpenAICompatibleModel(config, trace, budget, base_url="https://test.invalid/v1", model="fixture", api_key="private", client=client)
    monkeypatch.setattr(runner, "OpenAICompatibleModel", factory)
    run_dir = BenchmarkRunner(ExperimentConfig(mode="live", groups=["B3"], cases_per_category=1, cache_modes=["warm"],
                                               output_dir=str(tmp_path), max_requests=100, max_tokens=100000)).run()
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["interrupted"] and checkpoint["requests"] == 1
    assert "HTTP 401" in checkpoint["interrupted"]["reason"]
    client.close()
