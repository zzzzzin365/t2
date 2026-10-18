from __future__ import annotations

import json
import os
import platform
import random
import subprocess
import time
import zipfile
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from ..workflow.engine import WorkflowEngine
from ..workflow.llm import Budget, OfflineModel, OpenAICompatibleModel, PROMPT_VERSION
from ..workflow.telemetry import Trace, canonical, fingerprint
from ..workflow.tools import SimulatedWorld
from ..workflow.types import ExperimentConfig
from .evaluate import evaluate
from .scenarios import SCENARIO_VERSION, make_scenarios


def verify_frozen_dataset(config: ExperimentConfig):
    manifest_path = Path(__file__).resolve().parents[3] / "datasets" / "manifest.json"
    lock = json.loads(manifest_path.read_text(encoding="utf-8"))
    if config.seed != lock["seed"]:
        return False
    full = make_scenarios(config.split, None, config.seed)
    actual = fingerprint([s.model_dump() for s in full])
    if lock["version"] != SCENARIO_VERSION or actual != lock[config.split]["sha256"]:
        raise ValueError("frozen dataset drift detected; review/version dataset changes before running experiments")
    return True


def code_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    return fingerprint({str(p.relative_to(root)): p.read_text(encoding="utf-8") for folder in ["workflow", "benchmark"] for p in sorted((root / folder).glob("*.py"))})


class BenchmarkRunner:
    def __init__(self, config: ExperimentConfig):
        self.config = config
        self.budget = Budget(config.max_requests, config.max_tokens)

    def run(self) -> Path:
        config = self.config
        frozen = verify_frozen_dataset(config)
        # Validate credentials BEFORE creating any run artifacts or consuming budget.
        if config.mode == "live":
            probe = OpenAICompatibleModel(config, Trace(), self.budget)
            probe.close()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_dir = Path(config.output_dir).resolve() / stamp
        run_dir.mkdir(parents=True, exist_ok=False)
        scenarios = make_scenarios(config.split, config.cases_per_category, config.seed)
        dataset = [s.model_dump() for s in scenarios]
        (run_dir / "scenarios.json").write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
        metadata = {"created_at": stamp, "config": config.model_dump(), "scenario_version": SCENARIO_VERSION, "frozen_dataset": frozen,
                    "dataset_sha256": fingerprint(dataset), "code_sha256": code_digest(), "prompt_version": PROMPT_VERSION,
                    "python": platform.python_version(), "packages": {p: version(p) for p in ["langgraph", "pydantic", "httpx", "tiktoken"]},
                    "model": os.getenv("LLM_MODEL") if config.mode == "live" else "deterministic-fixture",
                    "base_url": os.getenv("LLM_BASE_URL") if config.mode == "live" else None,
                    "tokenizer_estimate": "cl100k_base", "upstream_commit": "04c5750a727f993617d368980a5b7937008d6226"}
        (run_dir / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        source_root = Path(__file__).resolve().parents[1]
        with zipfile.ZipFile(run_dir / "source-snapshot.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for folder in ["workflow", "benchmark"]:
                for source in sorted((source_root / folder).glob("*.py")):
                    archive.write(source, arcname=str(source.relative_to(source_root)))
        jobs = []
        rng = random.Random(config.seed)
        for repetition in range(config.repetitions):
            order = list(scenarios)
            rng.shuffle(order)
            for scenario in order:
                groups = list(config.groups)
                rng.shuffle(groups)
                for cache in config.cache_modes:
                    for group in groups:
                        jobs.append((scenario, repetition, cache, group))
        completed, interrupted = [], None
        for job_index, (scenario, repetition, cache, group) in enumerate(jobs):
            job_id = f"{scenario.case_id}__r{repetition}__{cache}__{group}"
            trace = Trace(run_dir / "traces" / f"{job_id}.jsonl", case_id=scenario.case_id, category=scenario.category,
                          repetition=repetition, cache_mode=cache, group=group, mode=config.mode)
            self._state_dir(run_dir)
            world = SimulatedWorld(run_dir / "states" / f"{job_id}.sqlite", trace)
            model = OfflineModel(trace) if config.mode == "offline" else OpenAICompatibleModel(config, trace, self.budget)
            engine = WorkflowEngine(group, config, world, trace, model)
            assessments, results = [], []
            started = time.perf_counter()
            try:
                for event, expectation in zip(scenario.events, scenario.expectations):
                    if (run_dir / "STOP").exists():
                        interrupted = {"job_id": job_id, "event_id": event.event_id, "reason": "operator stop marker"}
                        break
                    result = engine.run(event, cache)
                    results.append(result.model_dump())
                    if result.status in {"budget_exhausted", "model_configuration_error"} and config.mode == "live":
                        interrupted = {"job_id": job_id, "event_id": event.event_id, "reason": result.error}
                        break
                    assessments.append(evaluate(result, expectation))
                    trace.emit("evaluation", **assessments[-1])
            finally:
                if hasattr(model, "close"):
                    model.close()
                world.close()
            mutations = [e["key"] for e in trace.events if e["kind"] == "mutation"]
            action_count = sum(len(r["proposed_actions"]) for r in results)
            row = {"job_id": job_id, "case_id": scenario.case_id, "category": scenario.category, "group": group,
                   "repetition": repetition, "cache_mode": cache, "mode": config.mode,
                   "complete": len(assessments) == len(scenario.events),
                   "success": len(assessments) == len(scenario.events) and all(a["success"] for a in assessments),
                   "event_count": len(results), "expected_event_count": len(scenario.events),
                   "elapsed_ms": (time.perf_counter() - started) * 1000,
                   **trace.totals(), "proposed_actions": action_count,
                   "decision_opportunities": sum(len({a["campaign_id"] for a in r["proposed_actions"]}) for r in results),
                   "duplicate_executions": len(mutations) - len(set(mutations)),
                   "required_branches": sum(a["required_branches"] for a in assessments),
                   "completed_required_branches": sum(a["completed_required_branches"] for a in assessments),
                   "constraint_checks": sum(a["constraint_checks"] for a in assessments),
                   "constraint_pass": sum(a["constraint_pass"] for a in assessments),
                   "failures": [{"event_id": r["event_id"], **a} for r, a in zip(results, assessments) if not a["success"]]}
            with (run_dir / "cases.jsonl").open("a", encoding="utf-8") as out:
                out.write(canonical(row) + "\n")
            if row["complete"]:
                completed.append(job_id)
            checkpoint = {"completed_jobs": completed, "interrupted": interrupted,
                          "next_job_index": job_index + (0 if interrupted else 1), "planned_jobs": len(jobs),
                          "requests": self.budget.requests, "budget_charged_tokens": self.budget.charged_tokens,
                          "remaining_jobs": [f"{s.case_id}__r{r}__{c}__{g}" for s, r, c, g in jobs[job_index + 1:]],
                          "note": "State DBs and traces retained. Interrupted jobs are incomplete; not successes/failures. Start a new configured run to replay; no automatic duplicate paid calls."}
            (run_dir / "checkpoint.json").write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
            if interrupted:
                break
        from .report import generate_report
        generate_report(run_dir)
        return run_dir

    @staticmethod
    def _state_dir(run_dir):
        (run_dir / "states").mkdir(exist_ok=True)
        return True


def validate(config: ExperimentConfig) -> dict:
    """Entire development/test fixture set, optimal group, zero real network/model calls."""
    verify_frozen_dataset(config)
    offline = config.model_copy(update={"mode": "offline"})
    failures = []
    scenarios = make_scenarios(config.split, config.cases_per_category, config.seed)
    events = 0
    for scenario in scenarios:
        trace = Trace()
        world = SimulatedWorld(":memory:", trace)
        engine = WorkflowEngine("B3", offline, world, trace, OfflineModel(trace))
        try:
            for event, expected in zip(scenario.events, scenario.expectations):
                result = engine.run(event)
                assessment = evaluate(result, expected)
                events += 1
                if not assessment["success"]:
                    failures.append({"case_id": scenario.case_id, "event_id": event.event_id, "errors": assessment["errors"]})
        finally:
            world.close()
    return {"mode": "offline", "cases": len(scenarios), "events": events, "failures": failures,
            "success": not failures, "dataset_sha256": fingerprint([s.model_dump() for s in scenarios])}
