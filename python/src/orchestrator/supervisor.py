"""Python supervisor facade for the five-role workflow."""
from __future__ import annotations

from ..workflow.engine import WorkflowEngine
from ..workflow.llm import Budget, OfflineModel, OpenAICompatibleModel
from ..workflow.telemetry import Trace
from ..workflow.tools import SimulatedWorld
from ..workflow.types import ExperimentConfig, Snapshot, WorkflowInput


class AdOptimizerSupervisor:
    """Five-role corrected workflow; use benchmark CLI for reproducible experiments."""

    def __init__(self, llm=None, db_client=None, ads_client=None, *, group="B3", config=None):
        if db_client is not None or ads_client is not None:
            raise ValueError("This supervisor uses a local simulated environment; real advertising clients are unsupported.")
        self.config = config or ExperimentConfig()
        self.trace = Trace()
        self.world = SimulatedWorld(":memory:", self.trace)
        self.model = llm if llm is not None else (
            OfflineModel(self.trace) if self.config.mode == "offline" else
            OpenAICompatibleModel(self.config, self.trace, Budget(self.config.max_requests, self.config.max_tokens))
        )
        self.engine = WorkflowEngine(group, self.config, self.world, self.trace, self.model)
        self.graph = self.engine.graph

    def run(self, campaign_ids=None, max_iterations=None, *, workflow_input=None):
        if max_iterations is not None:
            self.config.max_iterations = ExperimentConfig(max_iterations=max_iterations).max_iterations
        request = workflow_input
        if request is None:
            ids = campaign_ids if campaign_ids is not None else ["demo_campaign"]
            request = WorkflowInput(event_id="demo:0", simulated_time=0, snapshots=[
                Snapshot(campaign_id=cid, product="演示产品", data_version="v1", creative_requested=True)
                for cid in ids
            ])
        if isinstance(request, dict):
            request = WorkflowInput.model_validate(request)
        result = self.engine.run(request)
        output = result.model_dump()
        output.update(
            iteration=result.iteration,
            optimization_actions=[a.model_dump() for a in result.proposed_actions],
            budget_allocations=[],
            agent_messages=[{"agent": e["agent"], "content": "Executed for " + ", ".join(e["campaign_ids"])}
                            for e in self.trace.events if e["kind"] == "agent"],
            is_complete=result.status == "completed",
        )
        return output

    def get_summary(self, result):
        return (
            f"模式: {self.config.mode}；状态: {result['status']}\n"
            f"建议动作: {len(result['optimization_actions'])}\n"
            f"调用统计: {self.trace.totals()}\n"
            "业务数据与执行环境均为模拟，不表示广告效果。"
        )

    def close(self):
        if hasattr(self.model, "close"):
            self.model.close()
        self.world.close()


def main():
    supervisor = AdOptimizerSupervisor()
    try:
        print(supervisor.get_summary(supervisor.run()))
    finally:
        supervisor.close()


if __name__ == "__main__":
    main()
